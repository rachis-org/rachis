# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import tempfile
import unittest
import pathlib
import pytest
import subprocess

from rachis import Metadata
from rachis.sdk.plugin_manager import PluginManager
import rachis.core.type
from rachis.sdk import Result, Artifact, Visualization, ResultCollection
from rachis.sdk.result import ResultMetadata
from rachis.sdk.proxy import ProxyResult
from rachis.core.annotate import Signature
import rachis.core.archive as archive
import rachis.core.exceptions as exceptions

from rachis.core.testing.format import IntSequenceDirectoryFormat
from rachis.core.testing.type import (FourInts, SingleInt, IntSequence1,
                                      IntSequence2, Foo, Bar)
from rachis.core.testing.util import get_dummy_plugin, ArchiveTestingMixin
from rachis.core.testing.visualizer import mapping_viz
from rachis.core.util import set_permissions, OTHER_NO_WRITE


class TestResult(unittest.TestCase, ArchiveTestingMixin):
    def make_provenance_capture(self):
        # You can't actually import a visualization, but I won't tell
        # visualization if you don't...
        return archive.ImportProvenanceCapture()

    def setUp(self):
        # Ignore the returned dummy plugin object, just run this to verify the
        # plugin exists as the tests rely on it being loaded.
        get_dummy_plugin()

        # TODO standardize temporary directories created by QIIME 2
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')

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
                'Result constructor.*private.*Result.load'):
            Result()

    def test_alias_type_must_refine_realized_type(self):
        artifact = Artifact.import_data(Foo, 'foo', view_type=str)

        with self.assertRaisesRegex(
                TypeError,
                "Alias type Bar must be a subtype of realized result type "
                "Foo"):
            artifact._alias('output', None, None, Bar)

    def test_proxy_alias_type_must_refine_known_type(self):
        class Future:
            def result(self):
                raise AssertionError("Proxy alias type check blocked.")

        proxy = ProxyResult(Future(), 'output', Foo)

        with self.assertRaisesRegex(
                TypeError,
                "Alias type Bar must be a subtype of realized result type "
                "Foo"):
            proxy._alias('output', None, None, Bar)

    def test_load_artifact(self):
        saved_artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        saved_artifact.save(fp)

        artifact = Result.load(fp)

        self.assertIsInstance(artifact, Artifact)
        self.assertEqual(artifact.type, FourInts)
        self.assertEqual(artifact.uuid, saved_artifact.uuid)
        self.assertEqual(artifact.view(list), [-1, 42, 0, 43])

    def test_load_visualization(self):
        saved_visualization = Visualization._from_data_dir(
             self.data_dir, self.make_provenance_capture())
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        saved_visualization.save(fp)

        visualization = Result.load(fp)

        self.assertIsInstance(visualization, Visualization)
        self.assertEqual(visualization.type, rachis.core.type.Visualization)
        self.assertEqual(visualization.uuid, saved_visualization.uuid)

    def test_extract_artifact(self):
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        artifact.save(fp)

        root_dir = str(artifact.uuid)
        # pathlib normalizes away the `.`, it doesn't matter, but this is the
        # implementation we're using, so let's test against that assumption.
        output_dir = pathlib.Path(self.test_dir.name) / 'artifact-extract-test'
        result_dir = Result.extract(fp, output_dir=output_dir)
        self.assertEqual(result_dir, str(output_dir / root_dir))

        expected = {
            'VERSION',
            'checksums.sha512',
            'metadata.yaml',
            'data/file1.txt',
            'data/file2.txt',
            'data/nested/file3.txt',
            'data/nested/file4.txt',
            'provenance/metadata.yaml',
            'provenance/VERSION',
            'provenance/citations.bib',
            'provenance/conda-env.yaml',
            'provenance/action/action.yaml'
        }

        self.assertExtractedArchiveMembers(output_dir, root_dir, expected)

    def test_extract_visualization(self):
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization = Visualization._from_data_dir(
             self.data_dir, self.make_provenance_capture())
        visualization.save(fp)

        root_dir = str(visualization.uuid)
        output_dir = pathlib.Path(self.test_dir.name) / 'viz-extract-test'
        result_dir = Result.extract(fp, output_dir=output_dir)
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

    def test_peek_artifact(self):
        artifact = Artifact.import_data(FourInts, [0, 0, 42, 1000])
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        artifact.save(fp)

        metadata = Result.peek(fp)

        self.assertIsInstance(metadata, ResultMetadata)
        self.assertEqual(metadata.type, 'FourInts')
        self.assertEqual(metadata.uuid, str(artifact.uuid))
        self.assertEqual(metadata.format, 'FourIntsDirectoryFormat')

    def test_peek_visualization(self):
        visualization = Visualization._from_data_dir(
             self.data_dir, self.make_provenance_capture())
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        visualization.save(fp)

        metadata = Result.peek(fp)

        self.assertIsInstance(metadata, ResultMetadata)
        self.assertEqual(metadata.type, 'Visualization')
        self.assertEqual(metadata.uuid, str(visualization.uuid))
        self.assertIsNone(metadata.format)

    def test_save_artifact_auto_extension(self):
        artifact = Artifact.import_data(FourInts, [0, 0, 42, 1000])

        # Filename & extension endswith is matching (default).
        fp = os.path.join(self.test_dir.name, 'artifactqza')
        obs_fp = artifact.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifactqza.qza')

        # Filename & extension endswith is matching (non-default).
        fp = os.path.join(self.test_dir.name, 'artifacttxt')
        obs_fp = artifact.save(fp, 'txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifacttxt.txt')

        # No period in filename; no period in extension.
        fp = os.path.join(self.test_dir.name, 'artifact')
        obs_fp = artifact.save(fp, 'txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # No period in filename; multiple periods in extension.
        fp = os.path.join(self.test_dir.name, 'artifact')
        obs_fp = artifact.save(fp, '..txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # Single period in filename; no period in extension.
        fp = os.path.join(self.test_dir.name, 'artifact.')
        obs_fp = artifact.save(fp, 'txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # Single period in filename; single period in extension.
        fp = os.path.join(self.test_dir.name, 'artifact.')
        obs_fp = artifact.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # Single period in filename; multiple periods in extension.
        fp = os.path.join(self.test_dir.name, 'artifact.')
        obs_fp = artifact.save(fp, '..txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # Multiple periods in filename; single period in extension.
        fp = os.path.join(self.test_dir.name, 'artifact..')
        obs_fp = artifact.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # Multiple periods in filename; multiple periods in extension.
        fp = os.path.join(self.test_dir.name, 'artifact..')
        obs_fp = artifact.save(fp, '..txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # No extension in filename; no extension input.
        fp = os.path.join(self.test_dir.name, 'artifact')
        obs_fp = artifact.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.qza')

        # No extension in filename; different extension input.
        fp = os.path.join(self.test_dir.name, 'artifact')
        obs_fp = artifact.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.txt')

        # No extension in filename; default extension input.
        fp = os.path.join(self.test_dir.name, 'artifact')
        obs_fp = artifact.save(fp, '.qza')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.qza')

        # Different extension in filename; no extension input.
        fp = os.path.join(self.test_dir.name, 'artifact.zip')
        obs_fp = artifact.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.zip.qza')

        # Different extension in filename;
        # Different extension input (non-matching).
        fp = os.path.join(self.test_dir.name, 'artifact.zip')
        obs_fp = artifact.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.zip.txt')

        # Different extension in filename;
        # Different extension input (matching).
        fp = os.path.join(self.test_dir.name, 'artifact.zip')
        obs_fp = artifact.save(fp, '.zip')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.zip')

        # Different extension in filename; default extension input.
        fp = os.path.join(self.test_dir.name, 'artifact.zip')
        obs_fp = artifact.save(fp, '.qza')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.zip.qza')

        # Default extension in filename; no extension input.
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        obs_fp = artifact.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.qza')

        # Default extension in filename; different extension input.
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        obs_fp = artifact.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.qza.txt')

        # Default extension in filename; default extension input.
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        obs_fp = artifact.save(fp, '.qza')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'artifact.qza')

    def test_save_visualization_auto_extension(self):
        visualization = Visualization._from_data_dir(
             self.data_dir, self.make_provenance_capture())

        # Filename & extension endswith is matching (default).
        fp = os.path.join(self.test_dir.name, 'visualizationqzv')
        obs_fp = visualization.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualizationqzv.qzv')

        # Filename & extension endswith is matching (non-default).
        fp = os.path.join(self.test_dir.name, 'visualizationtxt')
        obs_fp = visualization.save(fp, 'txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualizationtxt.txt')

        # No period in filename; no period in extension.
        fp = os.path.join(self.test_dir.name, 'visualization')
        obs_fp = visualization.save(fp, 'txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # No period in filename; multiple periods in extension.
        fp = os.path.join(self.test_dir.name, 'visualization')
        obs_fp = visualization.save(fp, '..txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # Single period in filename; no period in extension.
        fp = os.path.join(self.test_dir.name, 'visualization.')
        obs_fp = visualization.save(fp, 'txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # Single period in filename; single period in extension.
        fp = os.path.join(self.test_dir.name, 'visualization.')
        obs_fp = visualization.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # Single period in filename; multiple periods in extension.
        fp = os.path.join(self.test_dir.name, 'visualization.')
        obs_fp = visualization.save(fp, '..txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # Multiple periods in filename; single period in extension.
        fp = os.path.join(self.test_dir.name, 'visualization..')
        obs_fp = visualization.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # Multiple periods in filename; multiple periods in extension.
        fp = os.path.join(self.test_dir.name, 'visualization..')
        obs_fp = visualization.save(fp, '..txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # No extension in filename; no extension input.
        fp = os.path.join(self.test_dir.name, 'visualization')
        obs_fp = visualization.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.qzv')

        # No extension in filename; different extension input.
        fp = os.path.join(self.test_dir.name, 'visualization')
        obs_fp = visualization.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.txt')

        # No extension in filename; default extension input.
        fp = os.path.join(self.test_dir.name, 'visualization')
        obs_fp = visualization.save(fp, '.qzv')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.qzv')

        # Different extension in filename; no extension input.
        fp = os.path.join(self.test_dir.name, 'visualization.zip')
        obs_fp = visualization.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.zip.qzv')

        # Different extension in filename;
        # Different extension input (non-matching).
        fp = os.path.join(self.test_dir.name, 'visualization.zip')
        obs_fp = visualization.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.zip.txt')

        # Different extension in filename;
        # Different extension input (matching).
        fp = os.path.join(self.test_dir.name, 'visualization.zip')
        obs_fp = visualization.save(fp, '.zip')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.zip')

        # Different extension in filename; default extension input.
        fp = os.path.join(self.test_dir.name, 'visualization.zip')
        obs_fp = visualization.save(fp, '.qzv')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.zip.qzv')

        # Default extension in filename; no extension input.
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        obs_fp = visualization.save(fp)
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.qzv')

        # Default extension in filename; different extension input.
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        obs_fp = visualization.save(fp, '.txt')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.qzv.txt')

        # Default extension in filename; default extension input.
        fp = os.path.join(self.test_dir.name, 'visualization.qzv')
        obs_fp = visualization.save(fp, '.qzv')
        obs_filename = os.path.basename(obs_fp)

        self.assertEqual(obs_filename, 'visualization.qzv')

    def test_import_data_single_dirfmt_to_single_dirfmt(self):
        temp_data_dir = os.path.join(self.test_dir.name, 'import')
        os.mkdir(temp_data_dir)

        with open(os.path.join(temp_data_dir, 'ints.txt'), 'w') as fh:
            fh.write("1\n2\n3\n")

        rachis.Artifact.import_data('IntSequence2', temp_data_dir,
                                    view_type="IntSequenceDirectoryFormat")

    def test_artifact_has_metadata_true(self):
        A = Artifact.import_data('Mapping', {'a': '1', 'b': '2'})
        self.assertTrue(A.has_metadata())

    def test_artifact_has_metadata_false(self):
        A = Artifact.import_data('IntSequence1', [1, 2, 3, 4])
        self.assertFalse(A.has_metadata())

    def test_validate_artifact_good(self):
        artifact = Artifact.import_data('IntSequence1', [1, 2, 3, 4])

        artifact.validate()
        self.assertTrue(True)  # Checkpoint

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_validate_artifact_bad(self):
        artifact = Artifact.import_data('IntSequence1', [1, 2, 3, 4])
        # We set everything in the artifact to be read-only. This test needs to
        # mimic if the user were to somehow write it anyway, so we set write
        # for self and group
        set_permissions(artifact._archiver.root_dir, OTHER_NO_WRITE,
                        OTHER_NO_WRITE)

        with (artifact._archiver.root_dir / 'extra.file').open('w') as fh:
            fh.write('uh oh')

        with self.assertRaisesRegex(exceptions.ValidationError,
                                    r'extra\.file'):
            artifact.validate()

    def test_validate_vizualization_good(self):
        visualization = Visualization._from_data_dir(
             self.data_dir, self.make_provenance_capture())

        visualization.validate()
        self.assertTrue(True)  # Checkpoint

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_validate_vizualization_bad(self):
        visualization = Visualization._from_data_dir(
             self.data_dir, self.make_provenance_capture())

        # We set everything in the artifact to be read-only. This test needs to
        # mimic if the user were to somehow write it anyway, so we set write
        # for self and group
        set_permissions(visualization._archiver.root_dir, OTHER_NO_WRITE,
                        OTHER_NO_WRITE)

        with (visualization._archiver.root_dir / 'extra.file').open('w') as fh:
            fh.write('uh oh')

        with self.assertRaisesRegex(exceptions.ValidationError,
                                    r'extra\.file'):
            visualization.validate()

    def test_import_min_validate(self):
        with tempfile.TemporaryDirectory() as tempdir:
            fp = os.path.join(tempdir, 'ints.txt')
            with open(fp, 'w') as fh:
                for i in range(5):
                    fh.write(f'{i}\n')
                fh.write('a\n')

            intseq_dir = IntSequenceDirectoryFormat(tempdir, 'r')

            # import with min allows format error outside of min purview
            _ = Artifact.import_data(
                'IntSequence1', intseq_dir, validate_level='min'
            )

            # import with max should catch all format errors, max is default
            with self.assertRaisesRegex(
                exceptions.ValidationError, 'Line 6 is not an integer'
            ):
                _ = Artifact.import_data('IntSequence1', tempdir)

        with tempfile.TemporaryDirectory() as tempdir:
            fp = os.path.join(tempdir, 'ints.txt')
            with open(fp, 'w') as fh:
                fh.write('1\n')
                fh.write('a\n')
                fh.write('3\n')

            intseq_dir = IntSequenceDirectoryFormat(tempdir, 'r')

            # import with min catches format errors within its purview
            with self.assertRaisesRegex(
                exceptions.ValidationError, 'Line 2 is not an integer'
            ):
                _ = Artifact.import_data(
                    'IntSequence1', [1, 'a', 3, 4], validate_level='min'
                )

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_validate_checksums(self):
        '''
        Tests that Artifact.validate_checksums passes when artifact contents
        are not changed and fails when a checksum in the checksum file is
        altered.
        '''
        artifact = Artifact.import_data('IntSequence1', [1, 2, 3, 5])
        artifact.validate_checksums()

        if not hasattr(artifact._archiver._fmt, 'CHECKSUM_FILE'):
            return

        checksum_fp = (
            pathlib.Path(artifact._archiver.root_dir) /
            artifact._archiver._fmt.CHECKSUM_FILE
        )
        with open(checksum_fp, 'r+') as fh:
            content = fh.read()

            # change the first character in the first checksum
            if content[0] != 'a':
                content = 'a' + content[1:]
            else:
                content = 'b' + content[1:]

            fh.write(content)

        with self.assertRaisesRegex(
            exceptions.ValidationError, 'Changed files'
        ):
            artifact.validate_checksums()


class TestResultCollection(unittest.TestCase):
    def setUp(self):
        # Ignore the returned dummy plugin object, just run this to verify the
        # plugin exists as the tests rely on it being loaded.
        get_dummy_plugin()

        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')
        self.output_fp = os.path.join(self.test_dir.name, 'output')

        self.collection = ResultCollection(
            {'foo': Artifact.import_data(SingleInt, 0),
             'bar': Artifact.import_data(SingleInt, 1)})

    def tearDown(self):
        self.test_dir.cleanup()

    def test_roundtrip_ordered_collection(self):
        self.collection.save(self.output_fp)

        foo = Artifact.load(os.path.join(self.output_fp, 'foo.qza'))
        bar = Artifact.load(os.path.join(self.output_fp, 'bar.qza'))

        self.assertEqual(foo.view(int), 0)
        self.assertEqual(bar.view(int), 1)

        with open(os.path.join(self.output_fp, '.order')) as fh:
            self.assertEqual(fh.read(), 'foo\nbar\n')

        read_collection = ResultCollection.load(self.output_fp)
        self.assertEqual(self.collection, read_collection)

    def test_roundtrip_unordered_collection(self):
        self.collection.save(self.output_fp)
        os.remove(os.path.join(self.output_fp, '.order'))

        foo = Artifact.load(os.path.join(self.output_fp, 'foo.qza'))
        bar = Artifact.load(os.path.join(self.output_fp, 'bar.qza'))

        self.assertEqual(foo.view(int), 0)
        self.assertEqual(bar.view(int), 1)

        with self.assertWarnsRegex(
                UserWarning, f"The directory '{self.output_fp}' does not "
                "contain a .order file"):
            read_collection = ResultCollection.load(self.output_fp)

        self.assertEqual(
            set(self.collection.items()), set(read_collection.items()))

    def test_type_normal_collection(self):
        self.assertEqual(
            self.collection.type, rachis.core.type.Collection[SingleInt])

    def test_type_weird_collection(self):
        weird_collection = ResultCollection({
            'foo': Artifact.import_data(SingleInt, 0),
            'bar': Artifact.import_data(FourInts, [1, 2, 3, 4]),
            'baz': Artifact.import_data(IntSequence1, [5, 6, 7]),
            'qux': Artifact.import_data(IntSequence2, [8, 9, 10])})

        self.assertEqual(
            weird_collection.type,
            rachis.core.type.Collection[SingleInt | FourInts | IntSequence1 |
                                        IntSequence2])

    def test_collection_order_file_contains_nonexistent_key(self):
        BAD_KEY = 'NonexistentKey'

        self.collection.save(self.output_fp)
        order_fp = os.path.join(self.output_fp, '.order')

        with open(order_fp, 'a') as order_fh:
            order_fh.write(BAD_KEY)

        foo = Artifact.load(os.path.join(self.output_fp, 'foo.qza'))
        bar = Artifact.load(os.path.join(self.output_fp, 'bar.qza'))

        self.assertEqual(foo.view(int), 0)
        self.assertEqual(bar.view(int), 1)

        with self.assertRaisesRegex(
                ValueError, f"The Result '{BAD_KEY}' is referenced in the "
                            "order file but does not exist"):
            ResultCollection.load(self.output_fp)

    def test_iter(self):
        self.assertEqual(list(self.collection), ['foo', 'bar'])
        self.assertEqual(
            list(self.collection), list(self.collection.collection)
        )

    def test_collection_non_str_keys(self):
        with self.assertRaisesRegex(
                KeyError, 'ResultCollection keys must be strings and may only '
                'contain the following characters:.*1'):
            ResultCollection({1: 0})

    def test_invalid_key_init(self):
        with self.assertRaisesRegex(
                KeyError,
                'ResultCollection keys must be strings and may only contain '
                'the following characters:.*valid key'):
            ResultCollection({'not a valid key': 0})

    def test_invalid_key_added(self):
        collection = ResultCollection()

        with self.assertRaisesRegex(
                KeyError,
                'ResultCollection keys must be strings and may only contain '
                'the following characters:.*valid key'):
            collection['not a valid key'] = 0

    def test_validate(self):
        '''
        Validates two result collections, one with all valid members which is
        expected to pass, and one with an invalid member which is expected to
        fail.
        '''
        int_seq_1 = Artifact.import_data(
            'IntSequence1', [1, 3, 5, 7], validate_level='min'
        )
        int_seq_2 = Artifact.import_data(
            'IntSequence1', [6, 7], validate_level='min'
        )
        int_seq_3 = Artifact.import_data(
            'IntSequence1', [2, 4, 6, 8], validate_level='min'
        )

        collection = ResultCollection({
            'is1': int_seq_1, 'is2': int_seq_2, 'is3': int_seq_3
        })
        collection.validate(level='max')

        # we want to test ResultCollection.validate's logic, not
        # IntSequenceFormat._validate_ directly
        with unittest.mock.patch(
            'rachis.core.testing.format.IntSequenceFormat._validate_',
            side_effect=lambda level: None
        ):
            int_seq_invalid = Artifact.import_data(
                'IntSequence1', [2, 4, 6, 'suh'], validate_level='min'
            )

        collection = ResultCollection({
            'is1': int_seq_1, 'is2': int_seq_2, 'is3': int_seq_invalid
        })

        with self.assertRaisesRegex(
            exceptions.ValidationError, 'not an integer'
        ):
            collection.validate(level='max')

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_validate_checksums(self):
        '''
        Tests that ResultCollection.validate_checksums passes when its artifact
        contents' are not changed and fails when a checksum in the checksum
        file of a member artifact is altered.
        '''
        artifact1 = Artifact.import_data('IntSequence1', [1, 2, 3, 4])
        artifact2 = Artifact.import_data('IntSequence1', [6, 7])
        collection = ResultCollection({'a1': artifact1, 'a2': artifact2})
        collection.validate_checksums()

        if not hasattr(artifact1._archiver._fmt, 'CHECKSUM_FILE'):
            return

        checksum_fp = (
            pathlib.Path(artifact1._archiver.root_dir) /
            artifact1._archiver._fmt.CHECKSUM_FILE
        )
        with open(checksum_fp, 'r+') as fh:
            content = fh.read()

            # change the first character in the first checksum
            if content[0] != 'a':
                content = 'a' + content[1:]
            else:
                content = 'b' + content[1:]

            fh.write(content)

        with self.assertRaisesRegex(
            exceptions.ValidationError, 'Changed files'
        ):
            collection.validate_checksums()


class TestRedactMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.mkdtemp(prefix='qiime2-q2cli-test-temp-')

        metadata_path = os.path.join(cls.tempdir, 'metadata.tsv')
        with open(metadata_path, 'w') as fh:
            fh.write('sample-id\tbarcode-sequence\n')
            fh.write('1\tACT')
        dummy_md = Metadata.load(metadata_path)

        pm = PluginManager()
        identity_with_metadata = pm.plugins['dummy-plugin'].actions[
            'identity_with_metadata'
        ]

        artifact1 = Artifact.import_data(IntSequence1, [0, 6, 7])
        cls.artifact1, = identity_with_metadata(artifact1, dummy_md)
        artifact2 = Artifact.import_data(IntSequence2, [3, 4, 5])
        cls.artifact2, = identity_with_metadata(artifact2, dummy_md)
        cls.artifact3 = Artifact.import_data(SingleInt, 9)

    def test_redact_metadata_success(self):
        self.artifact1.redact_metadata()
        metadata_paths, _ = self.artifact1.metadata_paths()

        for path in metadata_paths:
            self.assertEqual(len(path.read_bytes()), 0)

    def test_redact_metadata_twice_fails(self):
        self.artifact2.redact_metadata()

        with self.assertRaisesRegex(ValueError, 'only redacted metadata'):
            self.artifact2.redact_metadata()

    def test_redact_metadata_no_metadata(self):
        with self.assertRaisesRegex(ValueError, 'Result without metadata'):
            self.artifact3.redact_metadata()


@pytest.fixture
def signature_test_env(monkeypatch):
    # fake key info that gpg_find_key would normally parse
    fake_key_info = {
        "fingerprint": 'ABCDEF0123456789ABCDEF0123456789ABCDEF01',
        "algorithm": "Ed25519",
        "length": 0,
        "curve": "ed25519",
        "uids": [{"raw": "Test User <test@example.com>",
                  "name": "Test User",
                  "email": "test@example.com"}],
        "chosen_uid": {"raw": "Test User <test@example.com>",
                       "name": "Test User",
                       "email": "test@example.com"}
    }

    # default behavior will be to succeed
    key_lookup_behavior = {'mode': 'ok'}

    def fake_gpg_find_key(fp):
        if key_lookup_behavior['mode'] == 'ok':
            return dict(fake_key_info, fingerprint=fp)

        if key_lookup_behavior['mode'] == 'mismatch':
            # key found but different fingerprint
            return dict(fake_key_info, fingerprint='MEGALOCK' * 5)

        if key_lookup_behavior['mode'] == 'raise':
            # no matching key found
            raise RuntimeError(
                'No matching key found for the provided fingerprint')

    real_run = subprocess.run

    # fake subprocess.run to:
    # - write a dummy signature.gpg file when called with --detach-sign
    # - returncode 0 when called with --verify
    def fake_run(cmd, *args, **kwargs):
        if '--detach-sign' in cmd:
            # ['gpg', ..., '--output', sig_fp, '--detach-sign', checksums_fp]
            out_index = cmd.index('--output') + 1
            sig_fp = cmd[out_index]
            pathlib.Path(sig_fp).write_bytes(b'FAKESIG')
            return unittest.mock.Mock(returncode=0)

        elif cmd[:2] == ['gpg', '--verify']:
            # pretend verify succeeded
            return unittest.mock.Mock(returncode=0)

        else:
            return real_run(cmd, *args, **kwargs)

    # patch calls to gpg_find_key with fake dict & subprocess.run w/fake_run
    monkeypatch.setattr(rachis.core.annotate,
                        'gpg_find_key', fake_gpg_find_key)
    monkeypatch.setattr(
        rachis.core.archive.archiver, 'gpg_find_key', fake_gpg_find_key
    )
    monkeypatch.setattr(subprocess, 'run', fake_run)

    def set_key_lookup(mode):
        key_lookup_behavior['mode'] = mode

    return {
        'fake_key_info': fake_key_info,
        'set_key_lookup': set_key_lookup,
    }


def test_signature_roundtrip_success(signature_test_env):
    artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])

    sig = Signature(
        name='mysig',
        fingerprint='ABCDEF0123456789ABCDEF0123456789ABCDEF01'
    )

    artifact.add_annotation(sig)
    artifact.verify('mysig')


def test_signature_create_failure_invalid_fingerprint(signature_test_env):
    signature_test_env['set_key_lookup']('raise')

    with pytest.raises(RuntimeError) as e:
        Signature(
            name='badtothebone',
            fingerprint='0000000000000000000000000000000000000000'
        )

    assert 'No matching key' in str(e.value)


def test_signature_verify_failure_mismatched_fingerprint(signature_test_env):
    artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])
    sig = Signature(
        name='mysig',
        fingerprint='ABCDEF0123456789ABCDEF0123456789ABCDEF01'
    )
    artifact.add_annotation(sig)

    signature_test_env['set_key_lookup']('mismatch')
    with pytest.raises(ValueError) as e:
        artifact.verify('mysig')

    assert 'Found fingerprint does not match' in str(e.value)


if __name__ == '__main__':
    unittest.main()
