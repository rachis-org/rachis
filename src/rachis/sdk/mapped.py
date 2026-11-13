# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import csv
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from itertools import chain, product
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from typing import get_args, get_type_hints

import dill

from rachis.core import transform
from rachis.core.archive.provenance import ActionProvenanceCapture
from rachis.core.axis import Coord
from rachis.core.type.util import is_collection_type
from rachis.plugin.model.base import FormatBase
from rachis.sdk.action import (
    Frame, Method, checked_return_values, prepare_signature,
)
from rachis.sdk.plugin_manager import PluginManager
from rachis.sdk.result import Artifact
from rachis.sdk.results import Results


@dataclass(frozen=True)
class ArtifactCell:
    artifact: Artifact
    pattern: object
    coord: Coord

    def load(self):
        return self.artifact._axis(self.pattern)[self.coord]


@dataclass(frozen=True)
class Cell:
    path: Path
    inputs: dict
    outputs: dict


@dataclass(frozen=True)
class StoredView:
    path: Path
    format: type | None = None

    @classmethod
    def save(cls, value, path):
        if isinstance(value, FormatBase):
            if Path(value.path).is_dir():
                shutil.copytree(value.path, path)
            else:
                shutil.copyfile(value.path, path)
            return cls(path, type(value))
        with path.open('wb') as stream:
            dill.dump(value, stream)
        return cls(path)

    def load(self):
        if self.format is not None:
            return self.format(self.path, mode='r')
        with self.path.open('rb') as stream:
            return dill.load(stream)


@dataclass(frozen=True)
class CellOutcome:
    outputs: dict | None
    transforms: tuple = ()
    error: str | None = None


class CellFrame(Frame):
    def __init__(self, parent, bound_arguments, cell, axes, workspace):
        super().__init__(parent.action, parent.ctx, bound_arguments)
        self.cell = cell
        self.coordinate = next(iter(cell.outputs.values()))
        self.axes = axes
        self.workspace = workspace


class MappedProvenance(ActionProvenanceCapture):
    def make_execution_section(self):
        execution = super().make_execution_section()
        execution['coordinates'] = self.coordinates
        return execution

    def transformation_recorder(self, name):
        previous = self.transformers.get(name, ())
        recorder = super().transformation_recorder(name)
        self.transformers[name].extend(previous)
        return recorder


def read_outcome(path):
    with path.open('rb') as stream:
        return dill.load(stream)


def _input_index(value, pattern):
    if value is None:
        return {}
    if isinstance(value, int):
        if value < 0:
            raise ValueError('A mapped integer must be nonnegative')
        return {(i,): i for i in range(value)}
    artifact = value.result()
    if not isinstance(artifact, Artifact):
        raise TypeError('ForEach requires an artifact with registered axes')
    index = {}
    for coord in artifact.coords(*pattern.axes):
        key = tuple(coord[name] for name in pattern.axes)
        if key in index:
            raise ValueError(f'Duplicate axis coordinate {coord!r}')
        index[key] = ArtifactCell(artifact, pattern.as_unbound(), coord)
    return index


def _includes(rules, present):
    matched = [present[name] for name, rule in rules.items()
               if rule == 'matched']
    optional = [present[name] for name, rule in rules.items()
                if rule == 'optional']
    if matched:
        return all(matched)
    if optional:
        return any(optional)
    return any(present.values())


def _check_required(rules, present, coordinate):
    missing = [name for name, rule in rules.items()
               if rule == 'required' and not present[name]]
    if missing:
        raise ValueError(f'Required inputs {missing!r} lack {coordinate!r}')


def _groups(signature):
    groups = {}
    for name, pattern in signature.input_patterns.items():
        names = groups.setdefault(frozenset(pattern.bindings), {})
        names[name] = signature.input_coverage[name]
    return groups.values()


def _group_plan(signature, inputs, rules):
    first = next(iter(rules))
    bindings = signature.input_patterns[first].bindings
    indexes = {}
    for name in rules:
        pattern = signature.input_patterns[name]
        order = tuple(pattern.bindings.index(binding) for binding in bindings)
        indexes[name] = {
            tuple(coord[i] for i in order): selection
            for coord, selection in _input_index(inputs[name], pattern).items()
        }
    keys = dict.fromkeys(key for index in indexes.values() for key in index)
    plan = []
    for key in keys:
        selected = {name: index.get(key) for name, index in indexes.items()}
        present = {name: value is not None for name, value in selected.items()}
        if not _includes(rules, present):
            continue
        _check_required(rules, present, key)
        plan.append((dict(zip(bindings, key)), selected))
    return plan


