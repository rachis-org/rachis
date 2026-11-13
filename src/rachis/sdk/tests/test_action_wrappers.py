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

from rachis.core.cache import Cache
from rachis.core.testing.type import IntSequence1, IntSequence2
from rachis.core.testing.util import get_dummy_plugin
from rachis.sdk import Artifact, Results
from rachis.sdk.context import Context, ParallelContext
from rachis.sdk.proxy import ProxyArtifact


class TestActionWrappers(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()

    def test_context_wrappers_enclose_output_publication(self):
        events = []

        @contextmanager
        def outer(frame):
            self.assertEqual(frame.output_types,
                             frame.action.signature.outputs)
            events.append('outer-enter')
            yield
            self.assertIsInstance(frame.results, Results)
            self.assertIsInstance(frame.results.out, Artifact)
            events.append('outer-exit')

        @contextmanager
        def inner(frame):
            self.assertIsNone(frame.provenance)
            events.append('inner-enter')
            yield
            self.assertIsInstance(frame.results, Results)
            self.assertIsInstance(frame.results.out, Artifact)
            events.append('inner-exit')

        class ObservedContext(Context):
            outer_bind_wrappers = (outer,)
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
            outer_bind_wrappers = (replace_results,)

        action = self.plugin.methods['no_input_method']
        call = action._outer_bind(lambda: ReplacingContext(action))
        self.assertIs(call(), replacement)

    def test_all_outer_wrappers_can_observe_a_cache_hit(self):
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
                first = action._outer_bind(lambda: miss)()
                self.assertTrue(miss.dispatched)

                hit = ObservedContext(action, Context(), expect_dispatch=False)
                with patch.object(action, 'outer_bind_wrappers', (observe,)):
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
            events.append('inner-enter')
            yield
            events.append('inner-exit')

        class DeferredContext(Context):
            outer_bind_wrappers = (outer,)
            inner_apply_wrappers = (inner,)
            _defer_results = True

            def _dispatch_action_(self, frame):
                future = Future()
                pending.append((frame.for_worker(), future))
                return future

        action = self.plugin.methods['split_ints']
        artifact = Artifact.import_data(IntSequence1, [1, 2, 3])
        call = action._outer_bind(lambda: DeferredContext(action))
        results = call(artifact)
        frame, dispatched = pending.pop()

        self.assertIs(results._future_, dispatched)
        self.assertFalse(dispatched.done())
        self.assertIsNone(frame.provenance)
        self.assertIs(frame.bound_arguments['ints'], artifact)
        self.assertFalse(frame.call_kwargs)
        self.assertEqual(events, ['outer-enter', 'outer-exit'])

        dispatched.set_result(frame())
        self.assertEqual(results.left.view(list), [1])
        self.assertEqual(events, [
            'outer-enter', 'outer-exit', 'inner-enter', 'inner-exit'])

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


if __name__ == '__main__':
    unittest.main()
