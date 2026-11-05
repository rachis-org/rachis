# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import types
import unittest
from unittest.mock import patch

import rachis.plugin
import rachis.sdk
import rachis.util

from rachis.core.testing.type import (IntSequence1, IntSequence2, Mapping,
                                      FourInts, Kennel, Dog, Cat, SingleInt)
from rachis.core.testing.format import (IntSequenceDirectoryFormat,
                                        IntSequenceV2DirectoryFormat,
                                        SingleIntFormat)
from rachis.core.testing.util import get_dummy_plugin
from rachis.core.testing.plugin import is1_use, is2_use
from rachis.plugin.testing import assert_no_nans_in_tables


class TestPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()

    def get_data_path(self, filename):
        fp = rachis.util.get_filepath_from_package(
            'rachis.plugin.tests', 'data/%s' % filename)
        return str(fp)

    def test_name(self):
        self.assertEqual(self.plugin.name, 'dummy-plugin')

    def test_version(self):
        self.assertEqual(self.plugin.version, '0.0.0-dev')

    def test_website(self):
        self.assertEqual(self.plugin.website,
                         'https://github.com/qiime2/qiime2')

    def test_package(self):
        self.assertEqual(self.plugin.package, 'rachis.core.testing')

    def test_citations(self):
        self.assertEqual(self.plugin.citations[0].type, 'article')

    def test_user_support_text(self):
        self.assertEqual(self.plugin.user_support_text,
                         'For help, see https://qiime2.org')

    def test_short_description_text(self):
        self.assertEqual(self.plugin.short_description,
                         'Dummy plugin for testing.')

    def test_description_text(self):
        self.assertEqual(self.plugin.description,
                         'Description of dummy plugin.')

    def test_citations_default(self):
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')
        self.assertEqual(plugin.citations, ())

    def test_user_support_text_default(self):
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')
        self.assertTrue(plugin.user_support_text.startswith('Please post'))
        self.assertTrue(plugin.user_support_text.endswith(
                            'https://forum.qiime2.org'))

    def test_actions(self):
        actions = self.plugin.actions

        self.assertIsInstance(actions, types.MappingProxyType)

        self.assertEqual(actions.keys(),
                         {'merge_mappings', 'concatenate_ints', 'split_ints',
                          'most_common_viz', 'mapping_viz',
                          'split_ints_compat',
                          'identity_with_metadata',
                          'identity_with_metadata_column',
                          'identity_with_categorical_metadata_column',
                          'identity_with_numeric_metadata_column',
                          'identity_with_optional_metadata',
                          'identity_with_optional_metadata_column',
                          'params_only_method', 'no_input_method',
                          'optional_artifacts_method', 'variadic_input_method',
                          'params_only_viz', 'no_input_viz',
                          'long_description_method', 'parameter_only_pipeline',
                          'typical_pipeline', 'optional_artifact_pipeline',
                          'pointless_pipeline', 'visualizer_only_pipeline',
                          'pipelines_in_pipeline',
                          'resumable_pipeline',
                          'resumable_varied_pipeline',
                          'resumable_nested_varied_pipeline',
                          'internal_fail_pipeline', 'de_facto_list_pipeline',
                          'mix_arts_and_proxies', 'de_facto_dict_pipeline',
                          'de_facto_collection_pipeline', 'list_pipeline',
                          'collection_pipeline',
                          'property_refinement_pipeline',
                          'property_refinement_collection_pipeline',
                          'property_refinement_mismatch_pipeline',
                          'failing_pipeline',
                          'viz_collection_pipeline',
                          'resumable_random_seed_pipeline',
                          'docstring_order_method',
                          'constrained_input_visualization',
                          'combinatorically_mapped_method',
                          'double_bound_variable_method',
                          'bool_flag_swaps_output_method',
                          'predicates_preserved_method',
                          'deprecated_method',
                          'migrated_method_all_optional_keys',
                          'migrated_method_no_optional_keys',
                          'migrated_method_from_distro',
                          'migrated_method_to_distro',
                          'migrated_method_epoch',
                          'migrated_method_from_distro_to_distro',
                          'migrated_method_from_distro_epoch',
                          'migrated_method_to_distro_epoch',
                          'union_inputs', 'unioned_primitives',
                          'type_match_list_and_set',
                          'list_of_ints', 'dict_of_ints', 'returns_int',
                          'collection_inner_union', 'collection_outer_union',
                          'dict_params', 'list_params', 'varied_method',
                          '_underscore_method', 'return_four_ints',
                          'return_many_ints', 'random_seed_method',
                          'random_seed_method_get_or_set_twice',
                          'random_seed_method_set_twice',
                          'random_seed_method_never_set',
                          'raises_rachis_warning'
                          })
        for action in actions.values():
            self.assertIsInstance(action, rachis.sdk.Action)

        # Read-only dict.
        with self.assertRaises(TypeError):
            actions["i-shouldn't-do-this"] = "my-action"
        with self.assertRaises(TypeError):
            actions["merge_mappings"] = "my-action"

    def test_methods(self):
        methods = self.plugin.methods

        self.assertEqual(methods.keys(),
                         {'merge_mappings', 'concatenate_ints', 'split_ints',
                          'split_ints_compat',
                          'identity_with_metadata',
                          'identity_with_metadata_column',
                          'identity_with_categorical_metadata_column',
                          'identity_with_numeric_metadata_column',
                          'identity_with_optional_metadata',
                          'identity_with_optional_metadata_column',
                          'params_only_method', 'no_input_method',
                          'optional_artifacts_method',
                          'long_description_method',
                          'variadic_input_method', 'docstring_order_method',
                          'combinatorically_mapped_method',
                          'double_bound_variable_method',
                          'bool_flag_swaps_output_method',
                          'predicates_preserved_method',
                          'deprecated_method',
                          'migrated_method_all_optional_keys',
                          'migrated_method_no_optional_keys',
                          'migrated_method_from_distro',
                          'migrated_method_to_distro',
                          'migrated_method_epoch',
                          'migrated_method_from_distro_to_distro',
                          'migrated_method_from_distro_epoch',
                          'migrated_method_to_distro_epoch',
                          'union_inputs', 'unioned_primitives',
                          'type_match_list_and_set',
                          'list_of_ints', 'dict_of_ints', 'returns_int',
                          'collection_inner_union',
                          'collection_outer_union', 'dict_params',
                          'list_params', 'varied_method', '_underscore_method',
                          'return_four_ints', 'return_many_ints',
                          'random_seed_method',
                          'random_seed_method_get_or_set_twice',
                          'random_seed_method_set_twice',
                          'random_seed_method_never_set',
                          'raises_rachis_warning'
                          })
        for method in methods.values():
            self.assertIsInstance(method, rachis.sdk.Method)

    def test_visualizers(self):
        visualizers = self.plugin.visualizers

        self.assertEqual(visualizers.keys(),
                         {'most_common_viz', 'mapping_viz', 'params_only_viz',
                          'no_input_viz', 'constrained_input_visualization'})
        for viz in visualizers.values():
            self.assertIsInstance(viz, rachis.sdk.Visualizer)

    def test_pipelines(self):
        pipelines = self.plugin.pipelines

        self.assertEqual(pipelines.keys(),
                         {'parameter_only_pipeline', 'typical_pipeline',
                          'optional_artifact_pipeline', 'pointless_pipeline',
                          'visualizer_only_pipeline', 'pipelines_in_pipeline',
                          'resumable_pipeline',
                          'resumable_varied_pipeline',
                          'resumable_nested_varied_pipeline',
                          'internal_fail_pipeline', 'de_facto_list_pipeline',
                          'mix_arts_and_proxies', 'de_facto_dict_pipeline',
                          'de_facto_collection_pipeline', 'list_pipeline',
                          'collection_pipeline',
                          'property_refinement_pipeline',
                          'property_refinement_collection_pipeline',
                          'property_refinement_mismatch_pipeline',
                          'failing_pipeline',
                          'viz_collection_pipeline',
                          'resumable_random_seed_pipeline'})
        for pipeline in pipelines.values():
            self.assertIsInstance(pipeline, rachis.sdk.Pipeline)

    # TODO test registration of directory formats.

    def test_deprecated_type_formats(self):
        # Plugin.type_formats was replaced with Plugin.artifact_classes. For
        # backward compatibility the Plugin.type_formats property returns
        # the plugin's artifact_classes
        self.assertEqual(self.plugin.type_formats,
                         list(self.plugin.artifact_classes.values()))

    def test_type_fragments(self):
        types = self.plugin.type_fragments.keys()

        self.assertEqual(
            set(types),
            set(['IntSequence1', 'IntSequence2', 'IntSequence3', 'Mapping',
                 'FourInts', 'Kennel', 'Dog', 'Cat', 'SingleInt', 'C1', 'C2',
                 'C3', 'Foo', 'Bar', 'Baz', 'AscIntSequence', 'Squid',
                 'Octopus', 'Cuttlefish']))

    def test_types(self):
        types = self.plugin.types
        # Get just the ArtifactClassRecords out of the types dictionary, then
        # get just the types out of the ArtifactClassRecords namedtuples
        types = {type_.semantic_type for type_ in types.values()}

        exp = {IntSequence1, IntSequence2, FourInts, Mapping, Kennel[Dog],
               Kennel[Cat], SingleInt}
        self.assertLessEqual(exp, types)
        self.assertNotIn(Cat, types)
        self.assertNotIn(Dog, types)
        self.assertNotIn(Kennel, types)

    def test_register_semantic_type_to_format_deprecated_parameter_name(self):
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')

        # both the new (directory_format) and old (artifact_format) names for
        # the format work
        plugin.register_semantic_type_to_format(
            IntSequence1, directory_format=IntSequenceDirectoryFormat)

        plugin.register_semantic_type_to_format(
            IntSequence2, artifact_format=IntSequenceV2DirectoryFormat)

        ac = plugin.artifact_classes['IntSequence1']
        self.assertEqual(ac.semantic_type, IntSequence1)
        self.assertEqual(ac.format, IntSequenceDirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "")
        self.assertEqual(ac.examples, types.MappingProxyType({}))

        ac = plugin.artifact_classes['IntSequence2']
        self.assertEqual(ac.semantic_type, IntSequence2)
        self.assertEqual(ac.format, IntSequenceV2DirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "")
        self.assertEqual(ac.examples, types.MappingProxyType({}))

        # errors are raised when both or neither the new or old names for the
        # format are provided
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')

        regex = r'ory_format and artifact_for.*IntSequence1'
        with self.assertRaisesRegex(ValueError, regex):
            plugin.register_semantic_type_to_format(
                IntSequence1, directory_format=IntSequenceDirectoryFormat,
                artifact_format=IntSequenceDirectoryFormat)

        regex = r'ory_format or artifact_for.*IntSequence1'
        with self.assertRaisesRegex(ValueError, regex):
            plugin.register_semantic_type_to_format(IntSequence1)

    def test_register_artifact_class(self):
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')
        plugin.register_artifact_class(IntSequence1,
                                       IntSequenceDirectoryFormat)

        # the original approach for registering artifact_class still works
        plugin.register_semantic_type_to_format(IntSequence2,
                                                IntSequenceV2DirectoryFormat)

        plugin.register_artifact_class(Kennel[Dog],
                                       IntSequenceDirectoryFormat)

        plugin.register_artifact_class(Kennel[Cat],
                                       IntSequenceV2DirectoryFormat)

        # all and only the expected artifact classes have been registered
        self.assertEqual(len(plugin.artifact_classes), 4)

        ac = plugin.artifact_classes['IntSequence1']
        self.assertEqual(ac.semantic_type, IntSequence1)
        self.assertEqual(ac.type_expression, IntSequence1)
        self.assertEqual(ac.format, IntSequenceDirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "")
        self.assertEqual(ac.examples, types.MappingProxyType({}))

        ac = plugin.artifact_classes['IntSequence2']
        self.assertEqual(ac.semantic_type, IntSequence2)
        self.assertEqual(ac.type_expression, IntSequence2)
        self.assertEqual(ac.format, IntSequenceV2DirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "")
        self.assertEqual(ac.examples, types.MappingProxyType({}))

        ac = plugin.artifact_classes['Kennel[Dog]']
        self.assertEqual(ac.semantic_type, Kennel[Dog])
        self.assertEqual(ac.type_expression, Kennel[Dog])
        self.assertEqual(ac.format, IntSequenceDirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "")
        self.assertEqual(ac.examples, types.MappingProxyType({}))

        ac = plugin.artifact_classes['Kennel[Cat]']
        self.assertEqual(ac.semantic_type, Kennel[Cat])
        self.assertEqual(ac.type_expression, Kennel[Cat])
        self.assertEqual(ac.format, IntSequenceV2DirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "")
        self.assertEqual(ac.examples, types.MappingProxyType({}))

        self.assertFalse(plugin.artifact_classes['IntSequence1'] is
                         plugin.artifact_classes['IntSequence2'])

    def test_duplicate_artifact_class_registration_disallowed(self):
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')
        plugin.register_artifact_class(IntSequence1,
                                       IntSequenceDirectoryFormat)

        # Registration of type to the same format with both registration
        # methods is disallowed
        with self.assertRaisesRegex(NameError, "ct class IntSequence1.*once"):
            plugin.register_semantic_type_to_format(
                IntSequence1, IntSequenceDirectoryFormat)

        with self.assertRaisesRegex(NameError, "ct class IntSequence1.*once"):
            plugin.register_artifact_class(
                IntSequence1, IntSequenceDirectoryFormat)

        # Registration of type to the different format with both registration
        # methods is disallowed
        with self.assertRaisesRegex(NameError, "ct class IntSequence1.*once"):
            plugin.register_semantic_type_to_format(
                IntSequence1, IntSequenceV2DirectoryFormat)

        with self.assertRaisesRegex(NameError, "ct class IntSequence1.*once"):
            plugin.register_artifact_class(
                IntSequence1, IntSequenceV2DirectoryFormat)

    def test_register_artifact_class_w_annotations(self):
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')
        plugin.register_artifact_class(
            IntSequence1, IntSequenceDirectoryFormat,
            description="A sequence of integers.",
            examples=types.MappingProxyType({'Import ex 1': is1_use}))
        plugin.register_artifact_class(
            IntSequence2, IntSequenceV2DirectoryFormat,
            description="Different seq of ints.",
            examples=types.MappingProxyType({'Import ex': is2_use}))

        ac = plugin.artifact_classes['IntSequence1']
        self.assertEqual(ac.semantic_type, IntSequence1)
        self.assertEqual(ac.format, IntSequenceDirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "A sequence of integers.")
        self.assertEqual(ac.examples,
                         types.MappingProxyType({'Import ex 1': is1_use}))

        ac = plugin.artifact_classes['IntSequence2']
        self.assertEqual(ac.semantic_type, IntSequence2)
        self.assertEqual(ac.format, IntSequenceV2DirectoryFormat)
        self.assertEqual(ac.plugin, plugin)
        self.assertEqual(ac.description, "Different seq of ints.")
        self.assertEqual(ac.examples,
                         types.MappingProxyType({'Import ex': is2_use}))

    def test_register_artifact_class_multiple(self):
        plugin = rachis.plugin.Plugin(
            name='local-dummy-plugin',
            version='0.0.0-dev',
            website='https://github.com/qiime2/qiime2',
            package='rachis.core.testing')

        # multiple artifact_classes can be registered using the original
        # approach, since default descriptions and examples are used
        plugin.register_semantic_type_to_format(Kennel[Dog | Cat],
                                                IntSequenceDirectoryFormat)

        ac_c = plugin.artifact_classes['Kennel[Cat]']
        self.assertEqual(ac_c.semantic_type, Kennel[Cat])
        self.assertEqual(ac_c.format, IntSequenceDirectoryFormat)
        self.assertEqual(ac_c.plugin, plugin)
        self.assertEqual(ac_c.description, "")
        self.assertEqual(ac_c.examples, types.MappingProxyType({}))

        ac_d = plugin.artifact_classes['Kennel[Dog]']
        self.assertEqual(ac_d.semantic_type, Kennel[Dog])
        self.assertEqual(ac_d.format, IntSequenceDirectoryFormat)
        self.assertEqual(ac_d.plugin, plugin)
        self.assertEqual(ac_d.description, "")
        self.assertEqual(ac_d.examples, types.MappingProxyType({}))

        # multiple artifact_classes cannot be registered using
        # register_artifact_class, since default descriptions and examples
        # should be different from one another
        with self.assertRaisesRegex(TypeError,
                                    r'Only a single.*Kennel\[Dog \| Cat\]'):
            plugin.register_artifact_class(
                Kennel[Dog | Cat], IntSequenceDirectoryFormat)

    def test_table_does_not_have_nans(self):
        noNaN = self.get_data_path('no_nan.html')

        with open(noNaN) as fh:
            assert_no_nans_in_tables(fh)

    def test_table_has_nans(self):
        hasNaN = self.get_data_path('has_nan.html')

        with open(hasNaN) as fh:
            with self.assertRaises(AssertionError):
                assert_no_nans_in_tables(fh)

    def test_register_list_output(self):
        def bad_function() -> list:
            pass

        with self.assertRaisesRegex(TypeError,
                                    "'bad_output'.*'List'.*'Collection'.*"):
            self.plugin.methods.register_function(
                function=bad_function,
                inputs={},
                parameters={},
                outputs={
                    'bad_output': rachis.plugin.List[IntSequence1]
                },
                name='Output is registered as returning a List which is bad.',
                description='Output is registered as returning a List which is'
                            ' bad.'
            )

    def test_register_format_validates_compression(self):
        for level in (None, 0, 1, 9):
            with self.subTest(level=level):
                plugin = rachis.plugin.Plugin(
                    name='compression-test', version='0.0.0',
                    website='https://example.com')
                with patch.object(SingleIntFormat, 'COMPRESSION', level):
                    plugin.register_formats(SingleIntFormat)

        for level in (-1, 10, True, 1.5, '2'):
            with self.subTest(level=level):
                plugin = rachis.plugin.Plugin(
                    name='compression-test', version='0.0.0',
                    website='https://example.com')
                with patch.object(SingleIntFormat, 'COMPRESSION', level):
                    with self.assertRaisesRegex(
                            ValueError, 'COMPRESSION must be an integer '
                                        'from 0 to 9'):
                        plugin.register_formats(SingleIntFormat)


if __name__ == '__main__':
    unittest.main()
