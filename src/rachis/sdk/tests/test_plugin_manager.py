# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import unittest

import rachis.plugin
import rachis.sdk
from rachis.plugin.plugin import (SemanticTypeRecord, FormatRecord,
                                  ArtifactClassRecord)
from rachis.sdk.plugin_manager import GetFormatFilters, DuplicatePluginError

from rachis.core.testing.type import (IntSequence1, IntSequence2, IntSequence3,
                                      Mapping, FourInts, Kennel, Dog, Cat,
                                      SingleInt, C1, C2, C3, Foo, Bar, Baz,
                                      AscIntSequence, Squid, Octopus,
                                      Cuttlefish)

from rachis.core.testing.format import (Cephalapod, IntSequenceDirectoryFormat,
                                        MappingDirectoryFormat,
                                        IntSequenceV2DirectoryFormat,
                                        IntSequenceFormatV2,
                                        IntSequenceMultiFileDirectoryFormat,
                                        FourIntsDirectoryFormat,
                                        IntSequenceFormat,
                                        RedundantSingleIntDirectoryFormat,
                                        EchoFormat,
                                        EchoDirectoryFormat,
                                        CephalapodDirectoryFormat,
                                        ImportableOnlyFormat,
                                        ExportableOnlyFormat,
                                        FirstStepFormat,
                                        SecondStepFormat,
                                        ThirdStepFormat,
                                        FourthStepFormat,
                                        FifthStepFormat
)

from rachis.core.testing.validator import (validator_example_null1,
                                           validate_ascending_seq,
                                           validator_example_null2)

from rachis.core.testing.util import get_dummy_plugin

from rachis.core.testing.plugin import is1_use, is2_use


