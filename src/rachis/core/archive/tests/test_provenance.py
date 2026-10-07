# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import unittest
import re
import unittest.mock as mock

import pandas as pd

import rachis
from rachis.plugins import dummy_plugin
from rachis.core.testing.format import FirstStepFormat
from rachis.core.testing.type import IntSequence1, Mapping
from rachis.core.util import load_action_yaml
import rachis.core.archive.provenance as provenance


class TestProvenanceIntegration(unittest.TestCase):
    def test_chain_with_metadata(self):
        df = pd.DataFrame({'a': ['1', '2', '3']},
                          index=pd.Index(['0', '1', '2'], name='feature ID'))

        a = rachis.Artifact.import_data('IntSequence1', [1, 2, 3])
        m = rachis.Metadata(df)
        mc = rachis.CategoricalMetadataColumn(df['a'])

        b = dummy_plugin.actions.identity_with_metadata(a, m).out
        c = dummy_plugin.actions.identity_with_metadata_column(b, mc).out

        p_dir = c._archiver.provenance_dir

        new_m = rachis.Metadata.load(
            str(p_dir / 'artifacts' / str(b.uuid) / 'action' / 'metadata.tsv'))

        pd.testing.assert_frame_equal(m.to_dataframe(), new_m.to_dataframe())

        with (p_dir / 'action' / 'metadata.tsv').open() as fh:
            self.assertEqual(
                fh.read(),
                'feature ID\ta\n#q2:types\tcategorical\n0\t1\n1\t2\n2\t3\n')

    def test_chain_with_artifact_metadata(self):
        metadata_artifact_1 = rachis.Artifact.import_data(
            'Mapping', {'a': 'foo', 'b': 'bar'})
        metadata_artifact_2 = rachis.Artifact.import_data(
            'Mapping', {'c': 'baz'})
        m = metadata_artifact_1.view(rachis.Metadata)
        mc = metadata_artifact_2.view(rachis.Metadata).get_column('c')

        a = rachis.Artifact.import_data('IntSequence1', [1, 2, 3])

        b = dummy_plugin.actions.identity_with_metadata(a, m).out
        c = dummy_plugin.actions.identity_with_metadata_column(b, mc).out

        p_dir = c._archiver.provenance_dir

        m_yaml_value = "%s:metadata.tsv" % metadata_artifact_1.uuid
        mc_yaml_value = "%s:metadata.tsv" % metadata_artifact_2.uuid

        # Check action files for uuid-metadata values
        with (p_dir / 'action' / 'action.yaml').open() as fh:
            self.assertIn(mc_yaml_value, fh.read())
        with (p_dir / 'artifacts' / str(b.uuid) / 'action' /
              'action.yaml').open() as fh:
            self.assertIn(m_yaml_value, fh.read())

        # Check that metadata is written out fully
        new_m = rachis.Metadata.load(
            str(p_dir / 'artifacts' / str(b.uuid) / 'action' / 'metadata.tsv'))

        pd.testing.assert_frame_equal(m.to_dataframe(), new_m.to_dataframe())

        # Check that provenance of originating metadata artifact exists
        self.assertTrue((p_dir / 'artifacts' / str(metadata_artifact_1.uuid) /
                         'action' / 'action.yaml').exists())
        self.assertTrue((p_dir / 'artifacts' / str(metadata_artifact_2.uuid) /
                         'action' / 'action.yaml').exists())

    def test_chain_with_merged_artifact_metadata(self):
        md_artifact1 = rachis.Artifact.import_data(
            'Mapping', {'a': 'foo', 'b': 'bar'})
        md_artifact2 = rachis.Artifact.import_data(
            'Mapping', {'c': 'baz'})
        md1 = md_artifact1.view(rachis.Metadata)
        md2 = md_artifact2.view(rachis.Metadata)
        merged_md = md1.merge(md2)
        merged_mdc = merged_md.get_column('c')

        a = rachis.Artifact.import_data('IntSequence1', [1, 2, 3])

        b = dummy_plugin.actions.identity_with_metadata(a, merged_md).out
        c = dummy_plugin.actions.identity_with_metadata_column(
            b, merged_mdc).out

        p_dir = c._archiver.provenance_dir

        yaml_value = "%s,%s:metadata.tsv" % (md_artifact1.uuid,
                                             md_artifact2.uuid)

        # Check action files for uuid-metadata values
        with (p_dir / 'action' / 'action.yaml').open() as fh:
            self.assertIn(yaml_value, fh.read())
        with (p_dir / 'artifacts' / str(b.uuid) / 'action' /
              'action.yaml').open() as fh:
            self.assertIn(yaml_value, fh.read())

        # Check that metadata is written out fully
        with (p_dir / 'action' / 'metadata.tsv').open() as fh:
            self.assertEqual(fh.read(),
                             'id\tc\n#q2:types\tcategorical\n0\tbaz\n')

        new_merged_md = rachis.Metadata.load(
            str(p_dir / 'artifacts' / str(b.uuid) / 'action' / 'metadata.tsv'))
        pd.testing.assert_frame_equal(new_merged_md.to_dataframe(),
                                      merged_md.to_dataframe())

        # Check that provenance of originating metadata artifacts exists
        self.assertTrue((p_dir / 'artifacts' / str(md_artifact1.uuid) /
                         'action' / 'action.yaml').exists())
        self.assertTrue((p_dir / 'artifacts' / str(md_artifact2.uuid) /
                         'action' / 'action.yaml').exists())

    def test_with_optional_artifacts(self):
        ints1 = rachis.Artifact.import_data(IntSequence1, [0, 42, 43])
        ints2 = rachis.Artifact.import_data(IntSequence1, [99, -22])

        # One optional artifact is provided (`optional1`) while `optional2` is
        # omitted.
        obs = dummy_plugin.actions.optional_artifacts_method(
            ints1, 42, optional1=ints2).output

        p_dir = obs._archiver.provenance_dir
        with (p_dir / 'action' / 'action.yaml').open() as fh:
            yaml = fh.read()

        self.assertIn('ints: %s' % ints1.uuid, yaml)
        self.assertIn('optional1: %s' % ints2.uuid, yaml)
        self.assertIn('optional2: null', yaml)
        self.assertIn('num1: 42', yaml)
        self.assertIn('num2: null', yaml)

        self.assertTrue((p_dir / 'artifacts' / str(ints1.uuid) /
                         'action' / 'action.yaml').exists())
        self.assertTrue((p_dir / 'artifacts' / str(ints2.uuid) /
                         'action' / 'action.yaml').exists())

    def test_output_name_different(self):
        ints = rachis.Artifact.import_data(IntSequence1, [0, 1, 2, 3])

        left, right = dummy_plugin.actions.split_ints(ints)

        left_p_dir = left._archiver.provenance_dir
        right_p_dir = right._archiver.provenance_dir

        with (left_p_dir / 'action' / 'action.yaml').open() as fh:
            left_yaml = fh.read()

        with (right_p_dir / 'action' / 'action.yaml').open() as fh:
            right_yaml = fh.read()

        self.assertNotEqual(left_yaml, right_yaml)
        self.assertIn('output-name: left', left_yaml)
        self.assertIn('output-name: right', right_yaml)

    def test_output_name_visualization(self):
        viz, = dummy_plugin.actions.no_input_viz()

        viz_p_dir = viz._archiver.provenance_dir

        with (viz_p_dir / 'action' / 'action.yaml').open() as fh:
            self.assertIn('output-name: visualization', fh.read())

    def test_no_output_name_import(self):
        ints = rachis.Artifact.import_data(IntSequence1, [0, 2, 4])
        ints_p_dir = ints._archiver.provenance_dir

        with (ints_p_dir / 'action' / 'action.yaml').open() as fh:
            self.assertNotIn('output-name:', fh.read())

    def test_pipeline_alias_of(self):
        ints = rachis.Artifact.import_data(IntSequence1, [1, 2, 3])
        mapping = rachis.Artifact.import_data(Mapping, {'foo': '42'})
        r = dummy_plugin.actions.typical_pipeline(ints, mapping, False)

        # mapping is a pass-through
        new_mapping = r.out_map
        new_mapping_p_dir = new_mapping._archiver.provenance_dir

        with (new_mapping_p_dir / 'action' / 'action.yaml').open() as fh:
            new_mapping_yaml = fh.read()

        # Basic sanity check
        self.assertIn('type: pipeline', new_mapping_yaml)
        self.assertIn('int_sequence: %s' % ints.uuid, new_mapping_yaml)
        self.assertIn('mapping: %s' % mapping.uuid, new_mapping_yaml)
        # Remembers the original mapping uuid
        self.assertIn('alias-of: %s' % mapping.uuid, new_mapping_yaml)

    def test_nested_pipeline_alias_of(self):
        ints = rachis.Artifact.import_data(IntSequence1, [1, 2, 3])
        mapping = rachis.Artifact.import_data(Mapping, {'foo': '42'})
        r = dummy_plugin.actions.pipelines_in_pipeline(ints, mapping)

        right_p_dir = r.right._archiver.provenance_dir

        with (right_p_dir / 'action' / 'action.yaml').open() as fh:
            right_yaml = fh.read()

        self.assertIn('type: pipeline', right_yaml)
        self.assertIn('action: pipelines_in_pipeline', right_yaml)
        self.assertIn('int_sequence: %s' % ints.uuid, right_yaml)

        match = re.search(r'alias\-of: ([a-zA-Z0-9\-]+)$', right_yaml,
                          flags=re.MULTILINE)
        first_alias = match.group(1)

        with (right_p_dir / 'artifacts' / first_alias / 'action' /
              'action.yaml').open() as fh:
            first_alias_yaml = fh.read()

        # Should be the same input
        self.assertIn('type: pipeline', first_alias_yaml)
        self.assertIn('int_sequence: %s' % ints.uuid, first_alias_yaml)
        self.assertIn('action: typical_pipeline', first_alias_yaml)

        match = re.search(r'alias\-of: ([a-zA-Z0-9\-]+)$', first_alias_yaml,
                          flags=re.MULTILINE)

        second_alias = match.group(1)

        with (right_p_dir / 'artifacts' / second_alias / 'action' /
              'action.yaml').open() as fh:
            actual_method_yaml = fh.read()

        self.assertIn('type: method', actual_method_yaml)
        self.assertIn('ints: %s' % ints.uuid, actual_method_yaml)
        self.assertIn('action: split_ints', actual_method_yaml)

    def test_unioned_primitives(self):
        r = dummy_plugin.actions.unioned_primitives(3, 2)

        prov_dir = r.out._archiver.provenance_dir

        with (prov_dir / 'action' / 'action.yaml').open() as fh:
            prov_yml = fh.read()

        self.assertIn('foo: 3', prov_yml)
        self.assertIn('bar: 2', prov_yml)

    @mock.patch('rachis.core.archive.provenance.tzlocal.get_localzone',
                side_effect=ValueError())
    def test_ts_to_date(self, mocked_tzlocal):
        q2_paper_date = 1563984000

        obs = str(provenance._ts_to_date(q2_paper_date))
        exp = "2019-07-24 16:00:00+00:00"

        self.assertEqual(obs, exp)
        self.assertTrue(mocked_tzlocal.called)

    def test_prov_rename(self):
        viz, = dummy_plugin.actions.no_input_viz()

        viz_p_dir = viz._archiver.provenance_dir
        self.assertTrue(viz_p_dir.exists())

    @mock.patch('rachis.core.path.ProvenancePath.rename',
                side_effect=FileExistsError)
    def test_prov_rename_file_exists(self, _):
        viz, = dummy_plugin.actions.no_input_viz()

        viz_p_dir = viz._archiver.provenance_dir

        with (viz_p_dir / 'action' / 'action.yaml').open() as fh:
            self.assertIn('output-name: visualization', fh.read())

    def test_transitive_import_records_each_transformer(self):
        '''
        Asserts that transitive transformations made at import time are
        correctly recorded in the imported artifact's "action.yaml".
        '''
        artifact = rachis.Artifact.import_data(
            'ThirdStep', FirstStepFormat(), view_type=FirstStepFormat
        )

        action = load_action_yaml(artifact._archiver.path)

        self.assertEqual(
            action['transformers']['output'],
            [
                {
                    'from': 'FirstStepFormat',
                    'to': 'SecondStepFormat',
                    'plugin': 'dummy-plugin',
                },
                {
                    'from': 'SecondStepFormat',
                    'to': 'ThirdStepFormat',
                    'plugin': 'dummy-plugin',
                },
            ],
        )


if __name__ == '__main__':
    unittest.main()
