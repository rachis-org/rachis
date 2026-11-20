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
from tempfile import TemporaryDirectory
from threading import Event, current_thread
from typing import Annotated
from unittest.mock import patch

from parsl import Config
from parsl.executors import ThreadPoolExecutor
from parsl.serialize import pack_apply_message, unpack_apply_message
import pytest
import dill

from rachis import Artifact, Coord, ForEach
from rachis.core.testing.type import AssignmentScores, Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.plugin import CaptureHolder, Int
from rachis.sdk import MappedMethod, Results
from rachis.sdk.context import Context, ParallelContext
from rachis.sdk.context.parallel import _submit_batch
from rachis.sdk.futures import after_all, as_future
from rachis.sdk.mapped import (
    CellOutcome, CommittedCell, MappedProxyArtifact, commit_outcome,
    coordinate_source,
    read_outcome,
)
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
    ) -> Annotated[str, ForEach['sample'::'matched']]:
        return data

    plugin = get_dummy_plugin()
    with patch.dict(plugin.methods):
        plugin.methods.register_function(
            copy_samples, inputs={'data': Mapping}, parameters={},
            outputs={'output': Mapping}, name='Copy samples', description='',
        )
        method = plugin.methods['copy_samples']
        assert method.signature.input_patterns['data'] == (
            method.signature.output_patterns['output']
        )
        data = Artifact.import_data('Mapping', {'s1': '4'})
        assert method(data).output.view(dict) == {'s1': '4'}


def test_per_output_filtering():
    plugin = get_dummy_plugin()
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    method = plugin.methods['filter_samples']
    assert isinstance(method, MappedMethod)
    retained, all_samples = method(data)
    assert retained.view(dict) == {'s1': '4'}
    assert all_samples.view(dict) == {'s1': '4', 's2': '-5'}
    records = retained._archiver.provenance_dir / 'action' / 'coordinates.tsv'
    assert records.read_text().splitlines() == [
        'sample\tattempt\toutcome', 's1\t1\tcompleted', 's2\t1\tfiltered',
    ]


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


