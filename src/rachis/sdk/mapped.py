# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

"""Bind coordinate calls and assemble their committed cells locally."""

import copy
import csv
from concurrent.futures import Future, wait
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from itertools import chain
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
from typing import Callable, get_args

import dill

from rachis.core import transform
from rachis.core.archive.provenance import ActionProvenanceCapture, _ts_to_date
from rachis.core.axis import Coord
from rachis.plugin.model.base import FormatBase
from rachis.sdk.action import (
    Frame, FramePhase, Method, checked_return_values,
    prepare_artifact_argument, prepare_callable_arguments_wrapper,
    provenance_wrapper,
)
from rachis.sdk.plugin_manager import PluginManager
from rachis.sdk.futures import after_all, after_settled, join, result_as_future
from rachis.sdk.proxy import ProxyArtifact
from rachis.sdk.result import Artifact
from rachis.sdk.results import Results


@dataclass(frozen=True)
class CellView:
    """A cell view whose backing resources survive through output staging."""

    opener: Callable

    def open(self, recorder=None):
        return self.opener(recorder=recorder)


@dataclass
class PreparedIndex:
    """Input cells, their directory owner, and the preparation trace."""

    cells: dict
    transforms: tuple = ()
    directory: Path | None = None

    def close(self):
        """Release the driver's ownership of the directory."""
        self.directory = None


@dataclass(frozen=True)
class ProxyCell:
    """A future for one output cell or its absence."""

    future: Future

    def result(self):
        return self.future.result()


@contextmanager
def _open_axis_cell(reader, directory, coord, recorder=None):
    axis = reader(directory, mode='r')
    yield axis[coord]


@contextmanager
def _open_staged_view(path, view_type=None, recorder=None):
    if view_type is not None:
        yield view_type(path, mode='r')
    else:
        yield _read_record(path)


def _stage_view(value, path):
    if isinstance(value, FormatBase):
        if Path(value.path).is_dir():
            shutil.copytree(value.path, path)
        else:
            shutil.copyfile(value.path, path)
        return CellView(partial(_open_staged_view, path, type(value)))
    _write_record(path, value)
    return CellView(partial(_open_staged_view, path))


class CellFrame(Frame):
    def __init__(self, parent, coordinate, inputs, outputs, path,
                 output_view_types):
        super().__init__(parent.action, parent.ctx, inputs,
                         next_phase=FramePhase.INNER_APPLY)
        self.path = path
        self.output_coordinates = outputs
        self.coordinate = coordinate
        self.output_view_types = output_view_types
        self.output_types = parent.output_types


def _read_record(path):
    with path.open('rb') as stream:
        return dill.load(stream)


def _write_record(path, value):
    with path.open('wb') as stream:
        dill.dump(value, stream)


def read_commit(path):
    """Read the winning attempt's directory or its recorded failure."""
    return _read_record(path)


def commit_attempt(path, attempt):
    """Publish the completed attempt atomically on the shared filesystem."""
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        manifest = Path(directory) / 'attempt'
        _write_record(manifest, attempt)
        try:
            os.link(manifest, path)
        except FileExistsError:
            if isinstance(attempt, Path):
                shutil.rmtree(attempt)
            attempt = read_commit(path)
    return attempt


def _realized_index(cells, *, into):
    def retain_present(settled):
        # A failed value still occupies its coordinate. Alignment can exclude
        # it, or leave its failed future as a dependency of a downstream call.
        pairs = zip(cells.items(), settled, strict=True)
        present = {key: cell for (key, cell), future in pairs
                   if (future.cancelled() or future.exception() is not None
                       or future.result() is not None)}
        return PreparedIndex(present)

    after_settled((cell.future for cell in cells.values()), retain_present,
                  into=into)


