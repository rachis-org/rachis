# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import importlib
import sys
import tempfile
import types
import unittest

import rachis.sdk
from rachis.core.testing.util import get_dummy_plugin
from rachis.plugins import ArtifactAPIUsage


class TestImports(unittest.TestCase):
    def setUp(self):
        self._sys_modules = sys.modules.copy()
        # Ignore the returned dummy plugin object, just run this to verify the
        # plugin exists as the tests rely on it being loaded.
        get_dummy_plugin()

    def tearDown(self):
        # This allows us to reset the state of our imports to test different
        # expressions to make sure they work independently.
        remove = []
        for key in sys.modules:
            if key not in self._sys_modules:
                remove.append(key)  # can't delete in place while we iterate

        for key in remove:
            del sys.modules[key]

    def _check_spec(self, module, name, is_package):
        self.assertIsInstance(module, types.ModuleType)
        self.assertEqual(module.__name__, name)
        self.assertEqual(module.__spec__.name, name)
        self.assertEqual(module.__spec__.submodule_search_locations,
                         [] if is_package else None)
        self.assertFalse(module.__spec__.has_location)
        self.assertIn('generated Rachis API', repr(module))

    def _check_plugin(self, module):
        self._check_spec(module, 'rachis.plugins.dummy_plugin', True)
        self._check_methods(module.methods)
        self._check_visualizers(module.visualizers)
        self.assertEqual(set(x for x in dir(module) if not x.startswith('_')),
                         {'visualizers', 'methods', 'actions', 'pipelines'})

    def _check_methods(self, module):
        self._check_spec(module, 'rachis.plugins.dummy_plugin.methods', False)
        self.assertTrue(hasattr(module, 'concatenate_ints'))
        self.assertFalse(hasattr(module, 'most_common_viz'))

        self.assertIsInstance(module.concatenate_ints, rachis.sdk.Action)

    def _check_visualizers(self, module):
        self._check_spec(
            module, 'rachis.plugins.dummy_plugin.visualizers', False)
        self.assertTrue(hasattr(module, 'most_common_viz'))
        self.assertFalse(hasattr(module, 'concatenate_ints'))
        self.assertIsInstance(module.most_common_viz, rachis.sdk.Action)

    def test_import_root(self):
        import rachis.plugins.dummy_plugin
        self._check_plugin(rachis.plugins.dummy_plugin)

    def test_import_root_from(self):
        from rachis.plugins import dummy_plugin
        self._check_plugin(dummy_plugin)

    def test_import_methods(self):
        import rachis.plugins.dummy_plugin.methods
        self._check_methods(rachis.plugins.dummy_plugin.methods)

    def test_import_visualizers(self):
        import rachis.plugins.dummy_plugin.visualizers
        self._check_visualizers(rachis.plugins.dummy_plugin.visualizers)

    def test_import_methods_from(self):
        from rachis.plugins.dummy_plugin import methods
        self._check_methods(methods)

    def test_import_visualizers_from(self):
        from rachis.plugins.dummy_plugin import visualizers
        self._check_visualizers(visualizers)

    def test_import_non_plugin(self):
        with self.assertRaises(ImportError):
            import rachis.plugins.dummy_not_plugin  # noqa

    def test_import_non_action(self):
        with self.assertRaises(ImportError):
            import rachis.plugins.dummy_plugin.non_action  # noqa

    def test_import_side_module(self):
        # Certain implementations of __PATH__ can cause a module to load
        # siblings (__PATH__ = ['.'] for example)
        import rachis.metadata
        self.assertIsInstance(rachis.metadata, types.ModuleType)
        with self.assertRaises(ImportError):
            import rachis.plugins.metadata  # noqa

    def test_import_too_deep(self):
        with self.assertRaises(ImportError):
            import rachis.plugins.dummy_plugin.methods.too_deep  # noqa

    def test_import_non_module(self):
        with self.assertRaises(ImportError):
            import rachis.plugins.dummy_plugin.methods.concatenate_ints  # noqa

    def test_reload_fails(self):
        import rachis.plugins.dummy_plugin
        with self.assertRaises(ImportError):
            importlib.reload(rachis.plugins.dummy_plugin)

    def test_import_base_format_stores_checksums(self):
        from rachis import Artifact
        from rachis.core.testing.type import IntSequence2
        from rachis.core.testing.format import IntSequenceFormat
        from rachis.core.archive import Archiver
        from rachis.plugin.util import transform

        ff = transform([1, 2, 3,], to_type=IntSequenceFormat)
        ff2 = Artifact.import_data(IntSequence2, ff,
                                   view_type=IntSequenceFormat)

        # If the checksum is not stored, this will raise a KeyError
        archive = Archiver.get_archive(ff2._archiver.path)
        action = archive.load_action_yaml()['action']['manifest']
        self.assertIn('md5sum', action[0].keys())
        # This ensures that the checksum is exactly what we expect and will
        # catch empty strings or other weird values that could arise
        self.assertEqual(action[0]['md5sum'],
                         'c0710d6b4f15dfa88f600b0e6b624077')


