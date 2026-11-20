# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import copy
import csv
from concurrent.futures import wait
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from itertools import product
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
    Frame, FramePhase, Method, checked_return_values,
    prepare_callable_arguments_wrapper, provenance_wrapper,
)
from rachis.sdk.plugin_manager import PluginManager
from rachis.sdk.futures import after_all, join, result_as_future
from rachis.sdk.proxy import ProxyResult, ProxyResultCollection
from rachis.sdk.result import Artifact, ResultCollection
from rachis.sdk.results import Results
from rachis.sdk.util import Sievable


class CellView:
    """A cell view whose backing resources survive through output staging."""

    @contextmanager
    def open(self):
        yield self.load()


@dataclass(frozen=True)
class ArtifactCell(CellView):
    artifact: Artifact
    pattern: object
    coord: Coord

    @contextmanager
    def open(self):
        with self.artifact._axis(self.pattern) as (axis, _):
            yield axis[self.coord]


@dataclass(frozen=True)
class Cell:
    path: Path
    inputs: dict
    outputs: dict


@dataclass(frozen=True)
class StoredView(CellView):
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
    outputs: dict
    transforms: tuple = ()


class CellFrame(Frame):
    def __init__(self, parent, cell, axes):
        super().__init__(parent.action, parent.ctx, cell.inputs,
                         phase=FramePhase.INNER_APPLY)
        self.cell = replace(cell, inputs={})
        self.coordinate = next(iter(cell.outputs.values()))
        self.axes = axes

        self.output_types = parent.output_types

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


def _input_index(value, pattern):
    if value is None:
        return {}
    if isinstance(value, int):
        if value < 0:
            raise ValueError('A mapped integer must be nonnegative')
        axis, = pattern.axes
        return {Coord(**{axis: i}): i for i in range(value)}
    artifact = value.result()
    if not isinstance(artifact, Artifact):
        raise TypeError('ForEach requires an artifact with registered axes')
    index = {}
    for coord in artifact.coords(*pattern.axes):
        if coord in index:
            raise ValueError(f'Duplicate axis coordinate {coord!r}')
        index[coord] = ArtifactCell(artifact, pattern.as_unbound(), coord)
    return index


def _group_plan(signature, inputs, group):
    patterns = signature.input_patterns
    return group.align({
        name: _input_index(inputs[name], patterns[name])
        for name in group.inputs
    })


def plan_cells(frame, directory):
    signature = frame.action.signature
    groups = [_group_plan(signature, frame.bound_arguments, group)
              for group in signature.input_groups]
    broadcast = {
        name: value for name, value in frame.bound_arguments.items()
        if name not in signature.input_patterns
    }
    cells = {}
    for number, combination in enumerate(product(*groups)):
        bound = {}
        selections = dict(broadcast)
        for binding, selected in combination:
            bound.update(binding)
            selections.update(selected)
        coordinate = Coord(**bound)
        outputs = {
            name: pattern.to_axis_coord(coordinate)
            for name, pattern in signature.output_patterns.items()
        }
        cells[coordinate] = Cell(
            directory / f'{number}.cell', selections, outputs)
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
def cell_error_wrapper(frame):
    try:
        yield
    except Exception as error:
        raise RuntimeError(f'Mapped cell {frame.coordinate!r} failed: '
                           f'{error}') from error


@contextmanager
def cell_workspace_wrapper(frame):
    frame.workspace = Path(tempfile.mkdtemp(dir=frame.cell.path.parent))
    try:
        yield
    except BaseException:
        shutil.rmtree(frame.workspace)
        raise


@contextmanager
def cell_provenance_wrapper(frame):
    frame.provenance = SimpleNamespace(transforms=[])
    yield
    frame.results = CellOutcome(
        frame.results, tuple(frame.provenance.transforms))