def _index_for_path(semantic_type, source, target, cells):
    """Use the same path or group its cells by a registered prefix.

    Writing just the surviving descendants must give the same prefix view
    and presence as writing the complete artifact.
    """
    if source.axes == target.axes:
        return cells
    length = len(target.axes)
    if source.axes[:length] != target.axes:
        return None
    axes = PluginManager().get_axes(semantic_type)
    reader = axes.get(target.as_unbound())
    if reader is None:
        return None
    writer = axes[source.as_unbound()].axis
    groups = {}
    for coord, cell in cells.items():
        prefix = coord.project(target.axes)
        groups.setdefault(prefix, []).append((coord, cell))

    return {
        coord: _prefix_cell(coord, writer, reader.axis,
                            reader.directory_format, members)
        for coord, members in groups.items()
    }


def _prefix_cell(coordinate, writer, reader, reader_format, members):
    def collect(views):
        pairs = zip(members, views, strict=True)
        chunks = tuple((coord, view) for (coord, _), view in pairs
                       if view is not None)
        if chunks:
            return CellView(partial(
                _open_prefix, coordinate, writer, reader, reader_format, chunks
            ))
        return None

    return ProxyCell(after_all((cell.future for _, cell in members), collect))


@contextmanager
def _open_prefix(coordinate, writer, reader, reader_format, chunks,
                 recorder=None):
    with ExitStack() as resources:
        directory = writer.write(
            (coord, resources.enter_context(view.open(recorder)))
            for coord, view in chunks
        )
        readable = directory
        if type(directory) is not reader_format:
            source = transform.ModelType.from_view_type(type(directory))
            target = transform.ModelType.from_view_type(reader_format)
            readable = source.make_transformation(
                target, recorder=recorder)(directory)
        yield reader(readable.path, mode='r')[coordinate]


def coordinate_index(value, pattern, *, ctx, need_presence=True):
    """Resolve the keys needed for coverage, with values possibly pending."""
    if not isinstance(value, ProxyArtifact):
        return _index_argument(value, pattern, ctx=ctx)
    index = value._coordinate_index
    artifact = value._future_
    if (index is None or (artifact.done() and not artifact.cancelled()
                          and artifact.exception() is None)):
        return _index_argument(value, pattern, ctx=ctx)

    ready = Future()

    def reveal(done):
        try:
            info = done.result()
            if info is not None:
                source, cells, filterable = info
                cells = _index_for_path(value.type, source, pattern, cells)
                if cells is not None:
                    if filterable and need_presence:
                        _realized_index(cells, into=ready)
                    else:
                        ready.set_result(PreparedIndex(cells))
                    return
            _index_argument(value, pattern, ctx=ctx, into=ready)
        except BaseException as error:
            ready.set_exception(error)

    index.add_done_callback(reveal)
    return ready


class MappedProvenance(ActionProvenanceCapture):
    def make_execution_section(self):
        execution = super().make_execution_section()
        execution['coordinates'] = self.coordinates
        return execution


def _input_index(value, pattern):
    if value is None:
        return PreparedIndex({})
    if isinstance(value, int):
        if value < 0:
            raise ValueError('A mapped integer must be nonnegative')
        axis, = pattern.axes
        return PreparedIndex({Coord(**{axis: i}): i for i in range(value)})
    if not isinstance(value, Artifact):
        raise TypeError('ForEach requires an artifact with registered axes')
    index = {}
    transforms = []
    with value._axis(pattern, _transform_recorder(transforms.append)) as (
        axis, directory,
    ):
        # The driver keeps the owned path. Workers receive just its address.
        path = Path(str(directory))
        for coord in axis:
            if coord in index:
                raise ValueError(f'Duplicate axis coordinate {coord!r}')
            index[coord] = CellView(partial(
                _open_axis_cell, type(axis), path, coord))
    return PreparedIndex(index, tuple(transforms), directory)


def _index_argument(value, pattern, *, ctx, into=None):
    def build(values):
        with ctx.cache:
            return _input_index(values[0], pattern)

    return after_all((result_as_future(value),), build, into=into)


