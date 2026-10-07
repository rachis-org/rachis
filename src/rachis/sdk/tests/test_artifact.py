# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections
import os
import tempfile
import unittest
import uuid
import pathlib
from typing import Union

import pandas as pd

import rachis.plugin
import rachis.core.type
from rachis import Metadata
from rachis.sdk import Artifact
from rachis.sdk.result import ResultMetadata
from rachis.plugin.model import ValidationError
import rachis.core.archive as archive

from rachis.core.testing.format import IntSequenceFormat
from rachis.core.testing.type import IntSequence1, FourInts, Mapping, SingleInt
from rachis.core.testing.util import get_dummy_plugin, ArchiveTestingMixin


def get_data_path(filename):
    fp = rachis.util.get_filepath_from_package(
        'rachis.sdk.tests', 'data/%s' % filename)
    return str(fp)


class TestArtifact(unittest.TestCase, ArchiveTestingMixin):
    def setUp(self):
        # Ignore the returned dummy plugin object, just run this to verify the
        # plugin exists as the tests rely on it being loaded.
        get_dummy_plugin()

        # TODO standardize temporary directories created by QIIME 2
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')
        self.provenance_capture = archive.ImportProvenanceCapture()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_private_constructor(self):
        with self.assertRaisesRegex(
                NotImplementedError,
                'Artifact constructor.*private.*Artifact.load'):
            Artifact()

    # Note on testing strategy below: many of the tests for `_from_view` and
    # `load` are similar, with the exception that when `load`ing, the
    # artifact's UUID is known so more specific assertions can be performed.
    # While these tests appear somewhat redundant, they are important because
    # they exercise the same operations on Artifact objects constructed from
    # different sources, whose codepaths have very different internal behavior.
    # This internal behavior could be tested explicitly but it is safer to test
    # the public API behavior (e.g. as a user would interact with the object)
    # in case the internals change.

    def test_from_view(self):
        artifact = Artifact._from_view(FourInts, [-1, 42, 0, 43], list,
                                       self.provenance_capture)

        self.assertEqual(artifact.type, FourInts)
        # We don't know what the UUID is because it's generated within
        # Artifact._from_view.
        self.assertIsInstance(artifact.uuid, uuid.UUID)
        self.assertEqual(artifact.view(list), [-1, 42, 0, 43])
        # Can produce same view if called again.
        self.assertEqual(artifact.view(list), [-1, 42, 0, 43])

    def test_from_view_union(self):
        artifact = Artifact._from_view(FourInts, [-1, 42, 0, 43], list,
                                       self.provenance_capture)

        self.assertEqual(artifact.type, FourInts)
        # We don't know what the UUID is because it's generated within
        # Artifact._from_view.
        self.assertIsInstance(artifact.uuid, uuid.UUID)
        self.assertEqual(artifact.view(Union[list, str]), [-1, 42, 0, 43])
        # Can produce same view if called again.
        self.assertEqual(artifact.view(Union[list, str]), [-1, 42, 0, 43])

    def test_from_view_union_reordered(self):
        artifact = Artifact._from_view(FourInts, [-1, 42, 0, 43], list,
                                       self.provenance_capture)

        self.assertEqual(artifact.type, FourInts)
        # We don't know what the UUID is because it's generated within
        # Artifact._from_view.
        self.assertIsInstance(artifact.uuid, uuid.UUID)
        self.assertEqual(artifact.view(Union[str, list]), [-1, 42, 0, 43])
        # Can produce same view if called again.
        self.assertEqual(artifact.view(Union[str, list]), [-1, 42, 0, 43])

    def test_from_view_union_not_valid(self):
        artifact = Artifact._from_view(FourInts, [-1, 42, 0, 43], list,
                                       self.provenance_capture)

        self.assertEqual(artifact.type, FourInts)
        # We don't know what the UUID is because it's generated within
        # Artifact._from_view.
        self.assertIsInstance(artifact.uuid, uuid.UUID)
        with self.assertRaisesRegex(
                Exception,
                'No transformation into either of'):
            self.assertEqual(artifact.view(Union[str, dict]), [-1, 42, 0, 43])

    def test_from_view_different_type_with_multiple_view_types(self):
        artifact = Artifact._from_view(IntSequence1, [42, 42, 43, -999, 42],
                                       list, self.provenance_capture)

        self.assertEqual(artifact.type, IntSequence1)
        self.assertIsInstance(artifact.uuid, uuid.UUID)

        self.assertEqual(artifact.view(list),
                         [42, 42, 43, -999, 42])
        self.assertEqual(artifact.view(list),
                         [42, 42, 43, -999, 42])

        self.assertEqual(artifact.view(collections.Counter),
                         collections.Counter({42: 3, 43: 1, -999: 1}))
        self.assertEqual(artifact.view(collections.Counter),
                         collections.Counter({42: 3, 43: 1, -999: 1}))

    def test_from_view_and_save(self):
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        # Using four-ints data layout because it has multiple files, some of
        # which are in a nested directory.
        artifact = Artifact._from_view(FourInts, [-1, 42, 0, 43], list,
                                       self.provenance_capture)

        artifact.save(fp)

        root_dir = str(artifact.uuid)
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

        self.assertArchiveMembers(fp, root_dir, expected)

    def test_load(self):
        saved_artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        saved_artifact.save(fp)

        artifact = Artifact.load(fp)

        self.assertEqual(artifact.type, FourInts)
        self.assertEqual(artifact.uuid, saved_artifact.uuid)
        self.assertEqual(artifact.view(list), [-1, 42, 0, 43])
        self.assertEqual(artifact.view(list), [-1, 42, 0, 43])

    def test_load_different_type_with_multiple_view_types(self):
        saved_artifact = Artifact.import_data(IntSequence1,
                                              [42, 42, 43, -999, 42])
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        saved_artifact.save(fp)

        artifact = Artifact.load(fp)

        self.assertEqual(artifact.type, IntSequence1)
        self.assertEqual(artifact.uuid, saved_artifact.uuid)

        self.assertEqual(artifact.view(list),
                         [42, 42, 43, -999, 42])
        self.assertEqual(artifact.view(list),
                         [42, 42, 43, -999, 42])

        self.assertEqual(artifact.view(collections.Counter),
                         collections.Counter({42: 3, 43: 1, -999: 1}))
        self.assertEqual(artifact.view(collections.Counter),
                         collections.Counter({42: 3, 43: 1, -999: 1}))

    def test_load_and_save(self):
        fp1 = os.path.join(self.test_dir.name, 'artifact1.qza')
        fp2 = os.path.join(self.test_dir.name, 'artifact2.qza')
        artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        artifact.save(fp1)

        artifact = Artifact.load(fp1)
        # Overwriting its source file works.
        artifact.save(fp1)
        # Saving to a new file works.
        artifact.save(fp2)

        root_dir = str(artifact.uuid)
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

        self.assertArchiveMembers(fp1, root_dir, expected)

        root_dir = str(artifact.uuid)
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

        self.assertArchiveMembers(fp2, root_dir, expected)

    def test_roundtrip(self):
        fp1 = os.path.join(self.test_dir.name, 'artifact1.qza')
        fp2 = os.path.join(self.test_dir.name, 'artifact2.qza')
        artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])

        artifact.save(fp1)

        artifact1 = Artifact.load(fp1)
        artifact1.save(fp2)
        artifact2 = Artifact.load(fp2)

        self.assertEqual(artifact1.type, artifact2.type)
        self.assertEqual(artifact1.format, artifact2.format)
        self.assertEqual(artifact1.uuid, artifact2.uuid)
        self.assertEqual(artifact1.view(list),
                         artifact2.view(list))
        # double view to make sure multiple views can be taken
        self.assertEqual(artifact1.view(list),
                         artifact2.view(list))

    def test_roundtrip_pathlib(self):
        fp1 = pathlib.Path(os.path.join(self.test_dir.name, 'artifact1.qza'))
        fp2 = pathlib.Path(os.path.join(self.test_dir.name, 'artifact2.qza'))
        artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])

        artifact.save(fp1)

        artifact1 = Artifact.load(fp1)
        artifact1.save(fp2)
        artifact2 = Artifact.load(fp2)

        self.assertEqual(artifact1.type, artifact2.type)
        self.assertEqual(artifact1.format, artifact2.format)
        self.assertEqual(artifact1.uuid, artifact2.uuid)
        self.assertEqual(artifact1.view(list),
                         artifact2.view(list))
        # double view to make sure multiple views can be taken
        self.assertEqual(artifact1.view(list),
                         artifact2.view(list))

    def test_load_with_archive_filepath_modified(self):
        # Save an artifact for use in the following test case.
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        Artifact.import_data(FourInts, [-1, 42, 0, 43]).save(fp)

        # Load the artifact from a filepath then save a different artifact to
        # the same filepath. Assert that both artifacts produce the correct
        # views of their data.
        #
        # `load` used to be lazy, only extracting data when it needed to (e.g.
        # when `save` or `view` was called). This was buggy as the filepath
        # could have been deleted, or worse, modified to contain a different
        # .qza file. Thus, the wrong archive could be extracted on demand, or
        # the archive could be missing altogether. There isn't an easy
        # cross-platform compatible way to solve this problem, so Artifact.load
        # is no longer lazy and always extracts its data immediately. The real
        # motivation for lazy loading was for quick inspection of archives
        # without extracting/copying data, so that API is now provided through
        # Artifact.peek.
        artifact1 = Artifact.load(fp)
        Artifact.import_data(FourInts, [10, 11, 12, 13]).save(fp)
        artifact2 = Artifact.load(fp)

        self.assertEqual(artifact1.view(list), [-1, 42, 0, 43])
        self.assertEqual(artifact2.view(list), [10, 11, 12, 13])

    def test_extract(self):
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        artifact.save(fp)

        root_dir = str(artifact.uuid)
        # pathlib normalizes away the `.`, it doesn't matter, but this is the
        # implementation we're using, so let's test against that assumption.
        output_dir = pathlib.Path(self.test_dir.name) / 'artifact-extract-test'
        result_dir = Artifact.extract(fp, output_dir=output_dir)
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

    def test_peek(self):
        artifact = Artifact.import_data(FourInts, [0, 0, 42, 1000])
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        artifact.save(fp)

        metadata = Artifact.peek(fp)

        self.assertIsInstance(metadata, ResultMetadata)
        self.assertEqual(metadata.type, 'FourInts')
        self.assertEqual(metadata.uuid, str(artifact.uuid))
        self.assertEqual(metadata.format, 'FourIntsDirectoryFormat')

    def test_import_data_invalid_type(self):
        with self.assertRaisesRegex(TypeError,
                                    'concrete semantic type.*Visualization'):
            Artifact.import_data(rachis.core.type.Visualization, self.test_dir)

        with self.assertRaisesRegex(TypeError,
                                    'concrete semantic type.*Visualization'):
            Artifact.import_data('Visualization', self.test_dir)

    def test_import_data_with_filepath_multi_file_data_layout(self):
        fp = os.path.join(self.test_dir.name, 'test.txt')
        with open(fp, 'w') as fh:
            fh.write('42\n')

        with self.assertRaisesRegex(rachis.plugin.ValidationError,
                                    "FourIntsDirectoryFormat.*directory"):
            Artifact.import_data(FourInts, fp)

    def test_import_data_with_wrong_number_of_files(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        error_regex = ("Missing.*MappingDirectoryFormat.*mapping.tsv")
        with self.assertRaisesRegex(ValidationError, error_regex):
            Artifact.import_data(Mapping, data_dir)

    def test_import_data_with_unrecognized_files(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        with open(os.path.join(data_dir, 'file1.txt'), 'w') as fh:
            fh.write('42\n')
        with open(os.path.join(data_dir, 'file2.txt'), 'w') as fh:
            fh.write('43\n')
        nested = os.path.join(data_dir, 'nested')
        os.mkdir(nested)
        with open(os.path.join(nested, 'file3.txt'), 'w') as fh:
            fh.write('44\n')
        with open(os.path.join(nested, 'foo.txt'), 'w') as fh:
            fh.write('45\n')

        error_regex = ("Unrecognized.*foo.txt.*FourIntsDirectoryFormat")
        with self.assertRaisesRegex(ValidationError, error_regex):
            Artifact.import_data(FourInts, data_dir)

    def test_import_data_ignores_dotpaths_sfdf(self):
        '''
        Tests that dotfiles and files with a hidden directory anywhere in
        their ancestory are ignored during validation when importing into a
        SingleFileDirectoryFormat.
        '''
        test_dir = pathlib.Path(self.test_dir.name)

        with open(test_dir / 'ints.txt', 'w') as fh:
            fh.write('42\n')

        dotfile = test_dir / '.dotfile'
        dotfile.touch()

        dotdir = test_dir / '.dotdir'
        os.mkdir(dotdir)

        nested = test_dir / '.dotdir' / 'nested'
        nested.touch()

        # If dotfiles weren't ignored, this would fail.
        Artifact.import_data(IntSequence1, test_dir)

    def test_import_data_ignores_dotpaths_df(self):
        '''
        Tests that dotfiles and files with a hidden directory anywhere in
        their ancestory are ignored during validation when importing into a
        DirectoryFormat.
        '''
        test_dir = pathlib.Path(self.test_dir.name)

        with open(test_dir / 'file1.txt', 'w') as fh:
            fh.write('42\n')

        with open(test_dir / 'file2.txt', 'w') as fh:
            fh.write('42\n')

        dotfile = test_dir / '.dotfile'
        dotfile.touch()

        dotdir = test_dir / '.dotdir'
        os.mkdir(dotdir)

        nested = test_dir / '.dotdir' / 'nested'
        nested.touch()

        # If dotfiles weren't ignored, this would fail.
        Artifact.import_data(SingleInt, test_dir)



    def test_import_data_with_unreachable_path(self):
        with self.assertRaisesRegex(rachis.plugin.ValidationError,
                                    "does not exist"):
            Artifact.import_data(IntSequence1,
                                 os.path.join(self.test_dir.name, 'foo.txt'))

        with self.assertRaisesRegex(rachis.plugin.ValidationError,
                                    "does not exist"):
            Artifact.import_data(FourInts,
                                 os.path.join(self.test_dir.name, 'bar', ''))

    def test_import_data_with_invalid_format_single_file(self):
        fp = os.path.join(self.test_dir.name, 'foo.txt')
        with open(fp, 'w') as fh:
            fh.write('42\n')
            fh.write('43\n')
            fh.write('abc\n')
            fh.write('123\n')

        error_regex = "foo.txt.*IntSequenceFormat.*\n\n.*Line 3"
        with self.assertRaisesRegex(ValidationError, error_regex):
            Artifact.import_data(IntSequence1, fp)

    def test_import_data_with_invalid_format_multi_file(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        with open(os.path.join(data_dir, 'file1.txt'), 'w') as fh:
            fh.write('42\n')
        with open(os.path.join(data_dir, 'file2.txt'), 'w') as fh:
            fh.write('43\n')
        nested = os.path.join(data_dir, 'nested')
        os.mkdir(nested)
        with open(os.path.join(nested, 'file3.txt'), 'w') as fh:
            fh.write('44\n')
        with open(os.path.join(nested, 'file4.txt'), 'w') as fh:
            fh.write('foo\n')

        error_regex = "file4.txt.*SingleIntFormat.*\n\n.*integer"
        with self.assertRaisesRegex(ValidationError, error_regex):
            Artifact.import_data(FourInts, data_dir)

    def test_import_data_with_good_validation_multi_files(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        with open(os.path.join(data_dir, 'file1.txt'), 'w') as fh:
            fh.write('1\n')
        with open(os.path.join(data_dir, 'file2.txt'), 'w') as fh:
            fh.write('1\n')

        a = Artifact.import_data(SingleInt, data_dir)
        self.assertEqual(1, a.view(int))

    def test_import_data_with_bad_validation_multi_files(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        with open(os.path.join(data_dir, 'file1.txt'), 'w') as fh:
            fh.write('1\n')
        with open(os.path.join(data_dir, 'file2.txt'), 'w') as fh:
            fh.write('2\n')

        error_regex = ("test.*RedundantSingleIntDirectoryFormat.*\n\n"
                       ".*does not match")
        with self.assertRaisesRegex(ValidationError, error_regex):
            Artifact.import_data(SingleInt, data_dir)

    def test_import_data_with_filepath(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        # Filename shouldn't matter for single-file case.
        fp = os.path.join(data_dir, 'foo.txt')
        with open(fp, 'w') as fh:
            fh.write('42\n')
            fh.write('43\n')
            fh.write('42\n')
            fh.write('0\n')

        artifact = Artifact.import_data(IntSequence1, fp)

        self.assertEqual(artifact.type, IntSequence1)
        self.assertIsInstance(artifact.uuid, uuid.UUID)
        self.assertEqual(artifact.view(list), [42, 43, 42, 0])

    def test_import_data_with_directory_single_file(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        fp = os.path.join(data_dir, 'ints.txt')
        with open(fp, 'w') as fh:
            fh.write('-1\n')
            fh.write('-2\n')
            fh.write('10\n')
            fh.write('100\n')

        artifact = Artifact.import_data(IntSequence1, data_dir)

        self.assertEqual(artifact.type, IntSequence1)
        self.assertIsInstance(artifact.uuid, uuid.UUID)
        self.assertEqual(artifact.view(list), [-1, -2, 10, 100])

    def test_import_data_with_directory_multi_file(self):
        data_dir = os.path.join(self.test_dir.name, 'test')
        os.mkdir(data_dir)
        with open(os.path.join(data_dir, 'file1.txt'), 'w') as fh:
            fh.write('42\n')
        with open(os.path.join(data_dir, 'file2.txt'), 'w') as fh:
            fh.write('41\n')
        nested = os.path.join(data_dir, 'nested')
        os.mkdir(nested)
        with open(os.path.join(nested, 'file3.txt'), 'w') as fh:
            fh.write('43\n')
        with open(os.path.join(nested, 'file4.txt'), 'w') as fh:
            fh.write('40\n')

        artifact = Artifact.import_data(FourInts, data_dir)

        self.assertEqual(artifact.type, FourInts)
        self.assertIsInstance(artifact.uuid, uuid.UUID)
        self.assertEqual(artifact.view(list), [42, 41, 43, 40])

    def test_eq_identity(self):
        artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])

        self.assertEqual(artifact, artifact)

    def test_eq_same_uuid(self):
        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        artifact1 = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        artifact1.save(fp)

        artifact2 = Artifact.load(fp)

        self.assertEqual(artifact1, artifact2)

    def test_ne_same_data_different_uuid(self):
        artifact1 = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        artifact2 = Artifact.import_data(FourInts, [-1, 42, 0, 43])

        self.assertNotEqual(artifact1, artifact2)

    def test_ne_different_data_different_uuid(self):
        artifact1 = Artifact.import_data(FourInts, [-1, 42, 0, 43])
        artifact2 = Artifact.import_data(FourInts, [1, 2, 3, 4])

        self.assertNotEqual(artifact1, artifact2)

    def test_ne_subclass_same_uuid(self):
        class ArtifactSubclass(Artifact):
            pass

        fp = os.path.join(self.test_dir.name, 'artifact.qza')
        artifact1 = ArtifactSubclass.import_data(FourInts, [-1, 42, 0, 43])
        artifact1.save(fp)

        artifact2 = Artifact.load(fp)

        self.assertNotEqual(artifact1, artifact2)
        self.assertNotEqual(artifact2, artifact1)

    def test_ne_different_type_same_uuid(self):
        artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])

        class Faker:
            @property
            def uuid(self):
                return artifact.uuid

        faker = Faker()

        self.assertNotEqual(artifact, faker)

    def test_artifact_validate_max(self):
        A = Artifact.import_data('Mapping', {'a': '1', 'b': '2'})
        A.validate()
        self.assertTrue(True)  # Checkpoint assertion
        A.validate(level='max')
        self.assertTrue(True)  # Checkpoint assertion
        with self.assertRaisesRegex(ValidationError, '3 more'):
            A = Artifact.import_data('IntSequence1', [1, 2, 3, 4, 5, 6, 7, 10])

    def test_artifact_validate_max_on_import(self):
        fp = get_data_path('intsequence-fail-max-validation.txt')
        fmt = IntSequenceFormat(fp, mode='r')
        fmt.validate(level='min')
        self.assertTrue(True)  # Checkpoint assertion
        with self.assertRaisesRegex(ValidationError, '3 more'):
            Artifact.import_data('IntSequence1', fp)

    def test_artifact_validate_min(self):
        A = Artifact.import_data('IntSequence1', [1, 2, 3, 4])
        A.validate(level='min')
        self.assertTrue(True)  # Checkpoint assertion
        A = Artifact.import_data('Mapping', {'a': '1', 'b': '2'})
        A.validate(level='min')
        self.assertTrue(True)  # Checkpoint assertion

    def test_artifact_validate_invalid_level(self):
        A = Artifact.import_data('IntSequence1', [1, 2, 3, 4])
        with self.assertRaisesRegex(ValueError, 'peanut'):
            A.validate(level='peanut')

    def test_view_as_metadata(self):
        A = Artifact.import_data('Mapping', {'a': '1', 'b': '3'})

        obs_md = A.view(Metadata)

        exp_df = pd.DataFrame({'a': '1', 'b': '3'},
                              index=pd.Index(['0'], name='id', dtype=object),
                              dtype=object)
        exp_md = Metadata(exp_df)
        exp_md._add_artifacts([A])

        self.assertEqual(obs_md, exp_md)

        # This check is redundant because `Metadata.__eq__` being used above
        # takes source artifacts into account. Doesn't hurt to have an explicit
        # check though, since this API didn't always track source artifacts
        # (this check also future-proofs the test in case `Metadata.__eq__`
        # changes in the future).
        self.assertEqual(obs_md.artifacts, (A,))

    def test_cannot_be_viewed_as_metadata(self):
        A = Artifact.import_data('IntSequence1', [1, 2, 3, 4])
        with self.assertRaisesRegex(Exception, 'No transformation from '):
            A.view(Metadata)


if __name__ == '__main__':
    unittest.main()
