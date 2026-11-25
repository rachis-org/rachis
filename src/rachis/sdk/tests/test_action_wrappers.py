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
from rachis.core.testing.type import IntSequence1, Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.sdk import Artifact, Results
from rachis.sdk.action import FramePhase
from rachis.sdk.context import Context
from rachis.sdk.proxy import ProxyArtifact


class PendingArtifact(ProxyArtifact):
    def result(self):
        raise AssertionError('Binding tried to resolve a pending artifact')


class TestActionWrappers(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()

    def test_context_wrappers_enter_after_action_wrappers(self):
        events = []

        @contextmanager
        def outer(frame):
            self.assertIs(frame.current_phase, FramePhase.OUTER_BIND)
            self.assertIs(frame.next_phase, FramePhase.INNER_APPLY)
            self.assertEqual(frame.output_types,
                             frame.action.signature.outputs)
            self.assertIsNone(frame.transformed_arguments)
            events.append('outer-enter')
            yield
            self.assertIs(frame.current_phase, FramePhase.OUTER_BIND)
            self.assertEqual(frame.output_types,
                             frame.action.signature.outputs)
            self.assertIsInstance(frame.results, Results)
            self.assertIsInstance(frame.results.out, Artifact)
            events.append('outer-exit')

        @contextmanager
        def inner(frame):
            self.assertIs(frame.current_phase, FramePhase.INNER_APPLY)
            self.assertIs(frame.next_phase, FramePhase.FINISHED)
            self.assertIsNotNone(frame.provenance)
            events.append('inner-enter')
            yield
            self.assertIs(frame.current_phase, FramePhase.INNER_APPLY)
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
            outer_bind_wrappers = (*Context.outer_bind_wrappers, observe)

        with TemporaryDirectory() as directory:
            cache = Cache(os.path.join(directory, 'cache'))
            with cache.create_pool('recycle'):
                action = self.plugin.methods['no_input_method']
                first = Context().get_action('dummy_plugin', action.id)()
                with (
                    patch.object(action, 'outer_bind_wrappers',
                                 (*action.outer_bind_wrappers, observe)),
                    patch.object(ObservedContext, '_dispatch_action_',
                                 side_effect=AssertionError(
                                     'Cache hit dispatched the action')),
                ):
                    second = ObservedContext().get_action(
                        'dummy_plugin', action.id)()

        self.assertEqual(first.out.uuid, second.out.uuid)
        self.assertEqual(observed, [second, second])

    def test_invalid_input_raises_with_a_named_pool(self):
        with TemporaryDirectory() as directory:
            cache = Cache(os.path.join(directory, 'cache'))
            with cache.create_pool('recycle'):
                call = Context().get_action('dummy_plugin', 'split_ints')
                wrong_type = PendingArtifact(Future(), None, Mapping)
                for value in ('invalid input', wrong_type):
                    with (
                        self.subTest(input_type=type(value).__name__),
                        self.assertRaisesRegex(TypeError, "Parameter 'ints'"),
                    ):
                        call(value)

    def test_deferred_dispatch_binds_before_evaluating(self):
        dispatched = []

        class DeferredContext(Context):
            def _dispatch_action_(self, frame):
                future = Future()
                dispatched.append((frame, future))
                return Results._from_future(future, frame.output_types)

        action = self.plugin.methods['split_ints']
        artifact = Artifact.import_data(IntSequence1, [1, 2, 3])
        call = action._outer_bind(lambda: DeferredContext(action))
        proxy = PendingArtifact(Future(), None, IntSequence1)

        for argument in (artifact, proxy):
            with self.subTest(argument=type(argument).__name__):
                dispatched.clear()
                results = call(argument)
                (frame, future), = dispatched
                self.assertIs(frame.bound_arguments['ints'], argument)
                self.assertIsNone(frame.transformed_arguments)
                self.assertEqual(results.left.type, IntSequence1)
                self.assertFalse(future.done())

                frame.bound_arguments['ints'] = artifact
                with patch.object(
                    action.signature, 'solve_output',
                    side_effect=AssertionError(
                        'Evaluation tried to solve output types'),
                ):
                    future.set_result(frame())
                self.assertEqual(results.left.view(list), [1])
                self.assertEqual(frame.provenance.inputs['ints'],
                                 str(artifact.uuid))
                with self.assertRaisesRegex(RuntimeError, 'finished frame'):
                    frame()

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

    def test_serialized_frame_resumes_application_without_rebinding(self):
        @contextmanager
        def bind_state(frame):
            frame.request_label = 'bound in the caller'
            yield

        @contextmanager
        def consume_state(frame):
            assert frame.request_label == 'bound in the caller'
            yield

        def reject_rebind(frame):
            raise AssertionError('The restored frame rebound its arguments')

        class SerializingContext(Context):
            outer_bind_wrappers = (bind_state, *Context.outer_bind_wrappers)
            inner_apply_wrappers = (consume_state,)

            def _dispatch_action_(self, frame):
                worker = dill.loads(dill.dumps(frame))
                assert worker.current_phase is FramePhase.OUTER_BIND
                assert worker.next_phase is FramePhase.INNER_APPLY
                with patch.object(worker.action, 'outer_bind_wrappers',
                                  (reject_rebind,)):
                    return Context._dispatch_action_(worker.ctx, worker)

        action = self.plugin.methods['split_ints']
        artifact = Artifact.import_data(IntSequence1, [1, 2, 3])
        call = action._outer_bind(lambda: SerializingContext(action))
        results = call(artifact)
        self.assertEqual(results.left.view(list), [1])
        self.assertEqual(results.right.view(list), [2, 3])


if __name__ == '__main__':
    unittest.main()