def plan_cells(frame, indexes, directory):
    signature = frame.action.signature
    broadcast = {
        name: value for name, value in frame.bound_arguments.items()
        if name not in signature.input_patterns
    }
    axes = output_axes(frame)
    output_view_types = {name: axis.view_type for name, axis in axes.items()}
    cells = {}
    rows = signature.solve_coordinates(
        {name: index.cells for name, index in indexes.items()}
    )
    for number, (coordinate, selected) in enumerate(rows):
        outputs = {
            name: pattern.to_axis_coord(coordinate)
            for name, pattern in signature.output_patterns.items()
        }
        path = directory / f'{number}.cell'
        cells[coordinate] = CellFrame(
            frame, coordinate, {**broadcast, **selected}, outputs, path,
            output_view_types,
        )
    return cells, axes


def _as_view(value, annotation, recorder):
    candidates = get_args(annotation) if type(None) in get_args(annotation) \
        else (annotation,)
    source = transform.ModelType.from_view_type(type(value))
    for view_type in candidates:
        if view_type is type(None):
            continue
        target = transform.ModelType.from_view_type(view_type)
        if source.has_transformation(target):
            return source.make_transformation(target, recorder=recorder)(value)
    raise TypeError(
        f'No cell transformation from {type(value)} to {annotation}')


@contextmanager
def cell_workspace_wrapper(frame):
    """Stage one invocation's views and trace in its attempt directory."""
    frame.workspace = Path(tempfile.mkdtemp(dir=frame.path.parent))
    try:
        yield
        _write_record(frame.workspace / 'outputs', frame.results)
    except BaseException as error:
        shutil.rmtree(frame.workspace)
        if not isinstance(error, Exception):
            raise
        raise RuntimeError(f'Mapped cell {frame.coordinate!r} failed: '
                           f'{error}') from error
    frame.results = frame.workspace


@contextmanager
def cell_provenance_wrapper(frame):
    start = _ts_to_date(time.time()).isoformat()
    transforms = []

    def recorder(name):
        return _transform_recorder(
            lambda record: transforms.append((name, *record)))

    frame.provenance = SimpleNamespace(transformation_recorder=recorder)
    yield
    _write_record(frame.workspace / 'transformers', {
        'start': start,
        'end': _ts_to_date(time.time()).isoformat(),
        'transformers': tuple(transforms),
    })


def _transform_recorder(append):
    def record(transformer_record, input_name, input_record, output_name,
               output_record):
        # Identify the transformer's registered types, even when the model
        # wrapped it to adapt a single-file directory format.
        key = None
        if transformer_record is not None:
            annotations = transformer_record.transformer.__annotations__.copy()
            target = annotations.pop('return')
            source, = annotations.values()
            key = (source, target)
        append((input_name, output_name, key))

    return record


@contextmanager
def cell_views_wrapper(frame):
    signature = frame.action.signature
    recorder_for = frame.provenance.transformation_recorder
    with ExitStack() as views:
        frame.transformed_arguments = {}
        for name, spec in signature.signature_order.items():
            value = frame.bound_arguments[name]
            if name in signature.input_patterns:
                recorder = recorder_for(name)
                if isinstance(value, CellView):
                    value = views.enter_context(value.open(recorder))
                if value is not None:
                    value = _as_view(value, spec.view_type, recorder)
                elif type(None) not in get_args(spec.view_type):
                    raise TypeError(f'Input {name!r} does not accept None')
            elif name in signature.inputs:
                value = prepare_artifact_argument(signature, name, value)
                value = signature._transform_input(name, value, recorder_for)
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
            axis_view = frame.output_view_types[name]
            if axis_view is not view_type:
                value = _as_view(
                    value, axis_view, recorder_for(('return', name)))
            outputs[name] = _stage_view(
                value, frame.workspace / str(index))
        frame.results = outputs


