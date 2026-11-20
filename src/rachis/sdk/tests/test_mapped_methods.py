# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from contextlib import contextmanager
from concurrent.futures import Future, wait
from copy import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, current_thread
from typing import Annotated
from unittest.mock import patch

from parsl import Config
from parsl.executors import ThreadPoolExecutor
from parsl.serialize import pack_apply_message, unpack_apply_message
import pytest
import dill

from rachis import Artifact, Coord, ForEach, Metadata
from rachis.core.archive import Archiver, ImportProvenanceCapture
from rachis.core.testing.format import OldMappingDirFmt
from rachis.core.testing.type import AssignmentScores, Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.core.util import load_action_yaml
from rachis.plugin import CaptureHolder, Int
from rachis.plugin import Metadata as MetadataType
from rachis.plugin.plugin import TransformerRecord
from rachis.sdk import MappedMethod, PluginManager, Results
from rachis.sdk.action import FramePhase
from rachis.sdk.context import Context, ParallelContext
from rachis.sdk.context.parallel import _submit_batch
from rachis.sdk.futures import after_all, as_future
from rachis.sdk.mapped import commit_attempt, coordinate_index, read_commit
from rachis.sdk.parallel_config import ParallelConfig
from rachis.sdk.proxy import ProxyArtifact


class SerializingExecutor(ThreadPoolExecutor):
    def submit(self, func, resource_specification, *args, **kwargs):
        message = pack_apply_message(func, args, kwargs)
        func, args, kwargs = unpack_apply_message(message)
        return super().submit(func, resource_specification, *args, **kwargs)


@pytest.mark.parametrize('capture_type', [CaptureHolder, CaptureHolder[int]])
def test_mapped_method_rejects_capture_holder_parameters(capture_type):
    def capture_seed(
        data: Annotated[str, ForEach['sample']], seed: capture_type = None,
    ) -> Annotated[str, ForEach['sample']]:
        return data

    plugin = get_dummy_plugin()
    with pytest.raises(TypeError, match='Mapped methods.*CaptureHolder.*seed'):
        plugin.methods.register_function(
            capture_seed, inputs={'data': Mapping}, parameters={'seed': Int},
            outputs={'output': Mapping}, name='Capture a seed', description='',
        )


def test_mapped_output_coverage_defaults_to_matched():
    def copy_samples(
        data: Annotated[str, ForEach['sample':]],
    ) -> Annotated[str, ForEach['sample']]:
        return data

    plugin = get_dummy_plugin()
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            copy_samples, inputs={'data': Mapping}, parameters={},
            outputs={'output': Mapping}, name='Copy samples', description='',
        )
        method = plugin.methods['copy_samples']
        assert method.signature.input_patterns['data'] == ForEach['sample':]
        assert method.signature.output_patterns['output'] == ForEach['sample']
        data = Artifact.import_data('Mapping', {'s1': '4'})
        assert method(data).output.view(dict) == {'s1': '4'}


def test_per_output_filtering():
    plugin = get_dummy_plugin()
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    method = plugin.methods['filter_samples']
    assert isinstance(method, MappedMethod)
    offset = timedelta(hours=5, minutes=30)
    with patch('rachis.core.archive.provenance.tzlocal.get_localzone',
               return_value=timezone(offset)):
        before = datetime.now(timezone.utc)
        retained, all_samples = method(data)
        after = datetime.now(timezone.utc)
    assert retained.view(dict) == {'s1': '4'}
    assert all_samples.view(dict) == {'s1': '4', 's2': '-5'}
    action_dir = retained._archiver.provenance_dir / 'action'
    records = action_dir / 'cell-coordinates.tsv'
    header, *rows = records.read_text().splitlines()
    assert header == 'sample\tattempt\toutcome\tstart\tend'
    assert [row.split('\t')[:3] for row in rows] == [
        ['s1', '1', 'completed'], ['s2', '1', 'filtered'],
    ]
    all_records = (all_samples._archiver.provenance_dir / 'action'
                   / 'cell-coordinates.tsv')
    assert [row.split('\t')[3:] for row in rows] == [
        row.split('\t')[3:] for row in all_records.read_text().splitlines()[1:]
    ]
    for row in rows:
        start, end = map(datetime.fromisoformat, row.split('\t')[3:])
        assert start.utcoffset() == end.utcoffset() == offset
        assert before <= start <= end <= after


def test_cell_coordinates_do_not_overwrite_coordinates_metadata(tmp_path):
    def copy_samples(
        data: Annotated[str, ForEach['sample']], coordinates: Metadata,
    ) -> Annotated[str, ForEach['sample']]:
        return data

    source = tmp_path / 'metadata.tsv'
    source.write_text('sample-id\tlocation\ns1\tnorth\n')
    metadata = Metadata.load(source)
    data = Artifact.import_data(Mapping, {'s1': '4'})
    plugin = get_dummy_plugin()
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            copy_samples, inputs={'data': Mapping},
            parameters={'coordinates': MetadataType},
            outputs={'output': Mapping}, name='Copy samples', description='',
        )
        output = plugin.methods['copy_samples'](data, metadata).output

    action_dir = output._archiver.provenance_dir / 'action'
    assert Metadata.load(action_dir / 'coordinates.tsv') == metadata
    action = load_action_yaml(output._archiver.path)
    records = action['execution']['coordinates']['records']
    assert records == 'cell-coordinates.tsv'
    header, row = (action_dir / records).read_text().splitlines()
    assert header == 'sample\tattempt\toutcome\tstart\tend'
    assert row.split('\t')[:3] == ['s1', '1', 'completed']


