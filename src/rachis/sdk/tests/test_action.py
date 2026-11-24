# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import collections
import tempfile
import unittest
import warnings

import rachis.core.archive as archive

from rachis.core.testing.util import get_dummy_plugin
from rachis.plugin.testing import TestPluginBase

from rachis.sdk import Artifact, Visualization
from rachis.sdk.context import Context, ParallelContext
from rachis.sdk.parallel_config import ParallelConfig
from rachis.core.testing.type import (IntSequence1, IntSequence2, SingleInt,
                                      Mapping)
from rachis.core.testing.method import (migrated_method_true_no_dict,
                                        migrated_method_missing_required_key,
                                        migrated_method_invalid_key,
                                        migrated_method_empty_key_value,
                                        migrated_method_not_a_dict)
from rachis.core.testing.visualizer import most_common_viz
from rachis import Metadata
from rachis.metadata.tests.test_io import get_data_path


# NOTE: This test suite exists for tests not easily split into
# test_method, test_visualizer, test_pipeline
# TestBadInputs tests type mismatches between Action signatures and passed args


class TestBadInputs(TestPluginBase):
    def make_provenance_capture(self):
        # importing visualizations is not supported, but we do that here to
        # simplify testing machinery
        return archive.ImportProvenanceCapture()

    def setUp(self):
        self.plugin = get_dummy_plugin()

        # TODO standardize temporary directories created by QIIME 2
        # create a temporary data_dir for sample Visualizations
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')
        self.data_dir = os.path.join(self.test_dir.name, 'viz-output')
        os.mkdir(self.data_dir)
        most_common_viz(self.data_dir, collections.Counter(range(42)))

    def tearDown(self):
        self.test_dir.cleanup()

    def test_viz_passed_as_input(self):
        saved_viz = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        method = self.plugin.methods['optional_artifacts_method']
        ints1 = Artifact.import_data(IntSequence1, [0, 42, 43])

        # tests Viz passed as primitive parameter
        with self.assertRaisesRegex(
                TypeError, 'Visualizations may not be used as inputs.'):
            method(saved_viz, 42)

        # tests Viz passed as Artifact input
        with self.assertRaisesRegex(
                TypeError, 'Visualizations may not be used as inputs.'):
            method(ints1, 42, optional1=saved_viz)

        # tests Viz passed as metadata
        method = self.plugin.methods['identity_with_optional_metadata']
        with self.assertRaisesRegex(
                TypeError, 'Visualizations may not be used as inputs.'):
            method(ints1, metadata=saved_viz)

    def test_artifact_passed_incorrectly(self):
        concatenate_ints = self.plugin.methods['concatenate_ints']
        identity_with_metadata = self.plugin.methods['identity_with_metadata']
        ints1 = Artifact.import_data(IntSequence1, [0, 42, 43])
        ints2 = Artifact.import_data(IntSequence1, [99, -22])
        ints3 = Artifact.import_data(IntSequence2, [12, 111])
        inappropriate_Artifact = Artifact.import_data(IntSequence1, [-9999999])
        int1 = 4
        int2 = 5

        # tests Artifact passed as integer
        with self.assertRaisesRegex(
                TypeError, 'int1.*type Int.*IntSequence1'):
            concatenate_ints(ints1, ints2, ints3, inappropriate_Artifact, int2)

        # tests Artifact passed as metadata
        with self.assertRaisesRegex(
                TypeError, '\'metadata\'.*type Metadata.*IntSequence1'):
            identity_with_metadata(ints1, inappropriate_Artifact)

        # tests wrong type of Artifact passed
        with self.assertRaisesRegex(
                TypeError, 'ints3.*IntSequence2.*IntSequence1'):
            concatenate_ints(ints1, ints2, inappropriate_Artifact, int1, int2)

    def test_primitive_passed_incorrectly(self):
        concatenate_ints = self.plugin.methods['concatenate_ints']
        identity_with_metadata = self.plugin.methods['identity_with_metadata']
        params_only_method = self.plugin.methods['params_only_method']

        md_fp = get_data_path('valid/simple.tsv')
        inappropriate_metadata = Metadata.load(md_fp)

        ints1 = Artifact.import_data(IntSequence1, [0, 42, 43])
        ints3 = Artifact.import_data(IntSequence1, [12, 111])
        int1 = 4
        int2 = 5
        arbitrary_int = 43

        # tests primitive int passed as IntSequence artifact
        with self.assertRaisesRegex(TypeError,
                                    'ints2.*43.*incompatible.*IntSequence1'):
            concatenate_ints(ints1, arbitrary_int, ints3, int1, int2)

        # tests primitive passed as metadata
        with self.assertRaisesRegex(TypeError,
                                    'metadata.*43.*incompatible.*Metadata'):
            identity_with_metadata(ints1, arbitrary_int)

        # tests wrong type of primitive passed
        with self.assertRaisesRegex(TypeError,
                                    'age.*arbitraryString.*incompatible.*Int'):
            params_only_method('key string', 'arbitraryString')

        # tests metadata passed as artifact
        with self.assertRaisesRegex(TypeError,
                                    '\'ints2\'.*Metadata.*IntSequence1'):
            concatenate_ints(ints1, inappropriate_metadata, ints3, int1, int2)

    def test_primitive_param_out_of_range(self):
        range_nested_in_list = self.plugin.methods['variadic_input_method']
        range_not_nested_in_list = self.plugin.visualizers['params_only_viz']
        ints_list = [Artifact.import_data(IntSequence1, [0, 42, 43]),
                     Artifact.import_data(IntSequence2, [4, 5, 6])]
        int_set = {Artifact.import_data(SingleInt, 7),
                   Artifact.import_data(SingleInt, 8)}
        nums = {9, 10}
        bad_range_val = [11, 12, -9999]
        invalid_age = -99999

        # Tests primitives of correct type but outside of Range...
        # ... in a list
        with self.assertRaisesRegex(
                TypeError, 'opt_nums.*-9999.*incompatible.*List'):
            range_nested_in_list(ints_list, int_set, nums, bad_range_val)

        # ... not in a list
        with self.assertRaisesRegex(
                TypeError,
                r'\'age\'.*-99999.*incompatible.*Int % Range\(0, None\)'):
            range_not_nested_in_list('John Doe', invalid_age)

    def test_primitive_param_not_valid_choice(self):
        pipeline = self.plugin.pipelines['failing_pipeline']
        int_sequence = Artifact.import_data(IntSequence1, [0, 42, 43])
        break_from = "invalid choice"

        # test String not a valid choice
        with self.assertRaisesRegex(
                TypeError, 'break_from.*\'invalid choice\''):
            pipeline(int_sequence, break_from)