def output_axes(frame):
    manager = PluginManager()
    return {
        name: manager.get_axis(
            frame.output_types[name].qiime_type, pattern)
        for name, pattern in frame.action.signature.output_patterns.items()
    }


def _record_transforms(provenance, name, records):
    if records:
        recorder = provenance.transformation_recorder(name)
        manager = PluginManager()
        for source, target, key in records:
            transformer = (None if key is None else
                           manager.transformers[key[0]][key[1]])
            recorder(transformer, source, manager.views.get(source),
                     target, manager.views.get(target))


@contextmanager
def mapped_transforms_wrapper(frame):
    provenance = frame.provenance
    frame.transforms = {
        name: dict.fromkeys(index.transforms)
        for name, index in frame.coordinate_indexes.items()
        if index.transforms
    }
    frame.cell_times = {}
    for coord, (attempt, _) in frame.attempts.items():
        trace = _read_record(attempt / 'transformers')
        frame.cell_times[coord] = (trace['start'], trace['end'])
        for name, source, target, key in trace['transformers']:
            frame.transforms.setdefault(name, {})[source, target, key] = None
    for name in frame.action.signature.inputs:
        _record_transforms(provenance, name, frame.transforms.get(name))
    yield


def _write_outputs(frame):
    outputs = []
    for name in frame.output_types:
        selected = [
            (frame.cells[coord].output_coordinates[name], views[name],
             frame.cell_times[coord])
            for coord, (_, views) in frame.attempts.items()
        ]
        with ExitStack() as resources:
            chunks = ((coord, resources.enter_context(view.open()))
                      for coord, view, _ in selected if view is not None)
            outputs.append((frame.axes[name].axis.write(chunks), selected))
    return outputs


@contextmanager
def mapped_workspace_wrapper(frame):
    with ExitStack() as cleanup:
        frame.workspace_cleanup = cleanup
        for index in frame.coordinate_indexes.values():
            cleanup.callback(index.close)
        frame.directory = Path(cleanup.enter_context(
            tempfile.TemporaryDirectory(dir=frame.ctx.cache.path)))
        try:
            yield
        finally:
            frame.ctx._defer_cleanup(cleanup)


def _archive_outputs(frame, written):
    provenance = frame.ready_provenance.result()
    records = 'cell-coordinates.tsv'
    artifacts = []
    for (name, spec), (directory, selected) in zip(
        frame.output_types.items(), written, strict=True
    ):
        capture = provenance.fork(name)
        _record_transforms(
            capture, 'return', frame.transforms.get(('return', name)))
        pattern = frame.action.signature.output_patterns[name]
        capture.coordinates = {
            'axes': list(pattern.axes), 'records': records}
        with (capture.action_dir / records).open('w') as stream:
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow((*pattern.axes, 'attempt', 'outcome', 'start',
                             'end'))
            for coord, value, (start, end) in selected:
                outcome = 'filtered' if value is None else 'completed'
                writer.writerow((*coord.values(), 1, outcome, start, end))
        # The writer has already assembled the registered directory format.
        artifact = Artifact._from_view(
            spec.qiime_type, directory, type(directory), capture)
        artifacts.append(frame.ctx.add_reference(artifact))
    return Results(frame.output_types, artifacts)