def test_mapped_provenance_appends_axis_and_cell_transformations():
    def read_score(score: str) -> int:
        return int(score)

    def write_score(score: int) -> str:
        return str(score)

    def increment(
        data: Annotated[int, ForEach['sample']],
    ) -> Annotated[int, ForEach['sample']]:
        return data + 1

    old = OldMappingDirFmt()
    old.mapping.write_data({'s1': '4', 's2': '5'}, dict)
    data = Artifact._from_archiver(Archiver.from_data(
        Mapping, OldMappingDirFmt, data_initializer=old.path._move_or_copy,
        provenance_capture=ImportProvenanceCapture()))
    plugin = get_dummy_plugin()
    transformers = {
        str: {int: TransformerRecord(read_score, plugin, ())},
        int: {str: TransformerRecord(write_score, plugin, ())},
    }
    with (
        patch.dict(plugin.methods),
        patch.dict(PluginManager().transformers, transformers),
        patch.object(Artifact, '_axis', autospec=True,
                     side_effect=Artifact._axis) as open_axis,
    ):
        plugin.methods.register_function(
            increment, inputs={'data': Mapping}, parameters={},
            outputs={'output': Mapping}, name='Increment', description='')
        output = plugin.methods['increment'](data).output
    assert open_axis.call_count == 1
    assert output.view(dict) == {'s1': '5', 's2': '6'}
    action = output._archiver.provenance_dir / 'action' / 'action.yaml'
    provenance = action.read_text().split('transformers:', 1)[1]
    assert provenance.index('from: OldMappingDirFmt') < provenance.index(
        'from: builtins:str')
    assert provenance.index('from: builtins:int') < provenance.index(
        'from: MappingDirectoryFormat')


def test_transformer_recorders_append_and_forks_own_their_lists():
    capture = ImportProvenanceCapture()
    record = capture.transformation_recorder('return')
    record(None, 'outer', None, 'axis', None)
    first = capture.fork()
    second = capture.fork()
    first.transformation_recorder('return')(None, 'axis', None, 'cell', None)
    assert [record['to'] for record in first.transformers['return']] == [
        'axis', 'cell']
    assert [record['to'] for record in second.transformers['return']] == [
        'axis']


@pytest.mark.parametrize('binding', ['student', 'assignment'])
@pytest.mark.parametrize('coverage', ['matched', 'optional', 'required'])
@pytest.mark.parametrize('score_coverage', ['matched', 'required'])
def test_subset_input_broadcasts_over_composite_coordinates(
    binding, coverage, score_coverage,
):
    observed = []

    def add_offset(
        offset: Annotated[str | None, ForEach['sample':binding:coverage]],
        data: Annotated[str, ForEach[
            'student'::score_coverage, 'assignment'::score_coverage]],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        observed.append((data, offset))
        return str(int(data) + (0 if offset is None else int(offset)))

    plugin = get_dummy_plugin()
    scores = {'s1/a1': '4', 's1/a2': '5', 's2/a1': '6'}
    offsets = {'s1' if binding == 'student' else 'a1': '10', 'unused': '99'}
    index = 0 if binding == 'student' else 1
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            add_offset, inputs={'offset': Mapping, 'data': AssignmentScores},
            parameters={}, outputs={'output': AssignmentScores},
            name='Broadcast an offset', description='',
        )
        method = plugin.methods['add_offset']
        data = Artifact.import_data('AssignmentScores', scores)
        offset = Artifact.import_data('Mapping', offsets)
        if coverage == 'required':
            with pytest.raises(ValueError, match='Required inputs.*offset'):
                method(offset, data)
            assert observed == []
            offsets['s2' if binding == 'student' else 'a2'] = '20'
            offset = Artifact.import_data('Mapping', offsets)
        output = method(offset, data).output

    expected = {}
    expected_calls = []
    for coordinate, value in scores.items():
        amount = offsets.get(coordinate.split('/')[index])
        if amount is None and coverage == 'matched':
            continue
        expected[coordinate] = str(
            int(value) + (0 if amount is None else int(amount)))
        expected_calls.append((value, amount))
    assert output.view(dict) == expected
    assert observed == expected_calls


