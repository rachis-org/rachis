# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import unittest
import inspect

import pandas as pd

import rachis
import rachis.sdk
from rachis.core.testing.util import get_dummy_plugin
from rachis.core.testing.type import IntSequence1, SingleInt, Mapping, Foo
from rachis.plugin import Visualization, Int, Bool, Properties
import rachis.sdk.parallel_config
from rachis.plugin import List, Collection, IContext


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
                'add', kind, default=1, annotation=Int)),
            ('record_provenance', inspect.Parameter(
                'record_provenance', kind, default=True, annotation=Bool))
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

    # TODO: Right now this test just asserts things don't crash, not that
    # any specific desirable behavior is happening
    def test_typical_pipeline_record_no_inner_prov(self):
        for call in self.iter_callables('typical_pipeline'):
            results = call(
                self.int_sequence, self.mapping, False, record_provenance=False
            )

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
            ret = pipeline.parallel()._result()

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
        ''' This test basically just validates that nested pipelines in
            parallel don't blow anything up. It was added concurrently with
            nested parallel pipelines being flattened.
        '''
        pipeline = self.plugin.pipelines['pipelines_in_pipeline']

        ints = rachis.Artifact.import_data(IntSequence1, [1, 2, 3])
        mapping = rachis.Artifact.import_data(Mapping, {'foo': '42'})

        with rachis.sdk.parallel_config.ParallelConfig():
            pipeline.parallel(ints, mapping)._result()

        self.assertTrue(True)

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

    def test_failing_from_method(self):
        for call in self.iter_callables('failing_pipeline'):
            with self.assertRaisesRegex(ValueError, "Key 'foo' exists"):
                call(self.int_sequence, break_from='method')

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


if __name__ == '__main__':
    unittest.main()
