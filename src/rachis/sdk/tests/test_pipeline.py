# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import (
    Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError,
)
from contextlib import contextmanager
import inspect
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import pandas as pd

import rachis
import rachis.sdk
from rachis.core.cache import Cache, get_cache
from rachis.core.testing.util import get_dummy_plugin
from rachis.core.testing.type import IntSequence1, SingleInt, Mapping, Foo
from rachis.core.util import load_action_yaml
from rachis.plugin import Visualization, Int, Bool, Properties
import rachis.sdk.parallel_config
from rachis.plugin import List, Collection, IContext
from rachis.sdk.context import ParallelContext
from rachis.sdk.futures import result_as_future
from rachis.sdk.proxy import ProxyArtifact


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()
        self.single_int = rachis.Artifact.import_data(SingleInt, -1)
        self.int_sequence = rachis.Artifact.import_data(IntSequence1,
                                                        [1, 2, 3])
        self.mapping = rachis.Artifact.import_data(Mapping, {'foo': '42'})

    def test_private_constructor(self):
        with self.assertRaisesRegex(NotImplementedError,
                                    'Pipeline constructor.*private'):
            rachis.sdk.Pipeline()

    def test_from_function_spot_check(self):
        typical_pipeline = self.plugin.pipelines['typical_pipeline']
        self.assertEqual(typical_pipeline.id, 'typical_pipeline')

        assert typical_pipeline.signature.inputs
        for spec in typical_pipeline.signature.inputs.values():
            assert spec.has_description()
            assert spec.has_qiime_type()
            assert not spec.has_view_type()
            assert not spec.has_default()

        spec = typical_pipeline.signature.parameters['add']
        assert spec.has_default()

    def test_from_function_optional(self):
        optional_artifact_pipeline = self.plugin.pipelines[
            'optional_artifact_pipeline']

        spec = optional_artifact_pipeline.signature.inputs['single_int']
        assert spec.has_default()

    def test_is_callable(self):
        assert callable(self.plugin.pipelines['typical_pipeline'])

    def test_callable_and_async_signature(self):
        # Shouldn't include `ctx`
        typical_pipeline = self.plugin.pipelines['typical_pipeline']
        kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
        exp_parameters = [
            ('int_sequence', inspect.Parameter(
                'int_sequence', kind, annotation=IntSequence1)),
            ('mapping', inspect.Parameter(
                'mapping', kind, annotation=Mapping)),
            ('do_extra_thing', inspect.Parameter(
                'do_extra_thing', kind, annotation=Bool)),
            ('add', inspect.Parameter(
                'add', kind, default=1, annotation=Int))
        ]

        for callable_attr in '__call__', 'asynchronous':
            signature = inspect.Signature.from_callable(
                getattr(typical_pipeline, callable_attr))
            parameters = list(signature.parameters.items())

            self.assertEqual(parameters, exp_parameters)

    def test_signatures_independent(self):
        typical_pipeline = self.plugin.pipelines['typical_pipeline']
        parameter_only_pipeline = self.plugin.pipelines[
            'parameter_only_pipeline']

        for callable_attr in '__call__', 'asynchronous':
            signature_a = inspect.Signature.from_callable(
                getattr(typical_pipeline, callable_attr))

            signature_b = inspect.Signature.from_callable(
                getattr(parameter_only_pipeline, callable_attr))

            self.assertNotEqual(signature_a, signature_b)

    def test_list_pipeline(self):
        list_pipeline = self.plugin.pipelines['list_pipeline']

        int_list = [rachis.Artifact.import_data(IntSequence1, [0, 1, 2]),
                    rachis.Artifact.import_data(IntSequence1, [3, 4, 5])]
        int_dict = {'1': rachis.Artifact.import_data(IntSequence1, [0, 1, 2]),
                    '2': rachis.Artifact.import_data(IntSequence1, [3, 4, 5])}

        list_out = list_pipeline(int_list)
        dict_out = list_pipeline(int_dict)

        self.assertEqual(len(list_out), 1)
        self.assertEqual(len(dict_out), 1)

        self.assertIsInstance(list_out.output, rachis.sdk.ResultCollection)
        self.assertIsInstance(dict_out.output, rachis.sdk.ResultCollection)

        self.assertEqual(list(list_out.output.keys()), ['0', '1'])
        self.assertEqual(list(dict_out.output.keys()), ['0', '1'])

        self.assertEqual(
            [v.view(int) for v in list_out.output.values()], [4, 5])
        self.assertEqual(
            [v.view(int) for v in dict_out.output.values()], [4, 5])

    def test_collection_pipeline(self):
        collection_pipeline = self.plugin.pipelines['collection_pipeline']

        int_list = [rachis.Artifact.import_data(IntSequence1, [0, 1, 2]),
                    rachis.Artifact.import_data(IntSequence1, [3, 4, 5])]
        int_dict = {'1': rachis.Artifact.import_data(IntSequence1, [0, 1, 2]),
                    '2': rachis.Artifact.import_data(IntSequence1, [3, 4, 5])}

        list_out = collection_pipeline(int_list)
        dict_out = collection_pipeline(int_dict)

        self.assertEqual(len(list_out), 1)
        self.assertEqual(len(dict_out), 1)

        self.assertIsInstance(list_out.output, rachis.sdk.ResultCollection)
        self.assertIsInstance(dict_out.output, rachis.sdk.ResultCollection)

        self.assertEqual(list(list_out.output.keys()), ['key1', 'key2'])
        self.assertEqual(list(dict_out.output.keys()), ['key1', 'key2'])

        self.assertEqual(
            [v.view(int) for v in list_out.output.values()], [4, 5])
        self.assertEqual(
            [v.view(int) for v in dict_out.output.values()], [4, 5])

    def test_de_facto_collection_pipeline(self):
        de_facto_collection_pipeline = \
            self.plugin.pipelines['de_facto_collection_pipeline']

        result = de_facto_collection_pipeline()
        self.assertEqual(len(result), 1)

        output = result.output
        self.assertIsInstance(output, rachis.sdk.ResultCollection)

        expected = {'0': {'foo': '42'}, '1': {'foo': '42'}}
        observed = {}
        for k, v in output.items():
            observed[k] = v.view(dict)

        self.assertEqual(observed, expected)

    def test_de_facto_collection_pipeline_parallel(self):
        de_facto_collection_pipeline = \
            self.plugin.pipelines['de_facto_collection_pipeline']

        with rachis.sdk.parallel_config.ParallelConfig():
            result = de_facto_collection_pipeline.parallel()._result()

        self.assertEqual(len(result), 1)

        output = result.output

        self.assertIsInstance(output, rachis.sdk.ResultCollection)

        expected = {'0': {'foo': '42'}, '1': {'foo': '42'}}
        observed = {}
        for k, v in output.items():
            observed[k] = v.view(dict)

        self.assertEqual(observed, expected)

    def test_pipeline_output_property_refinement(self):
        pipeline = self.plugin.pipelines['property_refinement_pipeline']

        result = pipeline()

        self.assertEqual(result.output.type, Foo % Properties('A'))

    def test_pipeline_collection_output_property_refinement(self):
        pipeline = self.plugin.pipelines[
            'property_refinement_collection_pipeline']

        result = pipeline()

        expected_type = Foo % Properties('A')
        self.assertEqual(result.output.type, Collection[expected_type])
        for artifact in result.output.values():
            self.assertEqual(artifact.type, expected_type)

    def test_pipeline_output_property_refinement_mismatch(self):
        pipeline = self.plugin.pipelines[
            'property_refinement_mismatch_pipeline']

        with self.assertRaisesRegex(
                TypeError,
                "Expected output 'output' to be of type "
                ".*Foo.*Properties.*Bar"):
            pipeline()

    def iter_callables(self, name):
        pipeline = self.plugin.pipelines[name]
        yield pipeline
        yield lambda *args, **kwargs: pipeline.asynchronous(
            *args, **kwargs).result()

    def test_parameter_only_pipeline(self):
        index = pd.Index(['a', 'b', 'c'], name='id', dtype=object)
        df = pd.DataFrame({'col1': ['2', '1', '3']}, index=index, dtype=object)
        metadata = rachis.Metadata(df)
        for call in self.iter_callables('parameter_only_pipeline'):
            results = call(100)
            self.assertEqual(results.foo.view(list), [100, 2, 3])
            self.assertEqual(results.bar.view(list),
                             [100, 2, 3, 100, 2, 3, 100, 2, 3, 100, 2])

            results = call(3, int2=4, metadata=metadata)
            self.assertEqual(results.foo.view(list), [3, 4, 3])
            self.assertEqual(results.bar.view(list),
                             [3, 4, 3, 3, 4, 3, 3, 4, 3, 3, 4])

    def test_typical_pipeline(self):
        for call in self.iter_callables('typical_pipeline'):
            results = call(self.int_sequence, self.mapping, False)

            self.assertEqual(results.left_viz.type, Visualization)
            self.assertEqual(results.left.view(list), [1])
            self.assertEqual(results.right.view(list), [2, 3])
            self.assertNotEqual(results.out_map.uuid, self.mapping.uuid)
            self.assertEqual(results.out_map.view(dict),
                             self.mapping.view(dict))

            results = call(self.int_sequence, self.mapping, True, add=5)
            self.assertEqual(results.left.view(list), [6])
            self.assertEqual(results.right.view(list), [2, 3])

            with self.assertRaisesRegex(ValueError, 'Bad mapping'):
                m = rachis.Artifact.import_data(Mapping, {'a': 1})
                call(self.int_sequence, m, False)

    def test_optional_artifact_pipeline(self):
        for call in self.iter_callables('optional_artifact_pipeline'):
            ints, = call(self.int_sequence)
            self.assertEqual(ints.view(list), [1, 2, 3, 4])

            ints, = call(self.int_sequence, single_int=self.single_int)
            self.assertEqual(ints.view(list), [1, 2, 3, -1])

    def test_visualizer_only_pipeline(self):
        for call in self.iter_callables('visualizer_only_pipeline'):
            viz1, viz2 = call(self.mapping)

            self.assertEqual(viz1.type, Visualization)
            self.assertEqual(viz2.type, Visualization)

    def test_pipeline_in_pipeline(self):
        for call in self.iter_callables('pipelines_in_pipeline'):
            results = call(self.int_sequence, self.mapping)

            self.assertEqual(results.int1.view(int), 4)
            self.assertEqual(results.right_viz.type, Visualization)
            self.assertEqual(len(results), 8)

            with self.assertRaisesRegex(ValueError, 'Bad mapping'):
                m = rachis.Artifact.import_data(Mapping, {1: 1})
                call(self.int_sequence, m)

    def test_pointless_pipeline(self):
        for call in self.iter_callables('pointless_pipeline'):
            single_int, = call()
            self.assertEqual(single_int.type, SingleInt)
            self.assertEqual(single_int.view(int), 4)

    def test_de_facto_list_arg(self):
        pipeline = self.plugin.pipelines['de_facto_list_pipeline']

        exp = {'0': 0, '1': 1, '2': 2}

        ret = pipeline()
        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_de_facto_list_arg_parallel(self):
        pipeline = self.plugin.pipelines['de_facto_list_pipeline']

        exp = {'0': 0, '1': 1, '2': 2}

        with rachis.sdk.parallel_config.ParallelConfig():
            ret = pipeline.parallel()

        self.assertIsInstance(ret, rachis.sdk.Results)
        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_de_facto_list_kwarg(self):
        pipeline = self.plugin.pipelines['de_facto_list_pipeline']

        exp = {'0': 0, '1': 1, '2': 2}

        ret = pipeline(kwarg=True)
        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_de_facto_list_kwarg_parallel(self):
        pipeline = self.plugin.pipelines['de_facto_list_pipeline']

        exp = {'0': 0, '1': 1, '2': 2}

        with rachis.sdk.parallel_config.ParallelConfig():
            ret = pipeline.parallel(kwarg=True)._result()

        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_de_facto_dict_arg(self):
        pipeline = self.plugin.pipelines['de_facto_dict_pipeline']

        exp = {'1': 0, '2': 1, '3': 2}

        ret = pipeline()
        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_de_facto_dict_arg_parallel(self):
        pipeline = self.plugin.pipelines['de_facto_dict_pipeline']

        exp = {'1': 0, '2': 1, '3': 2}

        with rachis.sdk.parallel_config.ParallelConfig():
            ret = pipeline.parallel()._result()

        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_de_facto_dict_kwarg(self):
        pipeline = self.plugin.pipelines['de_facto_dict_pipeline']

        exp = {'1': 0, '2': 1, '3': 2}

        ret = pipeline(kwarg=True)
        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_de_facto_dict_kwarg_parallel(self):
        pipeline = self.plugin.pipelines['de_facto_dict_pipeline']

        exp = {'1': 0, '2': 1, '3': 2}

        with rachis.sdk.parallel_config.ParallelConfig():
            ret = pipeline.parallel(kwarg=True)._result()

        obs = rachis.sdk.util.view_collection(ret.output, int)

        self.assertEqual(obs, exp)

    def test_nested_pipeline_parallel(self):
        pipeline = self.plugin.pipelines['pipelines_in_pipeline']
        with rachis.sdk.parallel_config.ParallelConfig():
            results = pipeline.parallel(self.int_sequence, self.mapping)

        self.assertIsInstance(results.int1, rachis.Artifact)
        self.assertEqual(results.int1.view(int), 4)

    def test_synchronous_pipeline_preserves_active_cache(self):
        pipeline = self.plugin.pipelines['parameter_only_pipeline']
        with TemporaryDirectory() as directory:
            cache = Cache(f'{directory}/cache')
            with cache:
                first, _ = pipeline(1)
                self.assertIs(get_cache(), cache)
                self.assertEqual(first.view(list), [1, 2, 3])

    def test_annotations_correct(self):
        def annotations(
                    ctx: IContext,
                    is_int: int,
                    is_art: rachis.Artifact,
                    is_list: list[rachis.Artifact],
                    is_dict: dict[str, rachis.Artifact],
                ) -> tuple[rachis.Artifact, list[rachis.Artifact],
                           dict[str, rachis.Artifact], rachis.Visualization,
                           list[rachis.Visualization],
                           dict[str, rachis.Visualization]]:
            pass

        rachis.core.type.PipelineSignature(
            callable=annotations,
            inputs={
                'is_art': SingleInt,
                'is_list': List[SingleInt],
                'is_dict': Collection[SingleInt]
            },
            parameters={
                'is_int': Int,
            },
            outputs=[
                ('out1', SingleInt),
                ('out2', Collection[SingleInt]),
                ('out3', Collection[SingleInt]),
                ('out4', Visualization),
                ('out5', Collection[Visualization]),
                ('out6', Collection[Visualization])
            ],
            input_descriptions=None,
            parameter_descriptions=None,
            output_descriptions=None,
        )

    def test_annotations_bad_context(self):
        def annotations_collections_bad_context(
                    ctx: rachis.Artifact,
                    is_dict: dict[str, rachis.Artifact]
                ) -> tuple[list[rachis.Artifact]]:
            raise ValueError(
                "I should never run. I should never even pass registration"
            )

        with self.assertRaisesRegex(
                TypeError, r'.*Context.*IContext.*'):
            rachis.core.type.PipelineSignature(
                callable=annotations_collections_bad_context,
                inputs={
                    'is_dict': Collection[SingleInt]
                },
                parameters={},
                outputs=[
                    ('out1', Collection[SingleInt]),
                ],
                input_descriptions=None,
                parameter_descriptions=None,
                output_descriptions=None
            )

    def test_annotations_collections_bad_dict_input(self):
        def annotations_collections_bad_dict_input(
                    ctx: IContext,
                    is_dict: dict[rachis.Artifact]
                ) -> tuple[list[rachis.Artifact]]:
            raise ValueError(
                "I should never run. I should never even pass registration"
            )

        with self.assertRaisesRegex(
                TypeError, r'.*is_dict.*dict\[rachis.sdk.result.Artifact\].*'):
            rachis.core.type.PipelineSignature(
                callable=annotations_collections_bad_dict_input,
                inputs={
                    'is_dict': Collection[SingleInt]
                },
                parameters={},
                outputs=[
                    ('out1', Collection[SingleInt]),
                ],
                input_descriptions=None,
                parameter_descriptions=None,
                output_descriptions=None
            )

    def test_annotations_collections_bad_dict_output(self):
        def annotations_collections_bad_dict_output(
                    ctx: IContext,
                    is_list: list[rachis.Artifact]
                ) -> tuple[dict[rachis.Artifact]]:
            raise ValueError(
                "I should never run. I should never even pass registration"
            )

        with self.assertRaisesRegex(
                TypeError, r'.*out1.*dict\[rachis.sdk.result.Artifact\].*'):
            rachis.core.type.PipelineSignature(
                callable=annotations_collections_bad_dict_output,
                inputs={
                    'is_list': List[SingleInt],
                },
                parameters={},
                outputs=[
                    ('out1', Collection[SingleInt])
                ],
                input_descriptions=None,
                parameter_descriptions=None,
                output_descriptions=None
            )

    def test_annotations_mixed(self):
        def mixed_annotations(
                    ctx: IContext, annotated: int, unannotated
                ) -> tuple[rachis.Artifact]:
            raise ValueError(
                "I should never run. I should never even pass registration"
            )

        with self.assertRaisesRegex(
                    TypeError,
                    "Pipelines must have annotations for the context, all"
                    " inputs, parameters, and outputs or no annotations."
                ):
            rachis.core.type.PipelineSignature(
                callable=mixed_annotations,
                inputs={},
                parameters={
                    'annotated': Int,
                    'unannotated': Int
                },
                outputs=[('out1', SingleInt)],
                input_descriptions=None,
                parameter_descriptions=None,
                output_descriptions=None
            )

    def test_annotations_bad_input(self):
        def bad_input_annotation(
                    ctx: IContext,
                    is_artifact: int,
                    is_int: rachis.Artifact
                ) -> tuple[rachis.Artifact]:
            raise ValueError(
                "I should never run. I should never even pass registration"
            )

        with self.assertRaisesRegex(TypeError, ".*is_artifact.*int.*"):
            rachis.core.type.PipelineSignature(
                callable=bad_input_annotation,
                inputs={
                    'is_artifact': SingleInt,
                },
                parameters={
                    'is_int': Int,
                },
                outputs=[('out1', SingleInt)],
                input_descriptions=None,
                parameter_descriptions=None,
                output_descriptions=None
            )

    def test_annotations_bad_output(self):
        def bad_output_annotation(
                    ctx: IContext,
                    is_artifact: rachis.Artifact,
                    is_int: rachis.Artifact
                ) -> tuple[int]:
            raise ValueError(
                "I should never run. I should never even pass registration"
            )

        with self.assertRaisesRegex(TypeError, ".*out1.*int.*"):
            rachis.core.type.PipelineSignature(
                callable=bad_output_annotation,
                inputs={
                    'is_artifact': SingleInt,
                },
                parameters={
                    'is_int': Int,
                },
                outputs=[('out1', SingleInt)],
                input_descriptions=None,
                parameter_descriptions=None,
                output_descriptions=None
            )

    def test_failing_from_arity(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(TypeError, 'match number.*3.*1'):
                call(self.int_sequence, break_from='arity')

    def test_failing_from_return_view(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(TypeError, 'Result.*objects.*None'):
                call(self.int_sequence, break_from='return-view')

    def test_failing_from_invalid_result(self):
        for call in self.iter_callables('failing_pipeline'):
            for mode in ('return-scalar', 'return-collection-member'):
                with self.subTest(mode=mode), self.assertRaisesRegex(
                    TypeError, 'Result.*objects.*int'
                ):
                    call(self.int_sequence, break_from=mode)

    def test_failing_from_method(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(ValueError, "Key 'foo' exists"):
                call(self.int_sequence, break_from='method')

    def test_parallel_pipeline_catches_failure_at_inner_scope_exit(self):
        pipeline = self.plugin.pipelines['failing_pipeline']
        with rachis.sdk.parallel_config.ParallelConfig():
            mapping, = pipeline.parallel(self.int_sequence,
                                         break_from='method-in-scope')
        self.assertEqual(mapping.view(dict), {'caught': 'at scope exit'})

    def test_failing_from_type(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(TypeError, 'Mapping.*SingleInt'):
                call(self.int_sequence, break_from='type')

    def test_failing_from_internal(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(ValueError, 'this never works'):
                call(self.int_sequence, break_from='internal')

    def test_failing_from_missing_plugin(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(ValueError, r'plugin.*not\%a\$plugin'):
                call(self.int_sequence, break_from='no-plugin')

    def test_failing_from_missing_action(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(ValueError, r'action.*not\%a\$method'):
                call(self.int_sequence, break_from='no-action')


class TestParallelPipeline(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()
        registered = patch.dict(self.plugin.pipelines)
        registered.start()
        self.addCleanup(registered.stop)
        self.ints = rachis.Artifact.import_data(IntSequence1, [1, 2, 3])

    def register_pipeline(self, function, *, outputs, inputs=None):
        self.plugin.pipelines.register_function(
            function, inputs=inputs or {}, parameters={}, outputs=outputs,
            name=function.__name__, description='',
        )
        return self.plugin.pipelines[function.__name__]

    def test_downstream_pipeline_submits_before_producer_returns(self):
        submitted = Event()
        ints = self.ints

        def produce(ctx):
            if not submitted.wait(timeout=5):
                raise TimeoutError('The consumer did not submit its child')
            return ints

        def consume(ctx, ints):
            split = ctx.get_action('dummy_plugin', 'split_ints')
            left = split(ints).left
            submitted.set()
            return left

        self.register_pipeline(produce, outputs={'output': IntSequence1})
        self.register_pipeline(consume, inputs={'ints': IntSequence1},
                               outputs={'output': IntSequence1})
        with (rachis.sdk.parallel_config.ParallelConfig(),
              ParallelContext(None) as ctx):
            upstream = ctx.get_action('dummy_plugin', 'produce')()
            downstream = ctx.get_action('dummy_plugin', 'consume')(
                upstream.output)

        self.assertEqual(downstream.output.view(list), [1])

    def test_consumer_uses_ready_output_while_sibling_is_pending(self):
        delayed = Future()
        ready = self.ints
        sibling = rachis.Artifact.import_data(IntSequence1, [4])

        def produce(ctx):
            return ready, ProxyArtifact(delayed, None, IntSequence1)

        self.register_pipeline(
            produce, outputs={'ready': IntSequence1, 'delayed': IntSequence1})
        with (rachis.sdk.parallel_config.ParallelConfig(),
              ParallelContext(None) as ctx):
            try:
                # Catch an accidental _result() call before it can wait on
                # the sibling this test releases after the consumer finishes.
                with patch.object(
                    rachis.sdk.Results, '_result',
                    side_effect=AssertionError(
                        'Submission tried to realize all outputs'),
                ):
                    outputs = ctx.get_action('dummy_plugin', 'produce')()
                split = ctx.get_action('dummy_plugin', 'split_ints')
                first = result_as_future(split(outputs.ready).left)
                self.assertEqual(first.result(timeout=5).view(list), [1])
                self.assertFalse(result_as_future(outputs.delayed).done())
            finally:
                delayed.set_result(sibling)

        self.assertEqual(outputs.delayed.view(list), [4])

    def test_alias_waits_for_original_input_provenance(self):
        original = Future()
        alias_prepared = Future()
        ready = self.ints

        def return_ready(ctx, original):
            return ready

        @contextmanager
        def observe_alias(frame):
            yield
            alias_prepared.set_result(result_as_future(frame.results.output))

        pipeline = self.register_pipeline(
            return_ready, inputs={'original': IntSequence1},
            outputs={'output': IntSequence1})
        with (
            patch.object(pipeline, 'inner_apply_wrappers',
                         (observe_alias, *pipeline.inner_apply_wrappers)),
            rachis.sdk.parallel_config.ParallelConfig(),
            ParallelContext(None) as ctx,
        ):
            try:
                run = ctx.get_action('dummy_plugin', pipeline.id)
                result = run(ProxyArtifact(original, None, IntSequence1))
                self.assertFalse(alias_prepared.result(timeout=5).done())
            finally:
                original.set_result(ready)

        action = load_action_yaml(result.output._archiver.path)['action']
        self.assertEqual(action['inputs'], [{'original': str(ready.uuid)}])

    def test_provenance_snapshots_pending_collection_inputs(self):
        original = Future()
        cleared = Event()
        source = rachis.Artifact.import_data(SingleInt, 2)

        def clear_inputs(ctx, ints):
            ints.clear()
            cleared.set()
            return [source]

        pipeline = self.register_pipeline(
            clear_inputs, inputs={'ints': List[IntSequence1]},
            outputs={'output': Collection[SingleInt]})
        with (rachis.sdk.parallel_config.ParallelConfig(),
              ParallelContext(None) as ctx):
            try:
                run = ctx.get_action('dummy_plugin', pipeline.id)
                result = run([ProxyArtifact(original, None, IntSequence1)])
                self.assertTrue(cleared.wait(timeout=5))
            finally:
                original.set_result(self.ints)

        action = load_action_yaml(result.output['0']._archiver.path)['action']
        self.assertEqual(action['inputs'], [{'ints': [str(self.ints.uuid)]}])
        self.assertEqual(action['alias-of'], str(source.uuid))

    def test_pipeline_output_failure_surfaces_at_scope_exit(self):
        returned_to_caller = False

        with (rachis.sdk.parallel_config.ParallelConfig(),
              self.assertRaisesRegex(ValueError, "Key 'foo' exists"),
              ParallelContext(None) as ctx):
            fail = ctx.get_action('dummy_plugin', 'failing_pipeline')
            fail(self.ints, break_from='method')
            returned_to_caller = True

        self.assertTrue(returned_to_caller)

    def test_failure_from_discarded_nested_call_surfaces_at_scope_exit(self):
        mapping = rachis.Artifact.import_data(Mapping, {'foo': 'a'})
        conflicting = rachis.Artifact.import_data(Mapping, {'foo': 'b'})

        def discard_failed_method(ctx):
            merge = ctx.get_action('dummy_plugin', 'merge_mappings')
            merge(mapping, conflicting)
            return mapping

        def discard_nested_pipeline(ctx):
            ctx.get_action('dummy_plugin', 'discard_failed_method')()
            return mapping

        self.register_pipeline(discard_failed_method,
                               outputs={'output': Mapping})
        pipeline = self.register_pipeline(discard_nested_pipeline,
                                          outputs={'output': Mapping})
        with (rachis.sdk.parallel_config.ParallelConfig(),
              self.assertRaisesRegex(ValueError, "Key 'foo' exists")):
            pipeline.parallel()

    def test_body_failure_settles_output_and_drains_submitted_child(self):
        message = 'Pipeline body failed'
        child_input = Future()
        output = Future()

        def submit_then_raise(ctx):
            split = ctx.get_action('dummy_plugin', 'split_ints')
            split(ProxyArtifact(child_input, None, IntSequence1))
            raise ValueError(message)

        pipeline = self.register_pipeline(
            submit_then_raise, outputs={'output': IntSequence1})

        def run_scope(ctx):
            with ctx:
                run = ctx.get_action('dummy_plugin', pipeline.id)
                output.set_result(result_as_future(run().output))

        with (rachis.sdk.parallel_config.ParallelConfig(),
              ThreadPoolExecutor(max_workers=1) as executor):
            scope = executor.submit(run_scope, ParallelContext(None))
            try:
                with self.assertRaisesRegex(ValueError, message):
                    output.result(timeout=5).result(timeout=5)
                with self.assertRaises(FutureTimeoutError):
                    scope.result(timeout=0.25)
            finally:
                child_input.set_result(self.ints)

            with self.assertRaisesRegex(ValueError, message):
                scope.result(timeout=5)

    def test_output_alias_failure_surfaces_before_parallel_returns(self):
        ints = self.ints

        def return_artifact(ctx):
            return ints

        pipeline = self.register_pipeline(return_artifact,
                                          outputs={'output': IntSequence1})
        with (
            patch.object(rachis.Artifact, '_alias',
                         side_effect=ValueError('Alias failed')),
            rachis.sdk.parallel_config.ParallelConfig(),
            self.assertRaisesRegex(ValueError, '^Alias failed$'),
        ):
            pipeline.parallel()


if __name__ == '__main__':
    unittest.main()
