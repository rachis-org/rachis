# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future
from contextlib import contextmanager
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import dill

from rachis.core.cache import Cache
from rachis.core.testing.type import IntSequence1, IntSequence2, Mapping
from rachis.core.testing.type import SingleInt
from rachis.core.testing.util import get_dummy_plugin
from rachis.core.util import load_action_yaml
from rachis.plugin import Collection
from rachis.sdk import Artifact, ResultCollection, Results
from rachis.sdk.context import Context, ParallelContext
from rachis.sdk.futures import as_future
from rachis.sdk.parallel_config import ParallelConfig
from rachis.sdk.proxy import ProxyArtifact, ProxyResultCollection


class TestActionWrappers(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()

    def test_context_wrappers_enter_after_action_wrappers(self):
        events = []

        @contextmanager
        def outer(frame):
            self.assertEqual(frame.output_types,
                             frame.action.signature.outputs)
            self.assertIsNone(frame.transformed_arguments)
            events.append('outer-enter')
            yield
            self.assertEqual(frame.output_types,
                             frame.action.signature.outputs)
            self.assertIsInstance(frame.results, Results)
            self.assertIsInstance(frame.results.out, Artifact)
            events.append('outer-exit')

        @contextmanager
        def inner(frame):
            self.assertIsNotNone(frame.provenance)
            events.append('inner-enter')
            yield
            self.assertEqual(frame.results, {'foo': 42})
            events.append('inner-exit')

        class ObservedContext(Context):
            outer_bind_wrappers = (outer, *Context.outer_bind_wrappers)
            inner_apply_wrappers = (inner,)

        action = self.plugin.methods['no_input_method']
        call = action._outer_bind(lambda: ObservedContext(action))
        results = call()

        self.assertIsInstance(results, Results)
        self.assertEqual(events, [
            'outer-enter', 'inner-enter', 'inner-exit', 'outer-exit'])

    def test_outer_exit_can_replace_returned_results(self):
        replacement = object()

        @contextmanager
        def replace_results(frame):
            yield
            self.assertIsInstance(frame.results, Results)
            frame.results = replacement

        class ReplacingContext(Context):
            outer_bind_wrappers = (replace_results,
                                   *Context.outer_bind_wrappers)

        action = self.plugin.methods['no_input_method']
        call = action._outer_bind(lambda: ReplacingContext(action))
        self.assertIs(call(), replacement)

    def test_all_outer_bind_wrappers_can_observe_a_cache_hit(self):
        observed = []

        @contextmanager
        def observe(frame):
            yield
            observed.append(frame.results)

        class ObservedContext(Context):
            outer_bind_wrappers = (observe, *Context.outer_bind_wrappers)

            def __init__(self, action, parent, *, expect_dispatch):
                super().__init__(action, parent=parent)
                self.expect_dispatch = expect_dispatch
                self.dispatched = False

            def _dispatch_action_(self, frame):
                assert self.expect_dispatch, 'cache hit dispatched the action'
                self.dispatched = True
                return super()._dispatch_action_(frame)

        with TemporaryDirectory() as directory:
            cache = Cache(os.path.join(directory, 'cache'))
            with cache.create_pool('recycle'):
                action = self.plugin.methods['no_input_method']
                miss = ObservedContext(action, Context(), expect_dispatch=True)
                normalize_arguments = action.signature.normalize_arguments
                with patch.object(
                        action.signature, 'normalize_arguments',
                        wraps=normalize_arguments) as normalize:
                    first = action._outer_bind(lambda: miss)()
                normalize.assert_called_once_with()
                self.assertTrue(miss.dispatched)

                hit = ObservedContext(action, Context(), expect_dispatch=False)
                with patch.object(
                        action, 'outer_bind_wrappers',
                        (*action.outer_bind_wrappers, observe)):
                    second = action._outer_bind(lambda: hit)()

                self.assertEqual(first.out.uuid, second.out.uuid)
                self.assertEqual(observed, [first, second, second])

    def test_invalid_input_raises_with_a_named_pool(self):
        with TemporaryDirectory() as directory:
            cache = Cache(os.path.join(directory, 'cache'))
            with cache.create_pool('recycle'):
                call = Context().get_action('dummy_plugin', 'split_ints')
                with self.assertRaisesRegex(TypeError, "Parameter 'ints'"):
                    call('invalid input')

    def test_deferred_dispatch_does_not_transform_in_caller(self):
        events, pending = [], []

        @contextmanager
        def outer(frame):
            events.append('outer-enter')
            yield
            events.append('outer-exit')

        @contextmanager
        def inner(frame):
            self.assertFalse(frame.provenance.inputs)
            events.append('inner-enter')
            yield
            self.assertFalse(frame.provenance.inputs)
            events.append('inner-exit')

        @contextmanager
        def require_prepared_arguments(frame):
            self.assertEqual(frame.output_types['left'].qiime_type,
                             IntSequence1)
            self.assertIsNone(frame.transformed_arguments)
            yield

        class DeferredContext(ParallelContext):
            outer_bind_wrappers = (outer, *ParallelContext.outer_bind_wrappers)
            inner_apply_wrappers = (inner,)

            def _dispatch_action_(self, frame):
                future = Future()
                pending.append((frame.for_worker(), future))
                return future

        action = self.plugin.methods['split_ints']
        artifact = Artifact.import_data(IntSequence1, [1, 2, 3])
        call = action._outer_bind(lambda: DeferredContext(action))
        with self.assertRaisesRegex(TypeError, "Parameter 'ints'"):
            call(ProxyArtifact(Future(), None, Mapping))
        self.assertEqual(pending, [])

        proxy = ProxyArtifact(Future(), None, IntSequence1)
        solve_output = action.signature.solve_output
        for argument in (artifact, proxy):
            with (
                self.subTest(argument=type(argument).__name__),
                patch.object(action.signature, 'solve_output',
                             wraps=solve_output) as solve,
            ):
                events.clear()
                results = call(argument)
                frame, dispatched = pending.pop()

                self.assertIs(results._future_, dispatched)
                self.assertFalse(dispatched.done())
                self.assertIsNone(frame.provenance)
                self.assertIs(frame.bound_arguments['ints'], argument)
                self.assertIsNone(frame.transformed_arguments)
                self.assertEqual(frame.output_types['left'].qiime_type,
                                 IntSequence1)
                self.assertEqual(results.left.type, IntSequence1)
                self.assertEqual(events, ['outer-enter', 'outer-exit'])

                frame.bound_arguments = {'ints': artifact}
                with patch.object(action, 'inner_apply_wrappers', (
                    require_prepared_arguments, *action.inner_apply_wrappers,
                )):
                    dispatched.set_result(frame())
                solve.assert_called_once_with(ints=argument)
                self.assertEqual(frame.provenance.inputs['ints'],
                                 str(artifact.uuid))
                self.assertEqual(results.left.view(list), [1])
                with self.assertRaisesRegex(RuntimeError, 'finished frame'):
                    frame()
                self.assertEqual(events, [
                    'outer-enter', 'outer-exit', 'inner-enter', 'inner-exit'])

    def test_pending_collection_binds_before_callable_shape_is_available(self):
        class PendingCollection(ProxyResultCollection):
            def result(self):
                raise AssertionError('Binding tried to resolve the collection')

        future = Future()
        collection = ResultCollection({
            'named': Artifact.import_data(SingleInt, 1),
        })
        proxy = PendingCollection(future, None, Collection[SingleInt])

        with ParallelConfig(), ParallelContext(None) as ctx:
            as_list = ctx.get_action('dummy_plugin', 'list_of_ints')
            as_collection = ctx.get_action('dummy_plugin', 'dict_of_ints')
            try:
                list_output = as_list(proxy).output
                collection_output = as_collection(proxy).output
            finally:
                future.set_result(collection)

        self.assertEqual(list(list_output.keys()), ['0'])
        self.assertEqual(list(collection_output.keys()), ['named'])

    def test_bound_list_input_uses_result_collection_and_recycles(self):
        bound = []

        @contextmanager
        def observe(frame):
            bound.append(frame.bound_arguments['ints'])
            yield

        class ObservedContext(Context):
            outer_bind_wrappers = (observe, *Context.outer_bind_wrappers)

        action = self.plugin.methods['list_of_ints']
        artifact = Artifact.import_data(SingleInt, 1)

        def context_factory():
            return ObservedContext(action, parent=Context())

        call = action._outer_bind(context_factory)
        with TemporaryDirectory() as directory:
            cache = Cache(os.path.join(directory, 'cache'))
            with cache.create_pool('recycle'):
                first = call([artifact]).output['0']
                second = call({'named': artifact}).output['0']

        self.assertEqual(first.uuid, second.uuid)
        self.assertEqual(len(bound), 2)
        for arguments in bound:
            self.assertIsInstance(arguments, ResultCollection)
            self.assertEqual(list(arguments.keys()), ['0'])

    def test_pipeline_aliases_named_outputs_independently(self):
        pipeline = self.plugin.pipelines['parameter_only_pipeline']
        pending = Future()

        def return_outputs(ctx, **kwargs):
            first = ctx.make_artifact(IntSequence2, [1])
            second = ProxyArtifact(pending, None, IntSequence1)
            return first, second

        with ParallelContext(pipeline) as ctx:
            call = pipeline._outer_bind(lambda: ctx)
            try:
                with patch.object(pipeline, '_callable', return_outputs):
                    results = call(1)
                self.assertEqual(results[0]._future_.result(
                    timeout=5).view(list), [1])
                self.assertFalse(results[1]._future_.done())
            finally:
                pending.set_result(Artifact.import_data(IntSequence1, [2]))
            self.assertEqual(results[1].view(list), [2])

    def test_pipeline_alias_waits_for_original_inputs(self):
        pipeline = self.plugin.pipelines['list_pipeline']
        artifact = Artifact.import_data(IntSequence1, [1])
        pending = Future()
        proxy = ProxyArtifact(pending, None, IntSequence1)

        def return_outputs(ctx, ints):
            ints.clear()
            return [ctx.make_artifact(SingleInt, 2)]

        with ParallelContext(pipeline) as ctx:
            call = pipeline._outer_bind(lambda: ctx)
            try:
                with patch.object(pipeline, '_callable', return_outputs):
                    output = call([proxy]).output
                self.assertFalse(output._future_.done())
            finally:
                pending.set_result(artifact)

        alias = output.result()['0']
        provenance = load_action_yaml(alias._archiver.path)
        self.assertEqual(provenance['action']['inputs'],
                         [{'ints': [str(artifact.uuid)]}])

    def test_context_raises_pipeline_output_failure_at_scope_exit(self):
        sequence = Artifact.import_data(IntSequence1, [1])
        events = []

        with ParallelConfig():
            with self.assertRaisesRegex(ValueError, "Key 'foo' exists"):
                with ParallelContext(None) as ctx:
                    fail = ctx.get_action('dummy_plugin', 'failing_pipeline')
                    fail(sequence, break_from='method')
                    events.append('body completed')

        self.assertEqual(events, ['body completed'])

    def test_parallel_raises_failure_from_discarded_nested_call(self):
        pipeline = self.plugin.pipelines['parameter_only_pipeline']
        nested = self.plugin.pipelines['failing_pipeline']
        sequence = Artifact.import_data(IntSequence1, [1])

        def discard_failed_method(ctx, **kwargs):
            merge = ctx.get_action('dummy_plugin', 'merge_mappings')
            first = ctx.make_artifact(Mapping, {'foo': 'a'})
            second = ctx.make_artifact(Mapping, {'foo': 'b'})
            merge(first, second)
            return first

        def discard_nested_pipeline(ctx, **kwargs):
            call = ctx.get_action('dummy_plugin', 'failing_pipeline')
            call(sequence)
            return ctx.make_artifact(IntSequence2, [1]), sequence

        with (
            patch.object(nested, '_callable', discard_failed_method),
            patch.object(pipeline, '_callable', discard_nested_pipeline),
            ParallelConfig(),
        ):
            with self.assertRaisesRegex(ValueError, "Key 'foo' exists"):
                pipeline.parallel(1)

    def test_parallel_raises_output_alias_failure_before_return(self):
        pipeline = self.plugin.pipelines['parameter_only_pipeline']

        with (
            patch.object(Artifact, '_alias', side_effect=ValueError('alias')),
            ParallelConfig(),
        ):
            with self.assertRaisesRegex(ValueError, '^alias$'):
                pipeline.parallel(1)

    def test_nested_context_entries_report_their_own_results(self):
        first = Artifact.import_data(Mapping, {'outer': 'a', 'inner': 'a'})
        outer = Artifact.import_data(Mapping, {'outer': 'b'})
        inner = Artifact.import_data(Mapping, {'inner': 'b'})
        events = []

        with ParallelConfig(), ParallelContext(None) as ctx:
            merge = ctx.get_action('dummy_plugin', 'merge_mappings')
            with self.assertRaisesRegex(ValueError, "Key 'outer' exists"), ctx:
                merge(first, outer)
                with (
                    self.assertRaisesRegex(ValueError, "Key 'inner' exists"),
                    ctx,
                ):
                    merge(first, inner)
                    events.append('inner body completed')
                events.append('outer body completed')
            merged = merge(outer, inner).merged_mapping

        self.assertEqual(events, [
            'inner body completed', 'outer body completed'])
        self.assertEqual(merged.view(dict), {'outer': 'b', 'inner': 'b'})

    def test_visualizer_directory_is_cleaned_when_callable_fails(self):
        directories = []

        def fail(output_dir):
            directories.append(output_dir)
            self.assertTrue(os.path.isdir(output_dir))
            raise ValueError('failed')

        action = self.plugin.visualizers['no_input_viz']
        with patch.object(action, '_callable', fail):
            with self.assertRaisesRegex(ValueError, 'failed'):
                action()

        self.assertEqual(len(directories), 1)
        self.assertFalse(os.path.exists(directories[0]))

    def test_get_action_creates_a_context_for_each_application(self):
        contexts = []

        class ObservedContext(Context):
            def _dispatch_action_(self, frame):
                contexts.append(self)
                return super()._dispatch_action_(frame)

        call = ObservedContext().get_action('dummy_plugin', 'no_input_method')
        call()
        call()

        self.assertEqual(len(contexts), 2)
        self.assertIsNot(contexts[0], contexts[1])
        self.assertIsNot(contexts[0].execution_ctx, contexts[1].execution_ctx)

    def test_serialized_frame_resumes_application_without_rebinding(self):
        @contextmanager
        def bind_state(frame):
            frame.request_label = 'bound in the caller'
            yield

        @contextmanager
        def consume_state(frame):
            assert frame.request_label == 'bound in the caller'
            yield

        class SerializingContext(Context):
            outer_bind_wrappers = (bind_state, *Context.outer_bind_wrappers)
            inner_apply_wrappers = (consume_state,)

            def _dispatch_action_(self, frame):
                worker = dill.loads(dill.dumps(frame.for_worker()))
                return as_future(worker)

        action = self.plugin.methods['split_ints']
        artifact = Artifact.import_data(IntSequence1, [1, 2, 3])
        call = action._outer_bind(lambda: SerializingContext(action))
        results = call(artifact)
        self.assertEqual(results.left.view(list), [1])
        self.assertEqual(results.right.view(list), [2, 3])


if __name__ == '__main__':
    unittest.main()
