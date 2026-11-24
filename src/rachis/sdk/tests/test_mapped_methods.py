# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from contextlib import contextmanager
from concurrent.futures import Future
from threading import Event, current_thread
from typing import Annotated
from unittest.mock import patch

import pytest
import dill

from rachis import Artifact, ForEach, Metadata
from rachis.core.archive import Archiver, ImportProvenanceCapture
from rachis.core.testing.format import OldMappingDirFmt
from rachis.core.testing.type import AssignmentScores, Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.core.util import load_action_yaml
from rachis.plugin import CaptureHolder, Int
from rachis.plugin import Metadata as MetadataType
from rachis.plugin.plugin import TransformerRecord
from rachis.sdk import MappedMethod, PluginManager, Results
from rachis.sdk.context import Context, ParallelContext
from rachis.sdk.futures import as_future
from rachis.sdk.parallel_config import ParallelConfig
from rachis.sdk.proxy import ProxyArtifact


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
    retained, all_samples = method(data)
    assert retained.view(dict) == {'s1': '4'}
    assert all_samples.view(dict) == {'s1': '4', 's2': '-5'}
    action_dir = retained._archiver.provenance_dir / 'action'
    records = action_dir / 'cell-coordinates.tsv'
    assert records.read_text().splitlines() == [
        'sample\tattempt\toutcome', 's1\t1\tcompleted', 's2\t1\tfiltered',
    ]


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
    assert (action_dir / records).read_text().splitlines() == [
        'sample\tattempt\toutcome', 's1\t1\tcompleted',
    ]


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


def test_cell_context_exits_before_staging_outputs():
    plugin = get_dummy_plugin()
    method = plugin.methods['increment_samples']
    observed = []
    events = []

    @contextmanager
    def observe(frame):
        yield
        assert not (frame.workspace / 'outputs').exists()
        observed.append(frame.results)
        events.append(frame.coordinate.sample)

    @contextmanager
    def observe_invocation(frame):
        events.append('mapped-enter')
        yield
        assert all(coord is cell.coordinate
                   for coord, cell in frame.cells.items())
        assert len(frame.cell_futures) == 2
        assert all(future.done() for future in frame.cell_futures.values())
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

        def _dispatch_cells_(self, cells):
            futures = {}
            for coord, cell in cells.items():
                future = futures[coord] = Future()
                pending.append((cell, future))
            return futures

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
        def _dispatch_cells_(self, cells):
            return {
                coord: as_future(dill.loads(dill.dumps(cell)))
                for coord, cell in cells.items()
            }

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