class TestActionContext(unittest.TestCase):
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
        self.assertIsNot(contexts[0].execution_metadata,
                         contexts[1].execution_metadata)

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


class TestDeprecation(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()
        self.method = self.plugin.methods['deprecated_method']

    def test_successful_registration(self):
        self.assertTrue(self.method.deprecated)

    def test_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as w:
            self.method()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertTrue('Method is deprecated' in str(warning.message))

    def test_docstring(self):
        self.assertIn('Method is deprecated', self.method.__call__.__doc__)


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()
        self.method1 = self.plugin.methods['migrated_method_all_optional_keys']
        self.method2 = self.plugin.methods['migrated_method_no_optional_keys']
        self.method3 = self.plugin.methods['migrated_method_from_distro']
        self.method4 = self.plugin.methods['migrated_method_to_distro']
        self.method5 = self.plugin.methods['migrated_method_epoch']
        self.method6 = \
            self.plugin.methods['migrated_method_from_distro_to_distro']
        self.method7 = self.plugin.methods['migrated_method_from_distro_epoch']
        self.method8 = self.plugin.methods['migrated_method_to_distro_epoch']

    def test_migration_message_all_optional_keys(self):
        with warnings.catch_warnings(record=True) as w:
            self.method1()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin of the old distribution to '
                             'the smart plugin of the new distribution '
                             'in 2025.4.', str(warning.message))

    def test_migration_message_no_optional_keys(self):
        with warnings.catch_warnings(record=True) as w:
            self.method2()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin to the smart plugin in a '
                             'future release.', str(warning.message))

    def test_migration_message_from_distro(self):
        with warnings.catch_warnings(record=True) as w:
            self.method3()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin of the old distribution '
                             'to the smart plugin in a future release.',
                             str(warning.message))

    def test_migration_message_to_distro(self):
        with warnings.catch_warnings(record=True) as w:
            self.method4()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin to the smart plugin of the '
                             'new distribution in a future release.',
                             str(warning.message))

    def test_migration_message_epoch(self):
        with warnings.catch_warnings(record=True) as w:
            self.method5()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin to the smart plugin '
                             'in 2025.4.', str(warning.message))

    def test_migration_message_from_distro_to_distro(self):
        with warnings.catch_warnings(record=True) as w:
            self.method6()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin of the old distribution to '
                             'the smart plugin of the new distribution '
                             'in a future release.', str(warning.message))

    def test_migration_message_from_distro_epoch(self):
        with warnings.catch_warnings(record=True) as w:
            self.method7()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin of the old distribution to '
                             'the smart plugin in 2025.4.',
                             str(warning.message))

    def test_migration_message_to_distro_epoch(self):
        with warnings.catch_warnings(record=True) as w:
            self.method8()
            self.assertEqual(1, len(w))
            warning = w[0]
            self.assertEqual(warning.category, FutureWarning)
            self.assertEqual('This Method is slated for migration from the '
                             'dummy_plugin plugin to the smart plugin of the '
                             'new distribution in 2025.4.',
                             str(warning.message))

    def test_migration_failure_true_no_dict(self):
        with self.assertRaisesRegex(TypeError, '`migrated` must be False or a '
                                    'Mapping with at least `to_plugin`'):
            # migrated set to True; no dict
            self.plugin.methods.register_function(
                function=migrated_method_true_no_dict,
                inputs={},
                parameters={},
                outputs=[
                    ('out', Mapping)
                ],
                name='Migrated method set to True',
                description='Migrated method set to True without a dict or '
                            'required key.',
                migrated={True},
            )

    def test_migration_failure_missing_required_key(self):
        with self.assertRaisesRegex(ValueError, '`migrated` mapping missing '
                                    'required key: `to_plugin`.'):
            # missing required key
            self.plugin.methods.register_function(
                function=migrated_method_missing_required_key,
                inputs={},
                parameters={},
                outputs=[
                    ('out', Mapping)
                ],
                name='Migrated method missing `to_plugin`',
                description='Migrated method without the required '
                            '`to_plugin` key.',
                migrated={'epoch': '2025.4'},
            )

    def test_migration_failure_invalid_key(self):
        with self.assertRaisesRegex(ValueError, 'Got unexpected key: ziggy.*'):
            # invalid key
            self.plugin.methods.register_function(
                function=migrated_method_invalid_key,
                inputs={},
                parameters={},
                outputs=[
                    ('out', Mapping)
                ],
                name='Migrated method with an invalid key',
                description='Migrated method with an invalid key `ziggy`.',
                migrated={'to_plugin': 'smart', 'ziggy': 'dog'},
            )

    def test_migration_failure_empty_key_value(self):
        with self.assertRaisesRegex(TypeError, r'`migrated\["epoch"\]` must '
                                    r'be a non-empty string\.'):
            # empty key value
            self.plugin.methods.register_function(
                function=migrated_method_empty_key_value,
                inputs={},
                parameters={},
                outputs=[
                    ('out', Mapping)
                ],
                name='Migrated method with an empty key value',
                description='Migrated method with an optional key '
                            'containing an empty value.',
                migrated={'to_plugin': 'smart', 'epoch': ''},
            )

    def test_migration_failure_not_a_dict(self):
        with self.assertRaisesRegex(TypeError, '`migrated` must be False or a '
                                    'Mapping with at least `to_plugin`.'):
            # list not a dict
            self.plugin.methods.register_function(
                function=migrated_method_not_a_dict,
                inputs={},
                parameters={},
                outputs=[
                    ('out', Mapping)
                ],
                name='Migrated method with no dict',
                description='Migrated method with a list instead of a dict.',
                migrated=['to_plugin', 'to_distro', 'epoch'],
            )