class TestPluginManager(unittest.TestCase):
    def setUp(self):
        # PluginManager is a singleton so there's no issue creating it again.
        self.pm = rachis.sdk.PluginManager()
        self.plugin = get_dummy_plugin()
        self.other_plugin = self.pm.plugins['other-plugin']

    def test_plugin_name_clash(self):
        with self.assertRaises(DuplicatePluginError):
            self.pm.add_plugin(self.plugin)

    def test_plugins(self):
        plugins = self.pm.plugins

        exp = {
            'dummy-plugin': self.plugin,
            'other-plugin': self.other_plugin
        }
        self.assertEqual(plugins, exp)

    def test_validators(self):
        self.assertEqual({Kennel[Dog], Kennel[Cat], AscIntSequence, Squid,
                          Octopus, Cuttlefish},
                         set(self.pm.validators))

        self.assertEqual(
            set([r.validator for r in
                self.pm.validators[Kennel[Dog]]._validators]),
            {validator_example_null1, validator_example_null2})

        self.assertEqual(
            [r.validator for r in self.pm.validators[Kennel[Cat]]._validators],
            [validator_example_null1])

        self.assertEqual(
            [r.validator
             for r in self.pm.validators[AscIntSequence]._validators],
            [validate_ascending_seq])

    def test_type_fragments(self):
        types = self.pm.type_fragments

        exp = {
            'IntSequence1':   SemanticTypeRecord(semantic_type=IntSequence1,
                                                 plugin=self.plugin),
            'IntSequence2':   SemanticTypeRecord(semantic_type=IntSequence2,
                                                 plugin=self.plugin),
            'IntSequence3':   SemanticTypeRecord(semantic_type=IntSequence3,
                                                 plugin=self.plugin),
            'Mapping':        SemanticTypeRecord(semantic_type=Mapping,
                                                 plugin=self.plugin),
            'FourInts':       SemanticTypeRecord(semantic_type=FourInts,
                                                 plugin=self.plugin),
            'Kennel':         SemanticTypeRecord(semantic_type=Kennel,
                                                 plugin=self.plugin),
            'Dog':            SemanticTypeRecord(semantic_type=Dog,
                                                 plugin=self.plugin),
            'Cat':            SemanticTypeRecord(semantic_type=Cat,
                                                 plugin=self.plugin),
            'SingleInt':      SemanticTypeRecord(semantic_type=SingleInt,
                                                 plugin=self.plugin),
            'C1':             SemanticTypeRecord(semantic_type=C1,
                                                 plugin=self.plugin),
            'C2':             SemanticTypeRecord(semantic_type=C2,
                                                 plugin=self.plugin),
            'C3':             SemanticTypeRecord(semantic_type=C3,
                                                 plugin=self.plugin),
            'Foo':            SemanticTypeRecord(semantic_type=Foo,
                                                 plugin=self.plugin),
            'Bar':            SemanticTypeRecord(semantic_type=Bar,
                                                 plugin=self.plugin),
            'Baz':            SemanticTypeRecord(semantic_type=Baz,
                                                 plugin=self.plugin),
            'AscIntSequence': SemanticTypeRecord(semantic_type=AscIntSequence,
                                                 plugin=self.plugin),
            'Squid':          SemanticTypeRecord(semantic_type=Squid,
                                                 plugin=self.plugin),
            'Octopus':        SemanticTypeRecord(semantic_type=Octopus,
                                                 plugin=self.plugin),
            'Cuttlefish':     SemanticTypeRecord(semantic_type=Cuttlefish,
                                                 plugin=self.plugin),
        }

        self.assertEqual(types, exp)

    def test_get_semantic_types(self):
        artifact_classes = self.pm.get_semantic_types()

        is1 = ArtifactClassRecord(
            semantic_type=IntSequence1,
            format=IntSequenceDirectoryFormat,
            plugin=self.plugin,
            description="The first IntSequence",
            examples={'IntSequence1 import example': is1_use},
            type_expression=IntSequence1)
        is2 = ArtifactClassRecord(
            semantic_type=IntSequence2,
            format=IntSequenceV2DirectoryFormat,
            plugin=self.plugin,
            description="The second IntSequence",
            examples={'IntSequence2 import example': is2_use},
            type_expression=IntSequence2)
        is3 = ArtifactClassRecord(semantic_type=IntSequence3,
                                  format=IntSequenceMultiFileDirectoryFormat,
                                  plugin=self.plugin,
                                  description="",
                                  examples={},
                                  type_expression=IntSequence3)

        kd = ArtifactClassRecord(semantic_type=Kennel[Dog],
                                 format=MappingDirectoryFormat,
                                 plugin=self.plugin,
                                 description="",
                                 examples={},
                                 type_expression=Kennel[Dog])

        kc = ArtifactClassRecord(semantic_type=Kennel[Cat],
                                 format=MappingDirectoryFormat,
                                 plugin=self.plugin,
                                 description="",
                                 examples={},
                                 type_expression=Kennel[Cat])

        self.assertLessEqual(
            {str(e.semantic_type) for e in [is1, is2, is3, kd, kc]},
            artifact_classes.keys())

        self.assertEqual(is1, artifact_classes['IntSequence1'])
        self.assertEqual(is2, artifact_classes['IntSequence2'])
        self.assertEqual(is3, artifact_classes['IntSequence3'])

        self.assertNotIn('Cat', artifact_classes)
        self.assertNotIn('Dog', artifact_classes)
        self.assertNotIn('Kennel', artifact_classes)

        self.assertIn('Kennel[Dog]', artifact_classes)
        self.assertIn('Kennel[Cat]', artifact_classes)

    # TODO: add tests for type/directory/transformer registrations
    def test_get_formats_no_type_or_filter(self):
        exp = {
            'IntSequenceFormat':
                FormatRecord(format=IntSequenceFormat,
                             plugin=self.plugin),
            'IntSequenceDirectoryFormat':
                FormatRecord(format=IntSequenceDirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceFormatV2':
                FormatRecord(format=IntSequenceFormatV2,
                             plugin=self.plugin),
            'IntSequenceV2DirectoryFormat':
                FormatRecord(format=IntSequenceV2DirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin),
            'RedundantSingleIntDirectoryFormat':
                FormatRecord(format=RedundantSingleIntDirectoryFormat,
                             plugin=self.plugin),
            'FourIntsDirectoryFormat':
                FormatRecord(format=FourIntsDirectoryFormat,
                             plugin=self.plugin),
            'EchoFormat':
                FormatRecord(format=EchoFormat,
                             plugin=self.plugin),
            'EchoDirectoryFormat':
                FormatRecord(format=EchoDirectoryFormat,
                             plugin=self.plugin),
            'MappingDirectoryFormat':
                FormatRecord(format=MappingDirectoryFormat,
                             plugin=self.plugin),
            'Cephalapod':
                FormatRecord(format=Cephalapod, plugin=self.plugin),
            'CephalapodDirectoryFormat':
                FormatRecord(format=CephalapodDirectoryFormat,
                             plugin=self.plugin),
            'ImportableOnlyFormat':
                FormatRecord(format=ImportableOnlyFormat,
                             plugin=self.plugin),
            'ExportableOnlyFormat':
                FormatRecord(format=ExportableOnlyFormat,
                             plugin=self.plugin),
            'FirstStepFormat':
                FormatRecord(format=FirstStepFormat,
                             plugin=self.plugin),
            'SecondStepFormat':
                FormatRecord(format=SecondStepFormat,
                             plugin=self.plugin),
            'ThirdStepFormat':
                FormatRecord(format=ThirdStepFormat,
                             plugin=self.plugin),
            'FourthStepFormat':
                FormatRecord(format=FourthStepFormat,
                             plugin=self.plugin),
            'FifthStepFormat':
                FormatRecord(format=FifthStepFormat,
                             plugin=self.plugin),
        }

        obs = self.pm.get_formats()

        self.assertEqual(obs, exp)

    def test_get_formats_SFDF(self):
        exp = {
            'IntSequenceFormat':
                FormatRecord(format=IntSequenceFormat,
                             plugin=self.plugin),
            'IntSequenceFormatV2':
                FormatRecord(format=IntSequenceFormatV2,
                             plugin=self.plugin),
            'IntSequenceDirectoryFormat':
                FormatRecord(format=IntSequenceDirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceV2DirectoryFormat':
                FormatRecord(format=IntSequenceV2DirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin),
            'ImportableOnlyFormat':
                FormatRecord(format=ImportableOnlyFormat,
                             plugin=self.plugin),
            'ExportableOnlyFormat':
                FormatRecord(format=ExportableOnlyFormat,
                             plugin=self.plugin),
        }

        obs = self.pm.get_formats(semantic_type='IntSequence1')

        self.assertEqual(exp, obs)

    def test_get_formats_SFDF_EXPORTABLE(self):
        exp = {
            'IntSequenceFormat':
                FormatRecord(format=IntSequenceFormat,
                             plugin=self.plugin),
            'IntSequenceFormatV2':
                FormatRecord(format=IntSequenceFormatV2,
                             plugin=self.plugin),
            'IntSequenceDirectoryFormat':
                FormatRecord(format=IntSequenceDirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceV2DirectoryFormat':
                FormatRecord(format=IntSequenceV2DirectoryFormat,
                             plugin=self.plugin),
            'ExportableOnlyFormat':
                FormatRecord(format=ExportableOnlyFormat,
                             plugin=self.plugin)
        }

        obs = self.pm.get_formats(filter=GetFormatFilters.EXPORTABLE,
                                  semantic_type=IntSequence1)

        self.assertEqual(exp, obs)

    def test_get_formats_SFDF_IMPORTABLE(self):
        exp = {
            'IntSequenceFormat':
                FormatRecord(format=IntSequenceFormat,
                             plugin=self.plugin),
            'IntSequenceDirectoryFormat':
                FormatRecord(format=IntSequenceDirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin),
            'ImportableOnlyFormat':
                FormatRecord(format=ImportableOnlyFormat,
                             plugin=self.plugin)
        }

        obs = self.pm.get_formats(filter=GetFormatFilters.IMPORTABLE,
                                  semantic_type=IntSequence1)

        self.assertEqual(exp, obs)

    def test_get_formats_DF(self):
        exp = {
            'IntSequenceFormat':
                FormatRecord(format=IntSequenceFormat,
                             plugin=self.plugin),
            'IntSequenceFormatV2':
                FormatRecord(format=IntSequenceFormatV2,
                             plugin=self.plugin),
            'IntSequenceDirectoryFormat':
                FormatRecord(format=IntSequenceDirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceV2DirectoryFormat':
                FormatRecord(format=IntSequenceV2DirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin)
        }

        obs = self.pm.get_formats(semantic_type='IntSequence3')

        self.assertEqual(exp, obs)

    def test_get_formats_DF_EXPORTABLE(self):
        exp = {
            'IntSequenceFormat':
                FormatRecord(format=IntSequenceFormat,
                             plugin=self.plugin),
            'IntSequenceDirectoryFormat':
                FormatRecord(format=IntSequenceDirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin)
        }

        obs = self.pm.get_formats(filter=GetFormatFilters.EXPORTABLE,
                                  semantic_type=IntSequence3)

        self.assertEqual(exp, obs)

    def test_get_formats_DF_exportable_str(self):
        exp = {
            'IntSequenceFormat':
                FormatRecord(format=IntSequenceFormat,
                             plugin=self.plugin),
            'IntSequenceDirectoryFormat':
                FormatRecord(format=IntSequenceDirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin)
        }

        obs = self.pm.get_formats(filter="EXPORTABLE",
                                  semantic_type=IntSequence3)

        self.assertEqual(exp, obs)

    def test_get_formats_DF_IMPORTABLE(self):
        exp = {
            'IntSequenceFormatV2':
                FormatRecord(format=IntSequenceFormatV2,
                             plugin=self.plugin),
            'IntSequenceV2DirectoryFormat':
                FormatRecord(format=IntSequenceV2DirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin)
        }

        obs = self.pm.get_formats(filter=GetFormatFilters.IMPORTABLE,
                                  semantic_type=IntSequence3)

        self.assertEqual(exp, obs)

    def test_get_formats_DF_importable_str(self):
        exp = {
            'IntSequenceFormatV2':
                FormatRecord(format=IntSequenceFormatV2,
                             plugin=self.plugin),
            'IntSequenceV2DirectoryFormat':
                FormatRecord(format=IntSequenceV2DirectoryFormat,
                             plugin=self.plugin),
            'IntSequenceMultiFileDirectoryFormat':
                FormatRecord(format=IntSequenceMultiFileDirectoryFormat,
                             plugin=self.plugin)
        }

        obs = self.pm.get_formats(filter="IMPORTABLE",
                                  semantic_type=IntSequence3)

        self.assertEqual(exp, obs)

    def test_get_formats_invalid_type(self):
        with self.assertRaisesRegex(ValueError, "No formats associated"):
            self.pm.get_formats(semantic_type='Random[Frequency]')

    def test_get_formats_invalid_filter(self):
        with self.assertRaisesRegex(ValueError, "filter.*is not valid"):
            self.pm.get_formats(filter="xyz")

    def test_importable_formats_property(self):
        imp_f = self.pm.importable_formats
        self.assertTrue(isinstance(imp_f, dict))
        # spot check for a few formats that should be present
        self.assertTrue('IntSequenceFormatV2' in imp_f)
        self.assertTrue('CephalapodDirectoryFormat' in imp_f)
        self.assertTrue('ImportableOnlyFormat' in imp_f)
        # spot check one that shouldn't be here
        self.assertFalse('ExportableOnlyFormat' in imp_f)

    def test_exportable_formats_property(self):
        exp_f = self.pm.exportable_formats
        self.assertTrue(isinstance(exp_f, dict))
        # spot check for a few formats that should be present
        self.assertTrue('IntSequenceDirectoryFormat' in exp_f)
        self.assertTrue('IntSequenceV2DirectoryFormat' in exp_f)
        self.assertTrue('ExportableOnlyFormat' in exp_f)
        # spot check one that shouldn't be here
        self.assertFalse('ImportableOnlyFormat' in exp_f)

    def test_deprecated_type_formats(self):
        # PluginManager.type_formats was replaced with
        # PluginManager.artifact_classes. For backward compatibility the
        # PluginManager.type_formats property returns the plugin manager's
        # artifact_classes
        self.assertEqual(self.pm.type_formats,
                         list(self.pm.artifact_classes.values()))


if __name__ == '__main__':
    unittest.main()