def test_disjoint_input_bindings_still_form_a_product():
    def add_offsets(
        data: Annotated[str, ForEach['sample':'student']],
        offset: Annotated[str, ForEach['sample':'assignment']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        return str(int(data) + int(offset))

    plugin = get_dummy_plugin()
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            add_offsets, inputs={'data': Mapping, 'offset': Mapping},
            parameters={}, outputs={'output': AssignmentScores},
            name='Add independent offsets', description='',
        )
        data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
        offset = Artifact.import_data('Mapping', {'a1': '10', 'a2': '20'})
        output = plugin.methods['add_offsets'](data, offset).output
    assert output.view(dict) == {
        's1/a1': '14', 's1/a2': '24', 's2/a1': '15', 's2/a2': '25',
    }


def test_empty_product_still_checks_required_coverage():
    def join_scores(
        assignments: Annotated[int, ForEach['assignment']],
        left: Annotated[str, ForEach['sample':'student':'required']],
        right: Annotated[str, ForEach['sample':'student':'required']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        pytest.fail('an empty product should have no cells')

    plugin = get_dummy_plugin()
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            join_scores, inputs={'left': Mapping, 'right': Mapping},
            parameters={'assignments': Int},
            outputs={'output': AssignmentScores},
            name='Join scores', description='',
        )
        left = Artifact.import_data('Mapping', {'s1': '4'})
        right = Artifact.import_data('Mapping', {'s2': '5'})
        method = plugin.methods['join_scores']
        with pytest.raises(ValueError, match=r"Required inputs .*right.*s1"):
            method(0, left, right)
        assert method(0, left, left).output.view(dict) == {}


def test_mapped_method_runs_in_parallel_without_a_pipeline(tmp_path):
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    method = get_dummy_plugin().methods['increment_samples']
    config = Config(
        executors=[SerializingExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )

    with ParallelConfig(config):
        result = method.parallel(data, 2)._result()

    assert result.output.view(dict) == {'s1': '6', 's2': '-3'}


def test_cell_context_exits_before_staging_outputs():
    plugin = get_dummy_plugin()
    method = plugin.methods['increment_samples']
    observed = []
    events = []

    @contextmanager
    def observe(frame):
        assert frame.current_phase is FramePhase.INNER_APPLY
        assert frame.next_phase is FramePhase.FINISHED
        yield
        assert frame.current_phase is FramePhase.INNER_APPLY
        assert not (frame.workspace / 'outputs').exists()
        observed.append(frame.results)
        events.append(frame.coordinate.sample)

    @contextmanager
    def observe_invocation(frame):
        assert frame.current_phase is FramePhase.INNER_BIND
        assert all(coord is cell.coordinate
                   for coord, cell in frame.cells.items())
        assert all(cell.current_phase is None
                   and cell.next_phase is FramePhase.INNER_APPLY
                   for cell in frame.cells.values())
        events.append('mapped-enter')
        yield
        assert frame.current_phase is FramePhase.INNER_BIND
        assert len(frame.cell_futures) == 2
        assert all(future.done() for future in frame.cell_futures.values())
        events.append('mapped-exit')

    class ObservedContext(Context):
        inner_bind_wrappers = (observe_invocation,)
        inner_apply_wrappers = (observe,)

    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    call = method._outer_bind(lambda: ObservedContext(method))
    result = call(data, 1)
    assert result.output.view(dict) == {'s1': '5', 's2': '6'}
    assert observed == ['5', '6']
    assert events == ['mapped-enter', 's1', 's2', 'mapped-exit']


def test_synchronous_child_owns_its_mapped_workspace():
    method = get_dummy_plugin().methods['increment_samples']
    data = Artifact.import_data('Mapping', {'s1': '4'})
    directories = []

    @contextmanager
    def observe_workspace(frame):
        directories.append(frame.directory)
        yield

    call = Context().get_action('dummy_plugin', 'increment_samples')
    with patch.object(method, 'inner_bind_wrappers',
                      (*method.inner_bind_wrappers, observe_workspace)):
        result = call(data, 1)

    assert result.output.view(dict) == {'s1': '5'}
    directory, = directories
    assert not directory.exists()


def test_deferred_cells_gather_locally_and_keep_their_workspace():
    method = get_dummy_plugin().methods['increment_samples']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    pending = []

    class DeferredContext(Context):
        def _dispatch_action_(self, frame):
            return frame()

        def _dispatch_cells_(self, cells, *, cleanup):
            futures = {}
            for coord, cell in cells.items():
                future = futures[coord] = Future()
                pending.append((cell, future))
            return futures, ()

    with DeferredContext() as ctx:
        with DeferredContext(method, parent=ctx) as child:
            result = method._outer_bind(lambda: child)(data, 1)
            completion, = Results._futures(result)
            try:
                assert not completion.done()
                directory = pending[0][0].path.parent
                assert directory.is_dir()
            finally:
                for cell, future in reversed(pending):
                    outcome = as_future(cell)
                    if outcome.exception() is not None:
                        future.set_exception(outcome.exception())
                    else:
                        future.set_result(outcome.result())
        completed = completion.result(timeout=5)
        assert completed.output.view(dict) == {'s1': '5', 's2': '6'}
        assert directory.is_dir()
    assert not directory.exists()


def test_serialized_cells_resume_at_evaluation():
    class SerializingContext(Context):
        def _dispatch_cells_(self, cells, *, cleanup):
            return {
                coord: as_future(dill.loads(dill.dumps(cell)))
                for coord, cell in cells.items()
            }, ()

    method = get_dummy_plugin().methods['increment_samples']
    old = OldMappingDirFmt()
    old.mapping.write_data({'s1': '4', 's2': '5'}, dict)
    data = Artifact._from_archiver(Archiver.from_data(
        Mapping, OldMappingDirFmt, data_initializer=old.path._move_or_copy,
        provenance_capture=ImportProvenanceCapture()))
    call = method._outer_bind(lambda: SerializingContext(method))
    assert call(data, 1).output.view(dict) == {'s1': '5', 's2': '6'}


def test_parsl_plans_ready_coordinates_before_broadcast_inputs_arrive():
    source = Future()
    broadcast = Future()
    planned = Event()
    planning_threads = []
    cell_threads = []

    def add_offsets(
        data: Annotated[str, ForEach['sample']], offsets: dict,
    ) -> Annotated[str, ForEach['sample']]:
        cell_threads.append(current_thread())
        return str(int(data) + int(offsets['amount']))

    @contextmanager
    def observe_plan(frame):
        assert source.done()
        planning_threads.append(current_thread())
        planned.set()
        yield

    plugin = get_dummy_plugin()
    data = Artifact.import_data(Mapping, {'s1': '4', 's2': '5'})
    offsets = Artifact.import_data(Mapping, {'amount': '10'})
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            add_offsets, inputs={'data': Mapping, 'offsets': Mapping},
            parameters={}, outputs={'output': Mapping},
            name='Add offsets to samples', description='',
        )
        method = plugin.methods['add_offsets']
        with (
            patch.object(method, 'inner_bind_wrappers',
                         (*method.inner_bind_wrappers, observe_plan)),
            ParallelConfig(),
            ParallelContext(None) as ctx,
        ):
            try:
                call = ctx.get_action('dummy_plugin', method.id)
                result = call(ProxyArtifact(source, None, Mapping),
                              ProxyArtifact(broadcast, None, Mapping))
                assert not planned.is_set()
                source.set_result(data)
                assert planned.wait(timeout=5)
                assert not cell_threads
            finally:
                if not source.done():
                    source.set_result(data)
                broadcast.set_result(offsets)

        assert result.output.view(dict) == {'s1': '14', 's2': '15'}
        assert len(cell_threads) == 2
        planner, = planning_threads
        assert planner not in cell_threads


def test_cell_workspace_is_removed_when_evaluation_fails():
    method = get_dummy_plugin().methods['increment_samples']
    data = Artifact.import_data('Mapping', {'s1': '4'})
    workspaces = []

    @contextmanager
    def observe(frame):
        workspaces.append(frame.workspace)
        yield

    class ObservedContext(Context):
        inner_apply_wrappers = (observe,)

    with patch.object(method, '_callable', side_effect=ValueError('failure')):
        with pytest.raises(RuntimeError, match='Mapped cell.*s1.*failure'):
            method._outer_bind(lambda: ObservedContext(method))(data, 1)
    assert workspaces
    assert all(not path.exists() for path in workspaces)


@pytest.mark.parametrize('through_pipeline', [False, True])
def test_parsl_resolves_a_coordinate_through_the_dag_before_batch_exit(
    through_pipeline,
):
    plugin = get_dummy_plugin()
    pipeline = plugin.pipelines['mapped_sample_pipeline']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    downstream_committed = Event()
    order = []
    invocations = []
    inner_apply = MappedMethod._inner_apply

    def retain_samples(ctx, data):
        filter_samples = ctx.get_action('dummy_plugin', 'filter_samples')
        return filter_samples(data).all_samples

    def increment_retained(ctx, data):
        if through_pipeline:
            retained, = ctx.get_action('dummy_plugin', 'retain_samples')(data)
        else:
            retained = retain_samples(ctx, data)
        increment = ctx.get_action('dummy_plugin', 'increment_samples')
        intermediate, = increment(retained, 1)
        return increment(intermediate, 2).output

    @contextmanager
    def observe_invocation(frame):
        yield
        results = frame.results
        assert isinstance(results, Results)
        for output in results:
            assert isinstance(output, ProxyArtifact)
        invocations.append(frame.action.id)
        if (frame.action.id == 'increment_samples'
                and frame.bound_arguments['amount'] == 2):
            index = results.output._coordinate_index.result()[1]
            ready = index[Coord(sample='s1')].future
            ready.add_done_callback(mark_committed)

    def mark_committed(future):
        future.result()
        order.append('third-stage s1')
        downstream_committed.set()

    def batches(ctx, cells):
        if ctx.action_obj.id == 'filter_samples':
            yield tuple(cells)
        else:
            for cell in cells:
                yield (cell,)

    def observe_cell(self, frame):
        coord = frame.coordinate
        if frame.action.id == 'filter_samples' and coord.sample == 's2':
            # Block the producer app itself until the third stage commits s1.
            assert downstream_committed.wait(timeout=10), (
                'downstream s1 waited for the producer batch')
            order.append('producer s2')
        return inner_apply(self, frame)

    with patch.dict(plugin.pipelines), TemporaryDirectory() as run_dir:
        plugin.pipelines.register_function(
            retain_samples, inputs={'data': Mapping}, parameters={},
            outputs={'retained': Mapping}, name='Retain samples',
            description='',
        )
        config = Config(
            executors=[SerializingExecutor(label='default', max_threads=3)],
            run_dir=run_dir,
        )
        with (patch.object(pipeline, '_callable', increment_retained),
              patch.object(ParallelContext, '_batches', batches),
              patch.object(MappedMethod, 'inner_bind_wrappers',
                           (observe_invocation,
                            *MappedMethod.inner_bind_wrappers)),
              patch.object(MappedMethod, '_inner_apply', observe_cell),
              ParallelConfig(config)):
            result = pipeline.parallel(data).output

    assert order == ['third-stage s1', 'producer s2']
    assert invocations == [
        'filter_samples', 'increment_samples', 'increment_samples']
    assert result.view(dict) == {'s1': '7', 's2': '-2'}


def test_pending_optional_broadcast_streams_before_producer_batch_finishes():
    plugin = get_dummy_plugin()
    downstream_committed = Event()
    order = []
    inner_apply = MappedMethod._inner_apply

    def broadcast_offset(
        offset: Annotated[str | None, ForEach['sample':'student':'optional']],
        data: Annotated[str, ForEach['student', 'assignment']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        return str(int(data) + (0 if offset is None else int(offset)))

    def broadcast_pipeline(ctx, offsets, scores):
        retained, _ = ctx.get_action('dummy_plugin', 'filter_samples')(offsets)
        return ctx.get_action('dummy_plugin', 'broadcast_offset')(
            retained, scores).output

    def mark_committed(future):
        future.result()
        order.append('consumer s1')
        if len(order) == 2:
            downstream_committed.set()

    @contextmanager
    def observe_invocation(frame):
        yield
        if frame.action.id == 'broadcast_offset':
            cells = frame.results.output._coordinate_index.result()[1]
            for assignment in ('a1', 'a2'):
                cell = cells[Coord(student='s1', assignment=assignment)]
                cell.future.add_done_callback(mark_committed)

    def batches(ctx, cells):
        if ctx.action_obj.id == 'filter_samples':
            yield tuple(cells)
        else:
            for cell in cells:
                yield (cell,)

    def observe_cell(self, frame):
        if (frame.action.id == 'filter_samples'
                and frame.coordinate.sample == 's2'):
            assert downstream_committed.wait(timeout=10), (
                'broadcast consumers waited for the producer batch')
            order.append('producer s2')
        return inner_apply(self, frame)

    with (patch.dict(plugin.methods), patch.dict(plugin.pipelines),
          TemporaryDirectory() as run_dir):
        plugin.methods.register_function(
            broadcast_offset,
            inputs={'offset': Mapping, 'data': AssignmentScores},
            parameters={}, outputs={'output': AssignmentScores},
            name='Broadcast a pending offset', description='',
        )
        plugin.pipelines.register_function(
            broadcast_pipeline,
            inputs={'offsets': Mapping, 'scores': AssignmentScores},
            parameters={}, outputs={'output': AssignmentScores},
            name='Broadcast through a pipeline', description='',
        )
        offsets = Artifact.import_data('Mapping', {'s1': '10', 's2': '-5'})
        scores = Artifact.import_data(
            'AssignmentScores', {'s1/a1': '4', 's1/a2': '5', 's2/a1': '6'})
        config = Config(
            executors=[SerializingExecutor(label='default', max_threads=3)],
            run_dir=run_dir,
        )
        with (patch.object(ParallelContext, '_batches', batches),
              patch.object(MappedMethod, 'inner_bind_wrappers',
                           (observe_invocation,
                            *MappedMethod.inner_bind_wrappers)),
              patch.object(MappedMethod, '_inner_apply', observe_cell),
              ParallelConfig(config)):
            result = plugin.pipelines['broadcast_pipeline'].parallel(
                offsets, scores).output

    assert order == ['consumer s1', 'consumer s1', 'producer s2']
    assert result.view(dict) == {'s1/a1': '14', 's1/a2': '15', 's2/a1': '6'}


@pytest.mark.parametrize('through_pipeline', [False, True])
def test_nested_axis_prefix_streams_after_its_own_cells(
    through_pipeline, tmp_path,
):
    producer_can_finish = Event()
    commits = {}
    inner_apply = MappedMethod._inner_apply
    plugin = get_dummy_plugin()
    data = Artifact.import_data('AssignmentScores', {
        's1/a1': '4', 's1/a2': '5', 's2/a1': '7',
    })

    def copy_scores(
        data: Annotated[str, ForEach['student', 'assignment']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        return data

    def copy_scores_pipeline(ctx, data):
        return ctx.get_action('dummy_plugin', 'copy_scores')(data).output

    def batches(ctx, cells):
        if ctx.action_obj.id == 'copy_scores':
            commits.update((frame.coordinate, frame.path)
                           for frame in cells.values())
            yield tuple(cells)
        else:
            yield from ((coord,) for coord in cells)

    def observe_cell(self, frame):
        if self.id == 'copy_scores' and frame.coordinate.student == 's2':
            assert producer_can_finish.wait(timeout=10)
        if self.id == 'sum_scores':
            assert frame.output_coordinates['totals'] == Coord(
                sample=frame.coordinate.student)
        return inner_apply(self, frame)

    config = Config(
        executors=[SerializingExecutor(label='default', max_threads=3)],
        run_dir=str(tmp_path),
    )
    with patch.dict(plugin.methods), patch.dict(plugin.pipelines):
        plugin.methods.register_function(
            copy_scores, inputs={'data': AssignmentScores}, parameters={},
            outputs={'output': AssignmentScores}, name='Copy scores',
            description='',
        )
        plugin.pipelines.register_function(
            copy_scores_pipeline, inputs={'data': AssignmentScores},
            parameters={}, outputs={'output': AssignmentScores},
            name='Copy scores in a pipeline', description='',
        )
        with (patch.object(ParallelContext, '_batches', batches),
              patch.object(MappedMethod, '_inner_apply', observe_cell),
              ParallelConfig(config), ParallelContext(None) as ctx):
            name = ('copy_scores_pipeline' if through_pipeline
                    else 'copy_scores')
            scores = ctx.get_action('dummy_plugin', name)(data).output
            try:
                totals = ctx.get_action('dummy_plugin', 'sum_scores')(scores)
                output = ctx.get_action('dummy_plugin', 'increment_samples')(
                    totals.totals, 1).output
                _, index, _ = output._coordinate_index.result(timeout=5)
                cell = index[Coord(sample='s1')].future.result(timeout=5)
                with cell.open() as value:
                    assert value == '10'
                assert all(path.is_file() == (coord.student == 's1')
                           for coord, path in commits.items())
                assert not scores._future_.done()
            finally:
                producer_can_finish.set()

    assert output.view(dict) == {'s1': '10', 's2': '8'}
    assert totals.totals._archiver.provenance_dir.joinpath(
        'artifacts', str(scores.uuid)).is_dir()
    assert all(not path.parent.exists() for path in commits.values())


def test_filterable_prefix_uses_realized_range_before_artifact(tmp_path):
    producer_exit = Future()

    def hold_score_batches(cells, executor):
        cells = tuple(cells)
        job = _submit_batch(cells, executor)
        if cells[0].action.id == 'filter_scores':
            return after_all((job, producer_exit), lambda _: None)
        return job

    data = Artifact.import_data('AssignmentScores', {
        's1/a1': '4', 's1/a2': '-1', 's2/a1': '-5',
    })
    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch('rachis.sdk.context.parallel._submit_batch',
                hold_score_batches),
          ParallelConfig(config), ParallelContext(None) as ctx):
        scores = ctx.get_action('dummy_plugin', 'filter_scores')(data).retained
        try:
            sum_scores = ctx.get_action('dummy_plugin', 'sum_scores')
            totals = sum_scores(scores).totals
            _, index, _ = totals._coordinate_index.result(timeout=5)
            assert tuple(index) == (Coord(sample='s1'),)
            cell = index[Coord(sample='s1')].future.result(timeout=5)
            with cell.open() as value:
                assert value == '4'
            assert not scores._future_.done()
            assert not totals._future_.done()
        finally:
            producer_exit.set_result(None)

    assert totals.view(dict) == {'s1': '4'}


def test_incompatible_axis_path_waits_for_artifact():
    artifact = Artifact.import_data('AssignmentScores', {'s1/a1': '4'})
    published = Future()
    index = as_future(lambda: (ForEach['assignment', 'student'], {}, False))
    proxy = ProxyArtifact(
        published, None, AssignmentScores, coordinate_index=index)

    ready = coordinate_index(proxy, ForEach['student'], ctx=Context())
    assert not ready.done()
    published.set_result(artifact)
    cells = ready.result(timeout=5).cells
    assert tuple(cells) == (Coord(student='s1'),)
    with cells[Coord(student='s1')].open() as view:
        assert view == {'a1': '4'}


@pytest.mark.parametrize('coverage', ['optional', 'required'])
def test_optional_broadcast_does_not_restore_filtered_composite_cells(
    coverage, tmp_path,
):
    filtering = Event()
    release_filter = Event()
    producer_exit = Future()
    dispatched = []
    dispatch_cells = ParallelContext._dispatch_cells_

    def observe_dispatch(ctx, cells, *, cleanup):
        if ctx.action_obj.id == 'add_offset':
            dispatched.extend(cells)
        return dispatch_cells(ctx, cells, cleanup=cleanup)

    def hold_score_batches(cells, executor):
        cells = tuple(cells)
        job = _submit_batch(cells, executor)
        if cells[0].action.id == 'filter_scores':
            return after_all((job, producer_exit), lambda _: None)
        return job
    observed = []

    def filter_scores(
        data: Annotated[str, ForEach['student', 'assignment']],
    ) -> Annotated[str | None, ForEach['student', 'assignment']]:
        if int(data) < 0:
            filtering.set()
            assert release_filter.wait(timeout=5)
            return None
        return data

    def add_offset(
        data: Annotated[str | None, ForEach[
            'student'::coverage, 'assignment'::coverage]],
        offset: Annotated[str | None, ForEach['sample':'student':'optional']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        observed.append((data, offset))
        return str(int(data or 0) + int(offset or 0))

    plugin = get_dummy_plugin()
    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=3)],
        run_dir=str(tmp_path),
    )
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            filter_scores, inputs={'data': AssignmentScores}, parameters={},
            outputs={'retained': AssignmentScores},
            name='Filter scores', description='',
        )
        plugin.methods.register_function(
            add_offset, inputs={'data': AssignmentScores, 'offset': Mapping},
            parameters={}, outputs={'output': AssignmentScores},
            name='Add an optional offset', description='',
        )
        scores = Artifact.import_data(
            'AssignmentScores', {'s1/a1': '4', 's1/a2': '-5', 's2/a1': '6'})
        offsets = Artifact.import_data('Mapping', {'s1': '10'})
        with (patch.object(ParallelContext, '_dispatch_cells_',
                           observe_dispatch),
              patch('rachis.sdk.context.parallel._submit_batch',
                    hold_score_batches),
              ParallelConfig(config), ParallelContext(None) as ctx):
            retained = ctx.get_action('dummy_plugin', 'filter_scores')(
                scores).retained
            output = ctx.get_action('dummy_plugin', 'add_offset')(
                retained, offsets).output
            try:
                assert filtering.wait(timeout=5)
                assert not dispatched
                release_filter.set()
                _, index, _ = output._coordinate_index.result(timeout=5)
                assert set(index) == {
                    Coord(student='s1', assignment='a1'),
                    Coord(student='s2', assignment='a1'),
                }
                cell = index[Coord(student='s1', assignment='a1')]
                cell = cell.future.result(timeout=5)
                with cell.open() as value:
                    assert value == '14'
                assert not retained._future_.done()
            finally:
                release_filter.set()
                producer_exit.set_result(None)

    assert [(key.student, key.assignment) for key in dispatched] == [
        ('s1', 'a1'), ('s2', 'a1'),
    ]

    assert sorted(observed, key=repr) == [('4', '10'), ('6', None)]
    assert output.view(dict) == {'s1/a1': '14', 's2/a1': '6'}
    action_dir = output._archiver.provenance_dir / 'action'
    records = action_dir / 'cell-coordinates.tsv'
    header, *rows = records.read_text().splitlines()
    assert header == 'student\tassignment\tattempt\toutcome\tstart\tend'
    assert [row.split('\t')[:4] for row in rows] == [
        ['s1', 'a1', '1', 'completed'],
        ['s2', 'a1', '1', 'completed'],
    ]


def test_parallel_root_cleans_up_mapped_work_after_pipeline_error(tmp_path):
    pipeline = get_dummy_plugin().pipelines['mapped_sample_pipeline']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    commits = []

    def fail_after_dispatch(ctx, data):
        ctx.get_action('dummy_plugin', 'filter_samples')(data)
        raise ValueError('pipeline failed after dispatch')

    def observe_commit(path, outcome):
        result = commit_attempt(path, outcome)
        assert path.is_file()
        commits.append(path)
        return result

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch.object(pipeline, '_callable', fail_after_dispatch),
          patch('rachis.sdk.mapped.commit_attempt', observe_commit),
          ParallelConfig(config)):
        with pytest.raises(ValueError, match='pipeline failed after dispatch'):
            pipeline.parallel(data)

        assert len(commits) == 2
        assert all(not path.parent.exists() for path in commits)


def test_range_alignment_excludes_failed_cells_before_dispatch(tmp_path):
    observed = []

    def align_samples(
        data: Annotated[str, ForEach['sample']],
        roster: Annotated[str, ForEach['sample']],
    ) -> Annotated[str, ForEach['sample']]:
        observed.append((data, roster))
        return data

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    plugin = get_dummy_plugin()
    with patch.dict(plugin.methods), ParallelConfig(config):
        plugin.methods.register_function(
            align_samples, inputs={'data': Mapping, 'roster': Mapping},
            parameters={}, outputs={'output': Mapping}, name='Align samples',
            description='',
        )
        data = Artifact.import_data(
            'Mapping', {'s1': '4', 's2': '-5', 's3': 'invalid'})
        roster = Artifact.import_data('Mapping', {'s1': 'yes', 's2': 'yes'})
        failure = 'Mapped cell.*s3.*failed'
        with (pytest.raises(RuntimeError, match=failure),
              ParallelContext(None) as ctx):
            filter_samples = ctx.get_action('dummy_plugin', 'filter_samples')
            producer = filter_samples(data).retained
            align = ctx.get_action('dummy_plugin', 'align_samples')
            consumer = align(producer, roster).output
            _, index, _ = consumer._coordinate_index.result(timeout=5)
            assert tuple(index) == (Coord(sample='s1'),)
            cell = index[Coord(sample='s1')].future.result(timeout=5)
            with cell.open() as value:
                assert value == '4'

            with pytest.raises(RuntimeError, match=failure):
                consumer._future_.result(timeout=5)
            increment = ctx.get_action('dummy_plugin', 'increment_samples')
            final = increment(consumer, 1).output
            _, index, _ = final._coordinate_index.result(timeout=5)
            assert tuple(index) == (Coord(sample='s1'),)
            cell = index[Coord(sample='s1')].future.result(timeout=5)
            with cell.open() as value:
                assert value == '5'

        for output in (producer, consumer, final):
            with pytest.raises(RuntimeError, match=failure):
                output.result()
    assert observed == [('4', 'yes')]


def test_partial_batch_submission_drains_worker_before_workspace_cleanup(
    tmp_path,
):
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    cleanup_started = Event()
    submitted = []
    completed = []
    inner_apply = MappedMethod._inner_apply

    def fail_second_submission(cells, executor):
        if submitted:
            raise ValueError('second batch failed to submit')
        cells = tuple(cells)
        submitted.append(cells[0].path)
        return _submit_batch(cells, executor)

    def finish_after_cleanup_starts(self, frame):
        outcome = inner_apply(self, frame)
        assert cleanup_started.wait(timeout=5)
        assert frame.path.is_file()
        completed.append(frame.path)
        return outcome

    def drain_batches(jobs):
        cleanup_started.set()
        wait(jobs)

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch('rachis.sdk.context.parallel._submit_batch',
                fail_second_submission),
          patch('rachis.sdk.context.parallel.wait', drain_batches),
          patch.object(MappedMethod, '_inner_apply',
                       finish_after_cleanup_starts),
          ParallelConfig(config)):
        with (pytest.raises(ValueError, match='second batch failed to submit'),
              ParallelContext(None) as ctx):
            ctx.get_action('dummy_plugin', 'increment_samples')(data, 1)

    assert completed == submitted
    path, = submitted
    assert not path.parent.exists()


def test_parallel_proxy_surfaces_mapped_finalization_error(tmp_path):
    data = Artifact.import_data('Mapping', {'s1': 'invalid'})
    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with ParallelConfig(config):
        with (pytest.raises(RuntimeError, match='Mapped cell .* failed'),
              ParallelContext(None) as ctx):
            action = ctx.get_action('dummy_plugin', 'filter_samples')
            result = action(data)
        with pytest.raises(RuntimeError, match='Mapped cell .* failed'):
            result.retained.result()


def test_late_batch_failure_invalidates_artifacts_after_cell_commit(tmp_path):
    data = Artifact.import_data('Mapping', {'s1': '4'})
    consumer_committed = Event()
    batch_exit = Future()
    inner_apply = MappedMethod._inner_apply

    def hold_producer_batch_open(cells, executor):
        cells = tuple(cells)
        job = _submit_batch(cells, executor)
        if cells[0].action.id == 'filter_samples':
            return after_all((job, batch_exit), lambda _: None)
        return job

    def observe_consumer(self, frame):
        outcome = inner_apply(self, frame)
        if self.id == 'increment_samples':
            consumer_committed.set()
        return outcome

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch('rachis.sdk.context.parallel._submit_batch',
                hold_producer_batch_open),
          patch.object(MappedMethod, '_inner_apply', observe_consumer),
          ParallelConfig(config)):
        with (pytest.raises(RuntimeError, match='producer batch failed'),
              ParallelContext(None) as ctx):
            producer = ctx.get_action('dummy_plugin', 'filter_samples')(
                data).all_samples
            consumer = ctx.get_action('dummy_plugin', 'increment_samples')(
                producer, 1).output
            try:
                assert consumer_committed.wait(timeout=5)
            finally:
                batch_exit.set_exception(RuntimeError('producer batch failed'))

        for output in (producer, consumer):
            with pytest.raises(RuntimeError, match='producer batch failed'):
                output.result()