def plan_cells(frame, directory):
    signature = frame.action.signature
    # Whole artifacts provide their declared semantic types to the solver.
    prepare_signature(frame)
    groups = [_group_plan(signature, frame.bound_arguments, rules)
              for rules in _groups(signature)]
    broadcast = {name: value for name, value in frame.call_kwargs.items()
                 if name not in signature.input_patterns}
    cells = []
    for number, combination in enumerate(product(*groups)):
        bound = {}
        selections = dict(broadcast)
        for binding, selected in combination:
            bound.update(binding)
            selections.update(selected)
        outputs = {
            name: Coord(**dict(zip(pattern.axes,
                                   (bound[b] for b in pattern.bindings))))
            for name, pattern in signature.output_patterns.items()
        }
        cells.append(Cell(directory / f'{number}.cell', selections, outputs))
    return cells, output_axes(frame)


def _as_view(value, annotation, name, trace):
    candidates = get_args(annotation) if type(None) in get_args(annotation) \
        else (annotation,)
    source = transform.ModelType.from_view_type(type(value))
    for view_type in candidates:
        if view_type is type(None):
            continue
        target = transform.ModelType.from_view_type(view_type)
        if source.has_transformation(target):
            result = source.make_transformation(target)(value)
            trace.append((name, type(value), view_type))
            return result
    raise TypeError(
        f'No cell transformation from {type(value)} to {annotation}')


@contextmanager
def cell_provenance_wrapper(frame):
    frame.provenance = SimpleNamespace(transforms=[])
    yield
    frame.results = CellOutcome(
        frame.results, tuple(frame.provenance.transforms))


@contextmanager
def cell_transform_wrapper(frame):
    _prepare_cell_views(frame)
    yield
    _stage_cell_outputs(frame)


def _prepare_cell_views(frame):
    signature = frame.action.signature
    for name, spec in signature.signature_order.items():
        value = frame.bound_arguments[name]
        if name in signature.input_patterns:
            if isinstance(value, ArtifactCell):
                value = value.load()
            if value is not None:
                value = _as_view(
                    value, spec.view_type, name, frame.provenance.transforms)
            elif type(None) not in get_args(spec.view_type):
                raise TypeError(f'Input {name!r} does not accept None')
        elif name in signature.inputs and value is not None:
            if is_collection_type(spec.qiime_type):
                if isinstance(value, list):
                    value = [item._view(spec.view_type) for item in value]
                else:
                    value = type(value)({key: item._view(spec.view_type)
                                         for key, item in value.items()})
            else:
                value = value._view(spec.view_type)
        frame.call_kwargs[name] = value


def _stage_cell_outputs(frame):
    signature = frame.action.signature
    views = checked_return_values(frame.results, len(signature.outputs))
    outputs = {}
    for index, ((name, spec), value) in enumerate(
        zip(signature.outputs.items(), views)
    ):
        args = get_args(spec.view_type)
        if value is None and type(None) in args:
            outputs[name] = None
            continue
        view_type = spec.view_type
        if type(None) in args:
            view_type = next(t for t in args if t is not type(None))
        if type(value) is not view_type:
            raise TypeError(f'Output {name!r} expected {view_type!r}, '
                            f'received {type(value)!r}')
        axis_view = get_type_hints(frame.axes[name].__getitem__)['return']
        if axis_view is not view_type:
            value = _as_view(value, axis_view, ('return', name),
                             frame.provenance.transforms)
        outputs[name] = StoredView.save(value, frame.workspace / str(index))
    frame.results = outputs


def output_axes(frame):
    patterns = frame.action.signature.output_patterns
    return {
        name: PluginManager().get_axis(frame.output_types[name].qiime_type,
                                       pattern)
        for name, pattern in patterns.items()
    }


def _record_transforms(provenance, name, pairs):
    if pairs:
        recorder = provenance.transformation_recorder(name)
        for source, target in sorted(pairs, key=repr):
            transform.ModelType.from_view_type(source).make_transformation(
                transform.ModelType.from_view_type(target), recorder=recorder)


def _read_cell_outcomes(frame):
    frame.outcomes = [read_outcome(cell.path) for cell in frame.cells]
    for cell, outcome in zip(frame.cells, frame.outcomes):
        if outcome.error is not None:
            raise RuntimeError(
                f'Mapped cell {next(iter(cell.outputs.values()))!r} failed: '
                f'{outcome.error}')


