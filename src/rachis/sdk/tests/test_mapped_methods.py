# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from contextlib import contextmanager
from concurrent.futures import Future
from typing import Annotated
from unittest.mock import patch

import pytest
import dill

from rachis import Artifact, ForEach
from rachis.core.testing.type import AssignmentScores, Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.plugin import CaptureHolder, Int
from rachis.sdk import MappedMethod, Results
from rachis.sdk.context import Context
from rachis.sdk.futures import as_future


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
def test_subset_input_broadcasts_over_composite_coordinates(binding, coverage):
    observed = []

    def add_offset(
        offset: Annotated[str | None, ForEach['sample':binding:coverage]],
        data: Annotated[str, ForEach['student', 'assignment']],
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

    with DeferredContext(method) as ctx:
        result = method._outer_bind(lambda: ctx)(data, 1)
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
        def _dispatch_cells_(self, cells):
            return {
                coord: as_future(dill.loads(dill.dumps(cell)))
                for coord, cell in cells.items()
            }

    method = get_dummy_plugin().methods['increment_samples']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    call = method._outer_bind(lambda: SerializingContext(method))
    assert call(data, 1).output.view(dict) == {'s1': '5', 's2': '6'}


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