def test_batch_continues_after_a_cell_fails(tmp_path):
    data = Artifact.import_data('Mapping', {'s1': 'invalid', 's2': '4'})
    outcomes = {}

    def observe_commit(path, outcome):
        result = commit_attempt(path, outcome)
        outcomes[path.name] = read_commit(path)
        return result

    def one_batch(ctx, cells):
        yield tuple(cells)

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch.object(ParallelContext, '_batches', one_batch),
          patch('rachis.sdk.mapped.commit_attempt', observe_commit),
          ParallelConfig(config)):
        with (pytest.raises(RuntimeError, match='Mapped cell.*s1.*failed'),
              ParallelContext(None) as ctx):
            ctx.get_action('dummy_plugin', 'filter_samples')(data)
    assert isinstance(outcomes['0.cell'], Exception)
    assert isinstance(outcomes['1.cell'], Path)


@pytest.mark.parametrize('value', ['4', '-5', 'invalid'])
def test_cell_attempts_are_reused_without_evaluation_or_publication(value):
    method = get_dummy_plugin().methods['filter_samples']
    data = Artifact.import_data('Mapping', {'s1': value})

    class RepeatingContext(Context):
        def _dispatch_cells_(self, cells, *, cleanup):
            futures = {}
            for coord, cell in cells.items():
                retry = copy(cell)
                first = futures[coord] = as_future(cell)
                committed = cell.path.read_bytes()
                if first.exception() is None:
                    trace = (first.result() / 'transformers').read_bytes()
                # A transported retry can recover its attempt without inputs.
                retry.bound_arguments = {}
                retry = dill.loads(dill.dumps(retry))
                repeated = as_future(retry)
                assert cell.path.read_bytes() == committed
                if first.exception() is not None:
                    assert str(repeated.exception()) == str(first.exception())
                else:
                    assert repeated.result() == first.result()
                    trace_path = repeated.result() / 'transformers'
                    assert trace_path.read_bytes() == trace
            return futures, ()

    with patch('rachis.sdk.mapped.commit_attempt',
               wraps=commit_attempt) as publish:
        call = method._outer_bind(lambda: RepeatingContext(method))
        if value == 'invalid':
            with pytest.raises(RuntimeError, match='Mapped cell.*s1.*failed'):
                call(data)
        else:
            result = call(data)
            assert result.retained.view(dict) == (
                {'s1': value} if value == '4' else {})
            assert result.all_samples.view(dict) == {'s1': value}
        assert publish.call_count == 1