class TestArtifactAPIUsage(unittest.TestCase):
    def setUp(self):
        # TODO standardize temporary directories created by QIIME 2
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')
        self.plugin = get_dummy_plugin()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_basic(self):
        action = self.plugin.actions['concatenate_ints']
        use = ArtifactAPIUsage()
        action.examples['concatenate_ints_simple'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

# This example demonstrates basic usage.
ints_d, = dummy_plugin_actions.concatenate_ints(
    ints1=ints_a,
    ints2=ints_b,
    ints3=ints_c,
    int1=4,
    int2=2,
)"""
        self.assertEqual(exp, use.render())

    def test_chained(self):
        action = self.plugin.actions['concatenate_ints']
        use = ArtifactAPIUsage()
        action.examples['concatenate_ints_complex'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

# This example demonstrates chained usage (pt 1).
ints_d, = dummy_plugin_actions.concatenate_ints(
    ints1=ints_a,
    ints2=ints_b,
    ints3=ints_c,
    int1=4,
    int2=2,
)
# This example demonstrates chained usage (pt 2).
concatenated_ints, = dummy_plugin_actions.concatenate_ints(
    ints1=ints_d,
    ints2=ints_b,
    ints3=ints_c,
    int1=41,
    int2=0,
)"""
        self.assertEqual(exp, use.render())

    def test_dereferencing(self):
        action = self.plugin.actions['typical_pipeline']
        use = ArtifactAPIUsage()
        action.examples['typical_pipeline_simple'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

action_results = dummy_plugin_actions.typical_pipeline(
    int_sequence=ints,
    mapping=mapper,
    do_extra_thing=True,
)
out_map = action_results.out_map
left = action_results.left
right = action_results.right
left_viz_viz = action_results.left_viz
right_viz_viz = action_results.right_viz"""
        self.assertEqual(exp, use.render())

    def test_chained_dereferencing(self):
        action = self.plugin.actions['typical_pipeline']
        use = ArtifactAPIUsage()
        action.examples['typical_pipeline_complex'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

action_results = dummy_plugin_actions.typical_pipeline(
    int_sequence=ints1,
    mapping=mapper1,
    do_extra_thing=True,
)
out_map1 = action_results.out_map
left1 = action_results.left
right1 = action_results.right
left_viz1_viz = action_results.left_viz
right_viz1_viz = action_results.right_viz
action_results = dummy_plugin_actions.typical_pipeline(
    int_sequence=left1,
    mapping=out_map1,
    do_extra_thing=False,
)
out_map2 = action_results.out_map
left2 = action_results.left
right2 = action_results.right
left_viz2_viz = action_results.left_viz
right_viz2_viz = action_results.right_viz"""
        self.assertEqual(exp, use.render())

    def test_metadata_merging(self):
        action = self.plugin.actions['identity_with_metadata']
        use = ArtifactAPIUsage()
        action.examples['identity_with_metadata_merging'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

md3_md = md1_md.merge(md2_md)
out, = dummy_plugin_actions.identity_with_metadata(
    ints=ints,
    metadata=md3_md,
)"""
        self.assertEqual(exp, use.render())

    def test_metadata_column_from_helper(self):
        action = self.plugin.actions['identity_with_metadata_column']
        use = ArtifactAPIUsage()
        action.examples['identity_with_metadata_column_get_mdc'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

mdc_mdc = md_md.get_column('a')
out, = dummy_plugin_actions.identity_with_metadata_column(
    ints=ints,
    metadata=mdc_mdc,
)"""
        self.assertEqual(exp, use.render())

    def test_optional_inputs(self):
        action = self.plugin.actions['optional_artifacts_method']
        use = ArtifactAPIUsage()
        action.examples['optional_inputs'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

output1, = dummy_plugin_actions.optional_artifacts_method(
    ints=ints,
    num1=1,
)
output2, = dummy_plugin_actions.optional_artifacts_method(
    ints=ints,
    num1=1,
    num2=2,
)
output3, = dummy_plugin_actions.optional_artifacts_method(
    ints=ints,
    num1=1,
    num2=None,
)
output4, = dummy_plugin_actions.optional_artifacts_method(
    ints=ints,
    optional1=output3,
    num1=3,
    num2=4,
)"""

        self.assertEqual(exp, use.render())

    def test_artifact_collection_dict_of_ints(self):
        action = self.plugin.actions['dict_of_ints']
        use = ArtifactAPIUsage(enable_assertions=True)
        action.examples['collection_dict_of_ints'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions
import re

out_artifact_collection, = dummy_plugin_actions.dict_of_ints(
    ints=ints_artifact_collection,
)
if str(out_artifact_collection['Foo'].type) != 'SingleInt':
    raise AssertionError
hits = sorted(out_artifact_collection['Foo']._archiver.data_dir.glob('file1.txt'))
if len(hits) != 1:
    raise ValueError
target = hits[0].read_text()
match = re.search('1', target, flags=re.MULTILINE)
if match is None:
    raise AssertionError"""  # noqa: E501
        self.assertEqual(exp, use.render())

    def test_collection_of_visualizations(self):
        action = self.plugin.actions['viz_collection_pipeline']
        use = ArtifactAPIUsage(enable_assertions=True)
        action.examples['collection_of_visualizations'](use)
        exp = """\
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

visualizations_viz_collection, = dummy_plugin_actions.viz_collection_pipeline(
    ints=ints,
)
if str(visualizations_viz_collection.type) != 'Collection[Visualization]':
    raise AssertionError"""

        self.assertEqual(exp, use.render())

    def test_construct_and_access_collection(self):
        action = self.plugin.actions['dict_of_ints']
        use = ArtifactAPIUsage()
        action.examples['construct_and_access_collection'](use)
        exp = """\
from rachis import ResultCollection
import rachis.plugins.dummy_plugin.actions as dummy_plugin_actions

rc_in_artifact_collection = ResultCollection({
    'a': ints_a,
    'b': ints_b,
})
rc_out_artifact_collection, = dummy_plugin_actions.dict_of_ints(
    ints=rc_in_artifact_collection,
)
ints_b_from_collection = rc_out_artifact_collection['b']"""

        self.assertEqual(exp, use.render())


if __name__ == '__main__':
    unittest.main()
