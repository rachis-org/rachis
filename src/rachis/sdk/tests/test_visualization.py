# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import json
import tempfile
import unittest
import uuid
import collections
import pathlib

import rachis.core.type
from rachis.sdk import Visualization, Artifact
from rachis.sdk.result import ResultMetadata
import rachis.core.archive as archive

from rachis.core.testing.type import Mapping, IntSequence1
from rachis.core.testing.visualizer import (
    mapping_viz, most_common_viz, multi_html_viz)
from rachis.core.testing.util import ArchiveTestingMixin, get_dummy_plugin


class TestVisualization(unittest.TestCase, ArchiveTestingMixin):
    def make_provenance_capture(self):
        # You can't actually import a visualization, but I won't tell
        # visualization if you don't...
        return archive.ImportProvenanceCapture()

    def setUp(self):
        # TODO standardize temporary directories created by QIIME 2
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')

        # Using `mapping_viz` because it produces multiple files, including a
        # nested directory.
        self.data_dir = os.path.join(self.test_dir.name, 'viz-output')
        os.mkdir(self.data_dir)
        mapping_viz(self.data_dir,
                    {'abc': 'foo', 'def': 'bar'},
                    {'ghi': 'baz', 'jkl': 'bazz'},
                    key_label='Key', value_label='Value')

    def tearDown(self):
        self.test_dir.cleanup()

    def test_private_constructor(self):
        with self.assertRaisesRegex(
                NotImplementedError,
                'Visualization constructor.*private.*Visualization.load'):
            Visualization()

    # Note on testing strategy below: many of the tests for `_from_data_dir`
    # and `load` are similar, with the exception that when `load`ing, the
    # visualization's UUID is known so more specific assertions can be
    # performed. While these tests appear somewhat redundant, they are
    # important because they exercise the same operations on Visualization
    # objects constructed from different sources, whose codepaths have very
    # different internal behavior. This internal behavior could be tested
    # explicitly but it is safer to test the public API behavior (e.g. as a
    # user would interact with the object) in case the internals change.

    def test_from_data_dir(self):
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())

        self.assertEqual(visualization.type, rachis.core.type.Visualization)
        self.assertIsInstance(visualization.uuid, uuid.UUID)

    def test_from_data_dir_and_save(self):
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())

        visualization.save(fp)

        root_dir = str(visualization.uuid)
        expected = {
            'VERSION',
            'checksums.sha512',
            'metadata.yaml',
            'data/index.html',
            'data/css/style.css',
            'provenance/metadata.yaml',
            'provenance/VERSION',
            'provenance/citations.bib',
            'provenance/conda-env.yaml',
            'provenance/action/action.yaml'
        }

        self.assertArchiveMembers(fp, root_dir, expected)

    def test_load(self):
        saved_visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        saved_visualization.save(fp)

        visualization = Visualization.load(fp)

        self.assertEqual(visualization.type, rachis.core.type.Visualization)
        self.assertEqual(visualization.uuid, saved_visualization.uuid)

    def test_load_and_save(self):
        fp1 = os.path.join(self.test_dir.name, 'visualization1.qzv')
        fp2 = os.path.join(self.test_dir.name, 'visualization2.qzv')
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        visualization.save(fp1)

        visualization = Visualization.load(fp1)
        # Overwriting its source file works.
        visualization.save(fp1)
        # Saving to a new file works.
        visualization.save(fp2)

        root_dir = str(visualization.uuid)
        expected = {
            'VERSION',
            'checksums.sha512',
            'metadata.yaml',
            'data/index.html',
            'data/css/style.css',
            'provenance/metadata.yaml',
            'provenance/VERSION',
            'provenance/citations.bib',
            'provenance/conda-env.yaml',
            'provenance/action/action.yaml'
        }

        self.assertArchiveMembers(fp1, root_dir, expected)

        root_dir = str(visualization.uuid)
        expected = {
            'VERSION',
            'checksums.sha512',
            'metadata.yaml',
            'data/index.html',
            'data/css/style.css',
            'provenance/metadata.yaml',
            'provenance/VERSION',
            'provenance/citations.bib',
            'provenance/conda-env.yaml',
            'provenance/action/action.yaml'
        }

        self.assertArchiveMembers(fp2, root_dir, expected)

    def test_roundtrip(self):
        fp1 = os.path.join(self.test_dir.name, 'visualization1.qzv')
        fp2 = os.path.join(self.test_dir.name, 'visualization2.qzv')
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        visualization.save(fp1)

        visualization1 = Visualization.load(fp1)
        visualization1.save(fp2)
        visualization2 = Visualization.load(fp2)

        self.assertEqual(visualization1.type, visualization2.type)
        self.assertEqual(visualization1.uuid, visualization2.uuid)

    def test_load_with_archive_filepath_modified(self):
        # Save a visualization for use in the following test case.
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        Visualization._from_data_dir(self.data_dir,
                                     self.make_provenance_capture()).save(fp)

        # Load the visualization from a filepath then save a different
        # visualization to the same filepath. Assert that both visualizations
        # access the correct data.
        #
        # `load` used to be lazy, only extracting data when it needed to (e.g.
        # when `save` or `get_index_paths` was called). This was buggy as the
        # filepath could have been deleted, or worse, modified to contain a
        # different .qzv file. Thus, the wrong archive could be extracted on
        # demand, or the archive could be missing altogether. There isn't an
        # easy cross-platform compatible way to solve this problem, so
        # Visualization.load is no longer lazy and always extracts its data
        # immediately. The real motivation for lazy loading was for quick
        # inspection of archives without extracting/copying data, so that API
        # is now provided through Visualization.peek.
        visualization1 = Visualization.load(fp)

        new_data_dir = os.path.join(self.test_dir.name, 'viz-output2')
        os.mkdir(new_data_dir)
        most_common_viz(new_data_dir, collections.Counter(range(42)))

        Visualization._from_data_dir(new_data_dir,
                                     self.make_provenance_capture()).save(fp)
        visualization2 = Visualization.load(fp)

        self.assertEqual(visualization1.get_index_paths(),
                         {'html': 'data/index.html'})
        self.assertEqual(visualization2.get_index_paths(),
                         {'html': 'data/index.html', 'tsv': 'data/index.tsv'})

    def test_extract(self):
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        visualization.save(fp)

        root_dir = str(visualization.uuid)
        # pathlib normalizes away the `.`, it doesn't matter, but this is the
        # implementation we're using, so let's test against that assumption.
        output_dir = pathlib.Path(self.test_dir.name) / 'viz-extract-test'
        result_dir = Visualization.extract(fp, output_dir=output_dir)
        self.assertEqual(result_dir, str(output_dir / root_dir))

        expected = {
            'VERSION',
            'checksums.sha512',
            'metadata.yaml',
            'data/index.html',
            'data/css/style.css',
            'provenance/metadata.yaml',
            'provenance/VERSION',
            'provenance/citations.bib',
            'provenance/conda-env.yaml',
            'provenance/action/action.yaml'
        }

        self.assertExtractedArchiveMembers(output_dir, root_dir, expected)

    def test_get_index_paths_single_load(self):
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        visualization.save(fp)
        visualization = Visualization.load(fp)

        actual = visualization.get_index_paths()
        expected = {'html': 'data/index.html'}
        self.assertEqual(actual, expected)

    def test_get_index_paths_single_from_data_dir(self):
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())

        actual = visualization.get_index_paths()
        expected = {'html': 'data/index.html'}
        self.assertEqual(actual, expected)

    def test_get_index_paths_multiple_load(self):
        data_dir = os.path.join(self.test_dir.name, 'mc-viz-output1')
        os.mkdir(data_dir)
        most_common_viz(data_dir,
                        collections.Counter(range(42)))
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization = Visualization._from_data_dir(
            data_dir, self.make_provenance_capture())
        visualization.save(fp)
        visualization = Visualization.load(fp)

        actual = visualization.get_index_paths()
        expected = {'html': 'data/index.html',
                    'tsv': 'data/index.tsv'}
        self.assertEqual(actual, expected)

    def test_get_index_paths_multiple_from_data_dir(self):
        data_dir = os.path.join(self.test_dir.name, 'mc-viz-output2')
        os.mkdir(data_dir)
        most_common_viz(data_dir, collections.Counter(range(42)))
        visualization = Visualization._from_data_dir(
            data_dir, self.make_provenance_capture())

        actual = visualization.get_index_paths()
        expected = {'html': 'data/index.html',
                    'tsv': 'data/index.tsv'}
        self.assertEqual(actual, expected)

    def test_get_index_paths_multiple_html_load(self):
        data_dir = os.path.join(self.test_dir.name, 'multi-html-viz1')
        os.mkdir(data_dir)
        multi_html_viz(data_dir, [1, 42])

        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization = Visualization._from_data_dir(
            data_dir, self.make_provenance_capture())
        visualization.save(fp)
        visualization = Visualization.load(fp)

        with self.assertRaises(ValueError):
            visualization.get_index_paths()

    def test_get_index_paths_multiple_html_from_data_dir(self):
        data_dir = os.path.join(self.test_dir.name, 'multi-html-viz2')
        os.mkdir(data_dir)
        multi_html_viz(data_dir, [1, 42])

        visualization = Visualization._from_data_dir(
            data_dir, self.make_provenance_capture())

        with self.assertRaises(ValueError):
            visualization.get_index_paths()

    def test_get_index_paths_relative_false(self):
        data_dir = os.path.join(self.test_dir.name, 'mc-viz-output2')
        os.mkdir(data_dir)
        most_common_viz(data_dir, collections.Counter(range(42)))
        visualization = Visualization._from_data_dir(
            data_dir, self.make_provenance_capture())

        def get_abs_path(rel):
            return str(visualization._archiver.data_dir /
                       pathlib.Path(rel).relative_to('data'))
        actual = visualization.get_index_paths(relative=False)
        expected = {'html': get_abs_path('data/index.html'),
                    'tsv': get_abs_path('data/index.tsv')}
        self.assertEqual(actual, expected)

    def test_peek(self):
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization.save(fp)

        metadata = Visualization.peek(fp)

        self.assertIsInstance(metadata, ResultMetadata)
        self.assertEqual(metadata.type, 'Visualization')
        self.assertEqual(metadata.uuid, str(visualization.uuid))
        self.assertIsNone(metadata.format)

    def test_eq_identity(self):
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())

        self.assertEqual(visualization, visualization)

    def test_eq_same_uuid(self):
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization1 = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        visualization1.save(fp)

        visualization2 = Visualization.load(fp)

        self.assertEqual(visualization1, visualization2)

    def test_ne_same_data_different_uuid(self):
        visualization1 = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        visualization2 = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())

        self.assertNotEqual(visualization1, visualization2)

    def test_ne_different_data_different_uuid(self):
        visualization1 = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())

        data_dir = os.path.join(self.test_dir.name, 'mc-viz-output1')
        os.mkdir(data_dir)
        most_common_viz(data_dir,
                        collections.Counter(range(42)))
        visualization2 = Visualization._from_data_dir(
            data_dir, self.make_provenance_capture())

        self.assertNotEqual(visualization1, visualization2)

    def test_ne_subclass_same_uuid(self):
        class VisualizationSubclass(Visualization):
            pass

        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization1 = VisualizationSubclass._from_data_dir(
            self.data_dir, self.make_provenance_capture())
        visualization1.save(fp)

        visualization2 = Visualization.load(fp)

        self.assertNotEqual(visualization1, visualization2)
        self.assertNotEqual(visualization2, visualization1)

    def test_ne_different_type_same_uuid(self):
        visualization = Visualization._from_data_dir(
            self.data_dir, self.make_provenance_capture())

        class Faker:
            @property
            def uuid(self):
                return visualization.uuid

        faker = Faker()

        self.assertNotEqual(visualization, faker)