def test_cell_context_exits_before_staging_outputs():
    plugin = get_dummy_plugin()
    method = plugin.methods['increment_samples']
    observed = []
    events = []

    @contextmanager
    def observe(frame):
        yield
        assert not frame.cell.path.exists()
        observed.append(frame.results)
        events.append(frame.coordinate.sample)

    @contextmanager
    def observe_invocation(frame):
        events.append('mapped-enter')
        yield
        assert len(frame.results) == 2
        assert all(future.done() for future in frame.results.values())
        events.append('mapped-exit')

    class ObservedContext(Context):
        inner_apply_wrappers = (observe,)

    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    call = method._outer_bind(lambda: ObservedContext(method))
    with patch.object(method, 'inner_bind_wrappers',
                      (*method.inner_bind_wrappers, observe_invocation)):
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
                directory = pending[0][0].cell.path.parent
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
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
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
        return ctx.get_action('dummy_plugin', 'filter_samples')(data).retained

    def increment_retained(ctx, data):
        retained, = ctx.get_action('dummy_plugin', 'retain_samples')(data)
        increment = ctx.get_action('dummy_plugin', 'increment_samples')
        intermediate, = increment(retained, 1)
        return increment(intermediate, 2).output

    @contextmanager
    def observe_invocation(frame):
        yield
        results = frame.results
        assert isinstance(results, Results)
        for output in results:
            assert isinstance(output, MappedProxyArtifact)
        invocations.append(frame.action.id)
        if (frame.action.id == 'increment_samples'
                and frame.bound_arguments['amount'] == 2):
            ready = results.output._cells['s1',].future
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
        pipeline_body = (increment_retained if through_pipeline
                         else pipeline._callable)
        with (patch.object(pipeline, '_callable', pipeline_body),
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
    assert result.view(dict) == {'s1': '7'}


@pytest.mark.parametrize('coverage', ['matched', 'optional'])
def test_pending_subset_broadcasts_before_producer_batch_finishes(coverage):
    plugin = get_dummy_plugin()
    downstream_committed = Event()
    order = []
    inner_apply = MappedMethod._inner_apply

    def broadcast_offset(
        offset: Annotated[str | None, ForEach['sample':'student':coverage]],
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
            for assignment in ('a1', 'a2'):
                cell = frame.results.output._cells['s1', assignment]
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
    expected = {'s1/a1': '14', 's1/a2': '15'}
    if coverage == 'optional':
        expected['s2/a1'] = '6'
    assert result.view(dict) == expected


def test_pipeline_alias_preserves_pending_mapped_cells():
    plugin = get_dummy_plugin()
    pipeline = plugin.pipelines['mapped_sample_pipeline']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    consumer_committed = Event()
    inner_apply = MappedMethod._inner_apply

    def return_mapped(ctx, data):
        retained, _ = ctx.get_action('dummy_plugin', 'filter_samples')(data)
        return retained

    def batches(ctx, cells):
        if ctx.action_obj.id == 'filter_samples':
            yield tuple(cells)
        else:
            for coord in cells:
                yield (coord,)

    def observe(self, frame):
        if self.id == 'filter_samples' and frame.coordinate.sample == 's2':
            assert consumer_committed.wait(timeout=10)
        return inner_apply(self, frame)

    with TemporaryDirectory() as run_dir:
        config = Config(
            executors=[SerializingExecutor(label='default', max_threads=2)],
            run_dir=run_dir,
        )
        with (patch.object(pipeline, '_callable', return_mapped),
              patch.object(ParallelContext, '_batches', batches),
              patch.object(MappedMethod, '_inner_apply', observe),
              ParallelConfig(config), ParallelContext(None) as ctx):
            first = ctx.get_action('dummy_plugin', pipeline.id)
            increment = ctx.get_action('dummy_plugin', 'increment_samples')
            alias = first(data).output
            assert not alias._future_.done()
            result = increment(alias, 1).output
            try:
                plan = coordinate_source(result, ForEach['sample'])
                cell = plan.result(timeout=5)._cells['s1',].future
                assert cell.result(timeout=5).outputs['output'].load() == '5'
                assert not alias._future_.done()
            finally:
                consumer_committed.set()
            assert result.view(dict) == {'s1': '5', 's2': '6'}
            assert result._archiver.provenance_dir.joinpath(
                'artifacts', str(alias.uuid)).is_dir()


@pytest.mark.parametrize('through_pipeline', [False, True])
def test_parsl_resolves_nested_axis_prefixes_before_batch_exit(
    through_pipeline,
):
    plugin = get_dummy_plugin()
    pipeline = plugin.pipelines['mapped_scores_pipeline']
    data = Artifact.import_data('AssignmentScores', {
        's1/a1': '4', 's1/a2': '5', 's1/a3': '-1',
        's2/a1': '7', 's3/a1': '-4',
    })
    sample_planned = Event()
    sample_started = Event()
    sample_committed = Event()
    inner_apply = MappedMethod._inner_apply
    score_commits = {}
    order = []
    s1_assignments = {
        Coord(student='s1', assignment=assignment)
        for assignment in ('a1', 'a2', 'a3')
    }
    s2_assignment = Coord(student='s2', assignment='a1')

    def filter_scores_pipeline(ctx, data):
        return ctx.get_action('dummy_plugin', 'filter_scores')(data).retained

    def sum_aliased_scores(ctx, data):
        filtered, = ctx.get_action(
            'dummy_plugin', 'filter_scores_pipeline')(data)
        totals, = ctx.get_action('dummy_plugin', 'sum_scores')(filtered)
        increment = ctx.get_action('dummy_plugin', 'increment_samples')
        output, = increment(totals, 1)
        return filtered, output

    def batches(ctx, cells):
        if ctx.action_obj.id == 'filter_scores':
            score_commits.update((cell.outputs['retained'], cell.path)
                               for cell in (frame.cell
                                            for frame in cells.values()))
            yield tuple(cells)
            return
        if ctx.action_obj.id == 'sum_scores':
            s1 = next(frame for frame in cells.values()
                      if frame.coordinate.sample == 's1')
            planned = {coord for coord, _ in
                       s1.bound_arguments['data'].members}
            assert planned == s1_assignments
            sample_planned.set()
        for cell in cells:
            yield (cell,)

    def observe_cell(self, frame):
        coord = frame.coordinate
        if self.id == 'filter_scores':
            if coord == Coord(student='s1', assignment='a2'):
                # Force the sample to be planned while suffixes are pending.
                assert sample_planned.wait(timeout=10)
                assert not sample_started.is_set()
            if coord == s2_assignment:
                assert sample_committed.wait(timeout=10), (
                    'downstream s1 waited for another sample or assembly')
                order.append('producer s2')
        if self.id == 'sum_scores' and coord.sample == 's1':
            assert all(score_commits[cell].is_file()
                       for cell in s1_assignments)
            assert not score_commits[s2_assignment].exists()
            sample_started.set()
        return inner_apply(self, frame)

    @contextmanager
    def observe_invocation(frame):
        yield
        if frame.action.id == 'increment_samples':
            cell = frame.results.output._cells['s1',]
            cell.future.add_done_callback(mark_committed)

    def mark_committed(future):
        future.result()
        order.append('third-stage s1')
        sample_committed.set()

    with patch.dict(plugin.pipelines), TemporaryDirectory() as run_dir:
        plugin.pipelines.register_function(
            filter_scores_pipeline, inputs={'data': AssignmentScores},
            parameters={}, outputs={'filtered': AssignmentScores},
            name='Filter scores in a pipeline', description='',
        )
        config = Config(
            executors=[SerializingExecutor(label='default', max_threads=3)],
            run_dir=run_dir,
        )
        pipeline_body = (sum_aliased_scores if through_pipeline
                         else pipeline._callable)
        with (patch.object(pipeline, '_callable', pipeline_body),
              patch.object(ParallelContext, '_batches', batches),
              patch.object(MappedMethod, 'inner_bind_wrappers',
                           (observe_invocation,
                            *MappedMethod.inner_bind_wrappers)),
              patch.object(MappedMethod, '_inner_apply', observe_cell),
              ParallelConfig(config)):
            filtered, output = pipeline.parallel(data)
            assert all(not path.parent.exists()
                       for path in score_commits.values())

    assert order == ['third-stage s1', 'producer s2']
    assert set(filtered.coords('student')) == {
        Coord(student=coord.student)
        for coord in filtered.coords('student', 'assignment')
    }
    assert filtered.view(dict, student='s1') == {'a1': '4', 'a2': '5'}
    assert output.view(dict) == {'s1': '10', 's2': '8'}


@pytest.mark.parametrize('coverage', ['optional', 'required'])
def test_optional_broadcast_does_not_restore_filtered_composite_cells(
    coverage, tmp_path,
):
    retained_called = Event()
    observed = []

    def filter_scores(
        data: Annotated[str, ForEach['student', 'assignment']],
    ) -> Annotated[str | None, ForEach['student', 'assignment']]:
        if int(data) < 0:
            assert retained_called.wait(timeout=5)
            return None
        return data

    def add_offset(
        data: Annotated[str | None, ForEach[
            'student'::coverage, 'assignment'::coverage]],
        offset: Annotated[str | None, ForEach['sample':'student':'optional']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        observed.append((data, offset))
        if data is not None:
            retained_called.set()
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
        with ParallelConfig(config), ParallelContext(None) as ctx:
            retained = ctx.get_action('dummy_plugin', 'filter_scores')(
                scores).retained
            output = ctx.get_action('dummy_plugin', 'add_offset')(
                retained, offsets).output

    assert sorted(observed, key=repr) == [('4', '10'), ('6', None)]
    assert output.view(dict) == {'s1/a1': '14', 's2/a1': '6'}
    records = output._archiver.provenance_dir / 'action' / 'coordinates.tsv'
    assert records.read_text().splitlines() == [
        'student\tassignment\tattempt\toutcome',
        's1\ta1\t1\tcompleted',
        's2\ta1\t1\tcompleted',
    ]


def test_parallel_root_cleans_up_mapped_work_after_pipeline_error(tmp_path):
    pipeline = get_dummy_plugin().pipelines['mapped_sample_pipeline']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    commits = []

    def fail_after_dispatch(ctx, data):
        ctx.get_action('dummy_plugin', 'filter_samples')(data)
        raise ValueError('pipeline failed after dispatch')

    def observe_commit(frame, outcome):
        result = commit_outcome(frame, outcome)
        assert frame.cell.path.is_file()
        commits.append(frame.cell.path)
        return result

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch.object(pipeline, '_callable', fail_after_dispatch),
          patch('rachis.sdk.mapped.commit_outcome', observe_commit),
          ParallelConfig(config)):
        with pytest.raises(ValueError, match='pipeline failed after dispatch'):
            pipeline.parallel(data)

        assert len(commits) == 2
        assert all(not path.parent.exists() for path in commits)


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
        submitted.append(cells[0].cell.path)
        return _submit_batch(cells, executor)

    def finish_after_cleanup_starts(self, frame):
        outcome = inner_apply(self, frame)
        assert cleanup_started.wait(timeout=5)
        assert frame.cell.path.is_file()
        completed.append(frame.cell.path)
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
                data).retained
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

    def observe_commit(frame, outcome):
        result = commit_outcome(frame, outcome)
        outcomes[frame.coordinate.sample] = read_outcome(frame.cell.path)
        return result

    def one_batch(ctx, cells):
        yield tuple(cells)

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch.object(ParallelContext, '_batches', one_batch),
          patch('rachis.sdk.mapped.commit_outcome', observe_commit),
          ParallelConfig(config)):
        with (pytest.raises(RuntimeError, match='Mapped cell.*s1.*failed'),
              ParallelContext(None) as ctx):
            ctx.get_action('dummy_plugin', 'filter_samples')(data)
    assert isinstance(outcomes['s1'], Exception)
    assert isinstance(outcomes['s2'], CellOutcome)


@pytest.mark.parametrize('outcome', ['value', 'absent', 'error'])
def test_cell_outcomes_are_reused_without_evaluation_or_publication(outcome):
    method = get_dummy_plugin().methods['increment_samples']
    value = 'invalid' if outcome == 'error' else '4'
    data = Artifact.import_data('Mapping', {'s1': value})
    recorded = []

    class RepeatingContext(Context):
        def _dispatch_cells_(self, cells, *, cleanup):
            futures = {}
            for coord, cell in cells.items():
                if outcome == 'absent':
                    cell.bound_arguments['data'] = CommittedCell(None, 'data')
                retry = copy(cell)
                first = futures[coord] = as_future(cell)
                recorded.append(read_outcome(cell.cell.path))
                committed = cell.cell.path.read_bytes()
                # A transported retry needs only its commit to recover any of
                # the three outcomes. Binding must not revisit these inputs.
                retry.bound_arguments = {}
                retry = dill.loads(dill.dumps(retry))
                repeated = as_future(retry)
                assert cell.cell.path.read_bytes() == committed
                if first.exception() is not None:
                    assert type(repeated.exception()) is type(
                        first.exception())
                    assert str(repeated.exception()) == str(first.exception())
                elif outcome == 'absent':
                    assert repeated.result() is None
                else:
                    assert repeated.result().outputs['output'].load() == '5'
            return futures, ()

    with patch('rachis.sdk.mapped.commit_outcome',
               wraps=commit_outcome) as publish:
        call = method._outer_bind(lambda: RepeatingContext(method))
        if outcome == 'error':
            with pytest.raises(RuntimeError, match='Mapped cell.*s1.*failed'):
                call(data, 1)
            assert isinstance(recorded[0], RuntimeError)
        else:
            result = call(data, 1)
            expected = {} if outcome == 'absent' else {'s1': '5'}
            assert result.output.view(dict) == expected
            if outcome == 'absent':
                assert recorded == [None]
            else:
                assert isinstance(recorded[0], CellOutcome)
        assert publish.call_count == 1


def test_cell_records_failure_after_evaluation_wrappers_unwind():
    method = get_dummy_plugin().methods['increment_samples']
    data = Artifact.import_data('Mapping', {'s1': '4'})
    events = []

    @contextmanager
    def fail_on_exit(frame):
        yield
        events.append('callable returned')
        raise ValueError('failed during unwind')

    class ObservedContext(Context):
        inner_apply_wrappers = (fail_on_exit,)

    def observe_commit(frame, outcome):
        assert isinstance(outcome, RuntimeError)
        assert isinstance(outcome.__cause__, ValueError)
        assert not frame.workspace.exists()
        events.append('published failure')
        return commit_outcome(frame, outcome)

    with patch('rachis.sdk.mapped.commit_outcome', observe_commit):
        with pytest.raises(RuntimeError, match='failed during unwind'):
            method._outer_bind(lambda: ObservedContext(method))(data, 1)
    assert events == ['callable returned', 'published failure']


def test_filtered_output_is_a_value_outcome_not_an_absent_cell():
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
    records = result._archiver.provenance_dir / 'action' / 'coordinates.tsv'
    assert records.read_text().splitlines() == [
        'sample\tattempt\toutcome', 's1\t1\tfiltered',
    ]