def _prepare_mapped_provenance(frame):
    action = frame.action
    signature = action.signature
    provenance = frame.provenance = MappedProvenance(
        action.type, action.plugin_id, action.id, frame.ctx.execution_ctx)
    for name, value in frame.call_kwargs.items():
        if name in signature.inputs:
            provenance.add_input(name, value)
        else:
            provenance.add_parameter(
                name, signature.parameters[name].qiime_type, value)
    frame.transforms = {}
    for outcome in frame.outcomes:
        for name, source, target in outcome.transforms:
            frame.transforms.setdefault(name, set()).add((source, target))
    for name in signature.inputs:
        _record_transforms(provenance, name, frame.transforms.get(name))


def _write_outputs(frame):
    axes = output_axes(frame)
    outputs = []
    for name in frame.output_types:
        selected = [
            (cell.outputs[name], outcome.outputs[name])
            for cell, outcome in zip(frame.cells, frame.outcomes)
            if outcome.outputs is not None
        ]
        chunks = ((coord, value.load()) for coord, value in selected
                  if value is not None)
        outputs.append((axes[name].write(chunks), selected))
    return outputs


def _publish_outputs(frame, outputs):
    artifacts = []
    for (name, spec), (directory, selected) in zip(
        frame.output_types.items(), outputs, strict=True
    ):
        capture = frame.provenance.fork(name)
        _record_transforms(
            capture, 'return', frame.transforms.get(('return', name)))
        pattern = frame.action.signature.output_patterns[name]
        capture.coordinates = {
            'axes': list(pattern.axes), 'records': 'coordinates.tsv'}
        with (capture.action_dir / 'coordinates.tsv').open('w') as stream:
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow((*pattern.axes, 'attempt', 'outcome'))
            for coord, value in selected:
                writer.writerow((*coord.values(), 1,
                                 'filtered' if value is None else 'completed'))
        # The writer has already assembled the registered directory format.
        artifact = Artifact._from_view(
            spec.qiime_type, directory, type(directory), capture)
        artifacts.append(frame.ctx.add_reference(artifact))
    return Results(frame.output_types, artifacts)


def iter_batches(ctx, cells):
    batches = [tuple(batch) for batch in ctx._iter_batches_(cells)]
    expected = {cell.path for cell in cells}
    scheduled = [cell.path for batch in batches for cell in batch]
    if len(scheduled) != len(expected) or set(scheduled) != expected:
        raise ValueError('Batches must partition the planned cells')
    return (batch for batch in batches if batch)


class MappedMethod(Method):
    """A method that invokes its callable once per aligned coordinate."""

    mapped_outer_wrappers = ()
    cell_wrappers = (
        cell_provenance_wrapper,
        cell_transform_wrapper,
    )

    def _inner_apply(self, frame):
        """Plan and dispatch the cells for one mapped invocation."""
        ctx = frame.ctx
        with ExitStack() as stack:
            for wrapper in chain(ctx.mapped_outer_wrappers,
                                 self.mapped_outer_wrappers):
                stack.enter_context(wrapper(frame))
            frame.cells, axes = plan_cells(frame, frame.directory)
            frame.batches = [
                (batch, ctx._dispatch_batch_(frame, batch, axes))
                for batch in iter_batches(ctx, frame.cells)
            ]
        return frame.results

    def _outer_return(self, frame):
        _read_cell_outcomes(frame)
        _prepare_mapped_provenance(frame)
        frame.results = _publish_outputs(frame, _write_outputs(frame))
        return frame.results

    def _run_batch_(self, frame, batch, axes):
        for cell in batch:
            self._run_cell_(frame, cell, axes)

    def _run_cell_(self, frame, cell, axes):
        # A repeated batch can reuse cells committed by its first attempt.
        if cell.path.exists():
            return
        workspace = Path(tempfile.mkdtemp(dir=cell.path.parent))
        keep = False
        try:
            try:
                outcome = self._mapped_inner_apply(
                    frame, cell, axes, workspace)
                if not isinstance(outcome, CellOutcome):
                    raise TypeError('Cell wrappers must produce a CellOutcome')
            except Exception as error:
                outcome = CellOutcome(
                    None, error=f'{type(error).__name__}: {error}')
            manifest = workspace / 'outcome'
            with manifest.open('wb') as stream:
                dill.dump(outcome, stream)
            try:
                # Release consumers only after every wrapper has exited.
                os.link(manifest, cell.path)
                keep = bool(outcome.outputs)
            except FileExistsError:
                pass
        finally:
            if not keep:
                shutil.rmtree(workspace)

    def _mapped_inner_apply(self, frame, cell, axes, workspace):
        local = CellFrame(frame, cell.inputs, cell, axes, workspace)
        with ExitStack() as stack:
            for wrapper in chain(frame.ctx.cell_wrappers, self.cell_wrappers):
                stack.enter_context(wrapper(local))
            local.results = self._callable(**local.call_kwargs)
        return local.results