@contextmanager
def mapped_cells_wrapper(frame):
    frame.cells, frame.axes = plan_cells(
        frame, frame.coordinate_indexes, frame.directory)
    yield
    futures = frame.cell_futures
    if futures.keys() != frame.cells.keys():
        raise ValueError('Cell dispatch must return each coordinate once')

    # Assembly owns its state while dispatch retains the exterior result.
    completed = copy.copy(frame)

    def assemble(inputs, attempts, _):
        completed.bound_arguments = inputs
        completed.attempts = dict(zip(completed.cells, attempts, strict=True))
        with (
            prepare_callable_arguments_wrapper(completed),
            provenance_wrapper(completed),
            mapped_transforms_wrapper(completed),
        ):
            written = _write_outputs(completed)
        return _archive_outputs(completed, written)

    inputs_ready = result_as_future(frame.bound_arguments)
    attempts = {
        coord: after_all((futures[coord],), _read_attempt)
        for coord in frame.cells
    }
    cells_ready = after_all(attempts.values(), tuple)
    # Readers can use committed cells while a batch is still running.
    batches_ready = after_all(frame.batch_futures, lambda _: None)
    future = join(
        (inputs_ready, cells_ready, batches_ready), assemble, ctx=frame.ctx,
    )
    frame.workspace_cleanup.callback(wait, (future,))
    outputs = []
    signature = frame.action.signature
    for name, spec in frame.output_types.items():
        cells = {
            cell.output_coordinates[name]: ProxyCell(
                after_all((attempts[coord],), lambda values, name=name:
                          values[0][1][name]))
            for coord, cell in frame.cells.items()
        }
        filterable = type(None) in get_args(signature.outputs[name].view_type)
        # Archive conversion must preserve the advertised coordinate keys.
        index = (signature.output_patterns[name], cells, filterable)
        outputs.append(ProxyArtifact(
            future, name, spec.qiime_type,
            coordinate_index=after_all((), lambda _: index),
        ))
    frame.results = Results(frame.output_types, outputs)


def _read_attempt(values):
    attempt, = values
    return attempt, _read_record(attempt / 'outputs')


class MappedMethod(Method):
    """Bind a map, dispatch its cells, and gather their exterior outputs."""

    _ProvCaptureCls = MappedProvenance
    _supports_parallel = True
    _first_inner_phase = FramePhase.INNER_BIND
    inner_bind_wrappers = (
        mapped_workspace_wrapper,
        mapped_cells_wrapper,
    )
    inner_apply_wrappers = (
        cell_workspace_wrapper,
        cell_provenance_wrapper,
        cell_views_wrapper,
    )

    def _prepare_coordinate_indexes(self, frame):
        """Resolve the mapped arguments' indexes in their declared axes."""
        signature = self.signature
        coverage_inputs = signature.coordinate_coverage_inputs
        indexes = {
            name: coordinate_index(
                frame.bound_arguments[name], pattern, ctx=frame.ctx,
                need_presence=name in coverage_inputs)
            for name, pattern in signature.input_patterns.items()
        }
        ready = after_all(indexes.values(), lambda values:
                          dict(zip(indexes, values, strict=True)))

        def close_on_failure(completed):
            if completed.cancelled() or completed.exception() is not None:
                for future in indexes.values():
                    future.add_done_callback(_close_ready_index)

        ready.add_done_callback(close_on_failure)
        return ready

    def _inner_bind(self, frame):
        if frame.coordinate_indexes is None:
            frame.coordinate_indexes = self._prepare_coordinate_indexes(
                frame).result()
        wrappers = chain(self.inner_bind_wrappers,
                         frame.ctx.inner_bind_wrappers)
        with ExitStack() as stack:
            for wrapper in wrappers:
                stack.enter_context(wrapper(frame))
            frame.cell_futures, frame.batch_futures = (
                frame.ctx._dispatch_cells_(
                    frame.cells, cleanup=frame.workspace_cleanup)
            )
        return frame.results

    def _inner_apply(self, frame):
        if frame.path.is_file():
            frame.results = read_commit(frame.path)
        else:
            try:
                frame.bound_arguments = {
                    name: value.result() if isinstance(value, ProxyCell)
                    else value for name, value in frame.bound_arguments.items()
                }
                attempt = super()._inner_apply(frame)
            except Exception as error:
                attempt = error
            frame.results = commit_attempt(frame.path, attempt)
        if isinstance(frame.results, Exception):
            raise frame.results
        return frame.results


def _close_ready_index(future):
    if not future.cancelled() and future.exception() is None:
        future.result().close()