class TestMakeReport(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')
        # load dummy plugin
        self.plugin = get_dummy_plugin()
        # create visualizations via the dummy plugin visualizers once for
        # use across tests
        artifact1 = Artifact.import_data(Mapping, {'a': '1'})
        artifact2 = Artifact.import_data(Mapping, {'b': '2'})
        mapping_res = self.plugin.visualizers['mapping_viz'](
            artifact1, artifact2, 'Key', 'Value')
        self.viz1 = mapping_res[0]

        ints_art = Artifact.import_data(IntSequence1, [0, 1, 1])
        most_common_res = self.plugin.visualizers['most_common_viz'](ints_art)
        self.viz2 = most_common_res[0]

        def template(destination, index):
            with open(os.path.join(destination, 'index.html'), 'w') as fh:
                fh.write('<html><body>Report</body></html>')

        self.template = template

    def tearDown(self):
        self.test_dir.cleanup()

    def test_make_report_simple(self):
        template_index = None

        def template(destination, index):
            nonlocal template_index
            template_index = index
            # write a simple index.html at the report root
            with open(os.path.join(destination, 'index.html'), 'w') as fh:
                fh.write('<html><body>Report</body></html>')

        collection = {'first': self.viz1, 'second': self.viz2}
        report_viz = Visualization.make_report(
            template,
            collection,
            descriptions={'first': 'A description for the first plot.'})
        res_dir = str(report_viz._archiver.data_dir.parent)

        # verify index.html at top level and subfigures index.json
        top_index = os.path.join(res_dir, 'data', 'index.html')
        subfigures_index = os.path.join(res_dir, 'data', 'subfigures',
                                        'index.json')

        self.assertTrue(os.path.exists(top_index))
        self.assertTrue(os.path.exists(subfigures_index))

        with open(subfigures_index) as fh:
            json_index = json.load(fh)

        # both keys present
        self.assertIn('first', json_index)
        self.assertIn('second', json_index)
        # Assert that the template recieved the same index as what was written
        self.assertEqual(json_index, template_index)

        self.assertEqual(json_index['first']['description'],
                         'A description for the first plot.')
        self.assertNotIn('description', json_index['second'])

    def test_make_report_nested_hoist(self):
        # create an inner report from viz2 alone
        # create visualization via dummy plugin
        inner_report = Visualization.make_report(
            self.template,
            {'inner': self.viz2},
            descriptions={'inner': 'A nested leaf description.'})
        inner_path = inner_report._archiver.data_dir.parent

        # inner report will have two entries in subfigures, index and viz2
        self.assertEqual(
            set(os.listdir(inner_path / 'data' / 'subfigures')),
            {'index.json', str(self.viz2.uuid)}
        )

        # outer report contains a visualization and a report
        collection = {'first': self.viz1, 'nested': inner_report}
        outer_report = Visualization.make_report(
            self.template,
            collection,
            descriptions={'nested': 'A description for the nested report.'})
        res_dir = outer_report._archiver.data_dir.parent

        subfigures_dir = os.path.join(res_dir, 'data', 'subfigures')
        index_fp = os.path.join(subfigures_dir, 'index.json')
        self.assertTrue(os.path.exists(index_fp))

        with open(index_fp) as fh:
            json_index = json.load(fh)

        # should contain both 'first' and 'nested'
        self.assertIn('first', json_index)
        self.assertIn('nested', json_index)

        # the inner report's subfigure should have been hoisted into top-level
        # subfigures directory: check that there exists at least two uuid dirs
        children = [name for name in os.listdir(subfigures_dir)
                    if os.path.isdir(os.path.join(subfigures_dir, name))]
        # expect 3 subfigure directories (viz1, nested, and hoisted inner)
        self.assertEqual(len(children), 3)

        # now assert that the inner report has a re-written index.json and
        # no subfigures in its directory
        inner, _ = os.path.split(json_index['nested']['index'])
        inner_subfigures = os.path.join(res_dir, 'data', inner, 'subfigures')
        # only index.json
        self.assertEqual(os.listdir(inner_subfigures), ['index.json'])
        with open(os.path.join(inner_subfigures, 'index.json')) as fh:
            inner_index = json.load(fh)

        self.assertEqual(inner_index['inner']['index'],
                         # the base dir has been updated to the parent dir
                         f'../{str(self.viz2.uuid)}/index.html')
        self.assertEqual(inner_index['inner']['description'],
                         'A nested leaf description.')
        self.assertEqual(json_index['nested']['description'],
                         'A description for the nested report.')

    def test_make_report_rejects_unknown_description(self):
        with self.assertRaisesRegex(ValueError, 'not in the collection'):
            Visualization.make_report(
                self.template,
                {'first': self.viz1},
                descriptions={'missing': 'This visualization does not exist.'})

    def test_make_report_rejects_non_text_description(self):
        with self.assertRaisesRegex(TypeError, 'must be plain text'):
            Visualization.make_report(
                self.template,
                {'first': self.viz1},
                descriptions={'first': ['This is not plain text.']})

    def test_make_report_without_descriptions(self):
        report = Visualization.make_report(
            self.template, {'first': self.viz1})

        self.assertIsInstance(report, Visualization)

if __name__ == '__main__':
    unittest.main()