@contextmanager
def cell_views_wrapper(frame):
    signature = frame.action.signature
    with ExitStack() as views:
        frame.transformed_arguments = {}
        for name, spec in signature.signature_order.items():
            value = frame.bound_arguments[name]
            if name in signature.input_patterns:
                if isinstance(value, CellView):
                    value = views.enter_context(value.open())
                if value is not None:
                    value = _as_view(
                        value, spec.view_type, name,
                        frame.provenance.transforms)
                elif type(None) not in get_args(spec.view_type):
                    raise TypeError(f'Input {name!r} does not accept None')
            elif name in signature.inputs and value is not None:
                if is_collection_type(spec.qiime_type):
                    if isinstance(value, ProxyResultCollection):
                        value = value.result()
                    if isinstance(value, ResultCollection):
                        value = signature._shape_collection_input(value, spec)
                    if isinstance(value, list):
                        value = [item._view(spec.view_type) for item in value]
                    else:
                        value = type(value)({key: item._view(spec.view_type)
                                             for key, item in value.items()})
                else:
                    value = value._view(spec.view_type)
            frame.transformed_arguments[name] = value

        yield

        returned_views = checked_return_values(
            frame.results, len(signature.outputs))
        outputs = {}
        for index, ((name, spec), value) in enumerate(
            zip(signature.outputs.items(), returned_views)
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
            outputs[name] = StoredView.save(
                value, frame.workspace / str(index))
        frame.results = outputs


def output_axes(frame):
    manager = PluginManager()
    return {
        name: manager.get_axis(
            frame.output_types[name].qiime_type, pattern).axis
        for name, pattern in frame.action.signature.output_patterns.items()
    }


def _record_transforms(provenance, name, pairs):
    if pairs:
        recorder = provenance.transformation_recorder(name)
        for source, target in sorted(pairs, key=repr):
            transform.ModelType.from_view_type(source).make_transformation(
                transform.ModelType.from_view_type(target), recorder=recorder)


@contextmanager
def mapped_transforms_wrapper(frame):
    provenance = frame.provenance
    frame.transforms = {}
    for outcome in frame.outcomes.values():
        if outcome is None:
            continue
        for name, source, target in outcome.transforms:
            frame.transforms.setdefault(name, set()).add((source, target))
    for name in frame.action.signature.inputs:
        _record_transforms(provenance, name, frame.transforms.get(name))
    yield


def _write_outputs(frame):
    outputs = []
    for name in frame.output_types:
        selected = [
            (frame.cells[coord].outputs[name], outcome.outputs[name])
            for coord, outcome in frame.outcomes.items()
            if outcome is not None
        ]
        chunks = ((coord, value.load()) for coord, value in selected
                  if value is not None)
        outputs.append((frame.axes[name].write(chunks), selected))
    return outputs


@contextmanager
def mapped_workspace_wrapper(frame):
    with ExitStack() as cleanup:
        frame.workspace_cleanup = cleanup
        frame.directory = Path(cleanup.enter_context(
            tempfile.TemporaryDirectory(dir=frame.ctx.cache.path)))
        try:
            yield
        finally:
            frame.ctx._defer_cleanup(cleanup)


@contextmanager
def mapped_plan_wrapper(frame):
    frame.cells, frame.axes = plan_cells(frame, frame.directory)
    yield


@contextmanager
def archive_mapped_outputs_wrapper(frame):
    yield

    provenance = frame.ready_provenance.result()
    artifacts = []
    for (name, spec), (directory, selected) in zip(
        frame.output_types.items(), frame.results, strict=True
    ):
        capture = provenance.fork(name)
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
    frame.results = Results(frame.output_types, artifacts)


@contextmanager
def gather_cells_wrapper(frame):
    yield
    futures = frame.results
    if futures.keys() != frame.cells.keys():
        raise ValueError('Cell dispatch must return each coordinate once')

    # Assembly owns its state while dispatch retains the exterior result.
    completed = copy.copy(frame)

    def assemble(inputs, *outcomes):
        completed.bound_arguments = inputs
        completed.outcomes = dict(zip(completed.cells, outcomes, strict=True))
        with (
            prepare_callable_arguments_wrapper(completed),
            archive_mapped_outputs_wrapper(completed),
            provenance_wrapper(completed),
            mapped_transforms_wrapper(completed),
        ):
            completed.results = _write_outputs(completed)
        return completed.results

    proxies, rebuild_inputs = Sievable.sieve(
        frame.bound_arguments,
        isinstance=(ProxyResult, ProxyResultCollection),
    )
    inputs_ready = after_all(
        map(result_as_future, proxies), rebuild_inputs,
    )
    future = join(
        (inputs_ready,
         *(futures[coord] for coord in frame.cells)), assemble, ctx=frame.ctx)
    frame.workspace_cleanup.callback(wait, (future,))
    frame.results = Results._from_future(future, frame.output_types)


class MappedMethod(Method):
    """Bind a map, dispatch its cells, and gather their exterior outputs."""

    _ProvCaptureCls = MappedProvenance
    _initial_frame_phase = FramePhase.INNER_BIND
    inner_bind_wrappers = (
        mapped_workspace_wrapper,
        gather_cells_wrapper,
        mapped_plan_wrapper,
    )
    inner_apply_wrappers = (
        cell_error_wrapper,
        cell_workspace_wrapper,
        cell_provenance_wrapper,
        cell_views_wrapper,
    )

    def _inner_bind(self, frame):
        with ExitStack() as stack:
            for wrapper in self.inner_bind_wrappers:
                stack.enter_context(wrapper(frame))
            cell_frames = {
                coord: CellFrame(frame, cell, frame.axes)
                for coord, cell in frame.cells.items()
            }
            frame.results = frame.ctx._dispatch_cells_(cell_frames)
        return frame.results
