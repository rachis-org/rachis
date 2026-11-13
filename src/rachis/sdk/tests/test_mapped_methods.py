# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from contextlib import contextmanager
from tempfile import TemporaryDirectory
from threading import Event
from typing import Annotated
from unittest.mock import patch

from parsl import Config
from parsl.executors import ThreadPoolExecutor
from parsl.serialize import pack_apply_message, unpack_apply_message
import pytest

from rachis import Artifact, ForEach
from rachis.core.testing.type import Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.plugin import CaptureHolder, Int
from rachis.sdk import MappedMethod
from rachis.sdk.context import Context, ParallelContext
from rachis.sdk.mapped import MappedProxyResults
from rachis.sdk.parallel_config import ParallelConfig


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
        inner_apply_wrappers = (observe_inner,)
        outer_apply_wrappers = (
            observe_invocation, *Context.outer_apply_wrappers)
        mapped_inner_apply_wrappers = (observe,)

    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    call = method._outer_bind(lambda: ObservedContext(method))
    assert call(data, 1).output.view(dict) == {'s1': '5', 's2': '6'}
    assert observed == ['5', '6']
    assert events == ['mapped-enter', 's1', 's2', 'mapped-exit']


def test_parsl_resolves_a_coordinate_through_the_dag_before_batch_exit():
    plugin = get_dummy_plugin()
    pipeline = plugin.pipelines['mapped_sample_pipeline']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    downstream_committed = Event()
    order = []
    invocations = []
    inner_apply = MappedMethod._inner_apply

    @contextmanager
    def observe_invocation(frame):
        yield
        proxy = frame.results
        assert isinstance(proxy, MappedProxyResults)
        invocations.append(frame.action.id)
        if (frame.action.id == 'increment_samples'
                and frame.bound_arguments['amount'] == 2):
            proxy.output._cells['s1',].future.add_done_callback(mark_committed)

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

    with TemporaryDirectory() as run_dir:
        config = Config(
            executors=[SerializingExecutor(label='default', max_threads=3)],
            run_dir=run_dir,
        )
        with (patch.object(ParallelContext, '_batches', batches),
              patch.object(ParallelContext, 'outer_apply_wrappers',
                           (observe_invocation,
                            *ParallelContext.outer_apply_wrappers)),
              patch.object(MappedMethod, '_inner_apply', observe_cell),
              ParallelConfig(config)):
            result = pipeline.parallel(data).output

    assert order == ['third-stage s1', 'producer s2']
    assert invocations == [
        'filter_samples', 'increment_samples', 'increment_samples']
    assert result.view(dict) == {'s1': '7'}


def test_parallel_root_cleans_up_mapped_work_after_pipeline_error(tmp_path):
    pipeline = get_dummy_plugin().pipelines['mapped_sample_pipeline']
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '5'})
    run_batch = MappedMethod._run_batch
    commits = []

    def fail_after_dispatch(ctx, data):
        ctx.get_action('dummy_plugin', 'filter_samples')(data)
        raise ValueError('pipeline failed after dispatch')

    def observe_batch(self, frame):
        run_batch(self, frame)
        for cell in frame.cells:
            assert cell.cell.path.is_file()
            commits.append(cell.cell.path)

    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with (patch.object(pipeline, '_callable', fail_after_dispatch),
          patch.object(MappedMethod, '_run_batch', observe_batch),
          ParallelConfig(config)):
        with pytest.raises(ValueError, match='pipeline failed after dispatch'):
            pipeline.parallel(data)

        assert len(commits) == 2
        assert all(not path.parent.exists() for path in commits)


def test_parallel_proxy_surfaces_mapped_finalization_error(tmp_path):
    data = Artifact.import_data('Mapping', {'s1': 'invalid'})
    config = Config(
        executors=[ThreadPoolExecutor(label='default', max_threads=2)],
        run_dir=str(tmp_path),
    )
    with ParallelConfig(config), ParallelContext(None) as ctx:
        action = ctx.get_action('dummy_plugin', 'filter_samples')
        result = action(data)
        with pytest.raises(RuntimeError, match='Mapped cell .* failed'):
            result.retained.result()