def test_cell_records_failure_after_evaluation_wrappers_unwind():
    method = get_dummy_plugin().methods['increment_samples']
    data = Artifact.import_data('Mapping', {'s1': '4'})
    events = []
    workspaces = []

    @contextmanager
    def fail_on_exit(frame):
        yield
        events.append('callable returned')
        workspaces.append(frame.workspace)
        raise ValueError('failed during unwind')

    class ObservedContext(Context):
        inner_apply_wrappers = (fail_on_exit,)

    def observe_commit(path, outcome):
        assert isinstance(outcome, RuntimeError)
        assert isinstance(outcome.__cause__, ValueError)
        workspace, = workspaces
        assert not workspace.exists()
        events.append('published failure')
        return commit_attempt(path, outcome)

    with patch('rachis.sdk.mapped.commit_attempt', observe_commit):
        with pytest.raises(RuntimeError, match='failed during unwind'):
            method._outer_bind(lambda: ObservedContext(method))(data, 1)
    assert events == ['callable returned', 'published failure']


def test_filtered_output_records_a_completed_call():
    def filter_every_sample(
        data: Annotated[str, ForEach['sample']],
    ) -> Annotated[str | None, ForEach['sample']]:
        return None

    plugin = get_dummy_plugin()
    data = Artifact.import_data('Mapping', {'s1': '4'})
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            filter_every_sample, inputs={'data': Mapping}, parameters={},
            outputs={'output': Mapping}, name='Filter samples', description='',
        )
        result = plugin.methods['filter_every_sample'](data).output
    assert result.view(dict) == {}
    action_dir = result._archiver.provenance_dir / 'action'
    records = action_dir / 'cell-coordinates.tsv'
    header, row = records.read_text().splitlines()
    assert header == 'sample\tattempt\toutcome\tstart\tend'
    assert row.split('\t')[:3] == ['s1', '1', 'filtered']
