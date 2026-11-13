# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from contextlib import contextmanager
from typing import Annotated
from unittest.mock import patch

import pytest

from rachis import Artifact, ForEach
from rachis.core.testing.type import Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.plugin import CaptureHolder, Int
from rachis.sdk import MappedMethod
from rachis.sdk.context import Context


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


def test_cell_wrappers_stage_outputs_before_the_context_exits():
    plugin = get_dummy_plugin()
    method = plugin.methods['increment_samples']
    observed = []
    events = []

    @contextmanager
    def observe(frame):
        yield
        staged = frame.results.outputs['output']
        assert not frame.cell.path.exists()
        observed.append(staged.load())
        events.append(frame.coordinate.sample)

    @contextmanager
    def observe_invocation(frame):
        events.append('mapped-enter')
        yield
        assert frame.provenance is not None
        assert len(frame.outcomes) == 2
        events.append('mapped-exit')

    @contextmanager
    def observe_inner(frame):
        events.append('inner-enter')
        yield

    class ObservedContext(Context):
        inner_wrappers = (observe_inner,)
        mapped_outer_wrappers = (
            observe_invocation, *Context.mapped_outer_wrappers)
        cell_wrappers = (observe,)

    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    call = method._outer_bind(lambda: ObservedContext(method))
    assert call(data, 1).output.view(dict) == {'s1': '5', 's2': '6'}
    assert observed == ['5', '6']
    assert events == ['mapped-enter', 's1', 's2', 'mapped-exit']
