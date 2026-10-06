# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import pytest
import tempfile
import unittest
import uuid
import zipfile
import pathlib
import importlib

from rachis.sdk.result import Result
from rachis.core.annotate import Note
from rachis.core.archive import Archiver
from rachis.core.archive import ImportProvenanceCapture
from rachis.core.archive.archiver import _ZipArchive, ArchiveCheck
from rachis.core.archive.format.util import artifact_version
from rachis.core.archive.provenance_lib.archive_parser import FORMAT_REGISTRY
from rachis.core.testing.format import IntSequenceDirectoryFormat
from rachis.core.testing.type import IntSequence1
from rachis.core.testing.util import ArchiveTestingMixin
from rachis.core.util import is_uuid4, set_permissions, OTHER_NO_WRITE


class TestArchiver(unittest.TestCase, ArchiveTestingMixin):
    def setUp(self):
        prefix = "rachis-test-temp-"
        self.temp_dir = tempfile.TemporaryDirectory(prefix=prefix)

        # Initialize an Archiver. The values passed to the constructor mostly
        # don't matter to the Archiver, but we'll pass valid Artifact test data
        # anyways in case Archiver's behavior changes in the future.
        def data_initializer(data_dir):
            fp = os.path.join(str(data_dir), 'ints.txt')
            with open(fp, 'w') as fh:
                fh.write('1\n')
                fh.write('2\n')
                fh.write('3\n')

        self.archiver = Archiver.from_data(
            IntSequence1, IntSequenceDirectoryFormat,
            data_initializer=data_initializer,
            provenance_capture=ImportProvenanceCapture())

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_invalid_filepath(self):
        # Empty filepath.
        with self.assertRaisesRegex(FileNotFoundError, 'No such file'):
            self.archiver.save('')

        # Directory.
        with self.assertRaisesRegex(IsADirectoryError, 'directory'):
            self.archiver.save(self.temp_dir.name)

        # Ends with path separator (no basename, e.g. /tmp/foo/).
        with self.assertRaises((IsADirectoryError, FileNotFoundError)):
            self.archiver.save(os.path.join(self.temp_dir.name, 'foo', ''))

    def test_save_excludes_dotfiles_in_data_dir(self):
        def data_initializer(data_dir):
            data_dir = str(data_dir)
            fp = os.path.join(data_dir, 'ints.txt')
            with open(fp, 'w') as fh:
                fh.write('1\n')
                fh.write('2\n')
                fh.write('3\n')

            hidden_fp = os.path.join(data_dir, '.hidden-file')
            with open(hidden_fp, 'w') as fh:
                fh.write("You can't see me if I can't see you\n")

            hidden_dir = os.path.join(data_dir, '.hidden-dir')
            os.mkdir(hidden_dir)
            with open(os.path.join(hidden_dir, 'ignored-file'), 'w') as fh:
                fh.write("I'm ignored because I live in a hidden dir :(\n")

        archiver = Archiver.from_data(
            IntSequence1, IntSequenceDirectoryFormat,
            data_initializer=data_initializer,
            provenance_capture=ImportProvenanceCapture())

        fp = os.path.join(self.temp_dir.name, 'archive.zip')
        archiver.save(fp)

        root_dir = str(archiver.uuid)
        expected = {
            'VERSION',
            'checksums.sha512',
            'metadata.yaml',
            'data/ints.txt',
            'provenance/metadata.yaml',
            'provenance/VERSION',
            'provenance/citations.bib',
            'provenance/conda-env.yaml',
            'provenance/action/action.yaml'
        }

        self.assertArchiveMembers(fp, root_dir, expected)

    def test_save_archive_members(self):
        fp = os.path.join(self.temp_dir.name, 'archive.zip')

        self.archiver.save(fp)

        root_dir = str(self.archiver.uuid)
        expected = {
            'VERSION',
            'checksums.sha512',
            'metadata.yaml',
            'data/ints.txt',
            'provenance/metadata.yaml',
            'provenance/VERSION',
            'provenance/citations.bib',
            'provenance/conda-env.yaml',
            'provenance/action/action.yaml'
        }

        self.assertArchiveMembers(fp, root_dir, expected)

    def test_load_archive(self):
        fp = os.path.join(self.temp_dir.name, 'archive.zip')
        self.archiver.save(fp)

        archiver = Archiver.load(fp)

        self.assertEqual(archiver.uuid, self.archiver.uuid)
        self.assertEqual(archiver.type, IntSequence1)
        self.assertEqual(archiver.format, IntSequenceDirectoryFormat)
        self.assertEqual({str(p.relative_to(archiver.data_dir))
                          for p in archiver.data_dir.iterdir()},
                         {'ints.txt'})

    def test_load_ignores_root_dotfiles(self):
        fp = os.path.join(self.temp_dir.name, 'archive.zip')
        self.archiver.save(fp)

        # Add some dotfiles to the archive.
        with zipfile.ZipFile(fp, mode='a') as zf:
            zf.writestr('.DS_Store', "The world's most beloved file\n")
            zf.writestr('.hidden-file',
                        "You can't see me if I can't see you\n")
            zf.writestr('.hidden-dir/ignored-file',
                        "I'm ignored because I live in a hidden dir :(\n")

        # Assert the expected files exist in the archive to verify this test
        # case is testing what we want it to.
        with zipfile.ZipFile(fp, mode='r') as zf:
            root_dir = str(self.archiver.uuid)
            expected = {
                '.DS_Store',
                '.hidden-file',
                '.hidden-dir/ignored-file',
                '%s/VERSION' % root_dir,
                '%s/checksums.sha512' % root_dir,
                '%s/metadata.yaml' % root_dir,
                '%s/data/ints.txt' % root_dir,
                '%s/provenance/metadata.yaml' % root_dir,
                '%s/provenance/VERSION' % root_dir,
                '%s/provenance/citations.bib' % root_dir,
                '%s/provenance/conda-env.yaml' % root_dir,
                '%s/provenance/action/action.yaml' % root_dir
            }

            observed = set(zf.namelist())

            # Not using self.assertArchiveMembers() because it accepts paths
            # relative to root_dir, and we have extra paths at the same level
            # as root_dir.
            self.assertEqual(observed, expected)

        archiver = Archiver.load(fp)

        self.assertEqual(archiver.uuid, self.archiver.uuid)
        self.assertEqual(archiver.type, IntSequence1)
        self.assertEqual(archiver.format, IntSequenceDirectoryFormat)
        self.assertEqual({str(p.relative_to(archiver.data_dir))
                          for p in archiver.data_dir.iterdir()},
                         {'ints.txt'})

    def test_load_empty_archive(self):
        fp = os.path.join(self.temp_dir.name, 'empty.zip')

        with zipfile.ZipFile(fp, mode='w') as zf:
            pass

        with zipfile.ZipFile(fp, mode='r') as zf:
            expected = set()
            observed = set(zf.namelist())

            self.assertEqual(observed, expected)

        with self.assertRaisesRegex(ValueError, 'visible root directory'):
            Archiver.load(fp)

    def test_load_dotfile_only_archive(self):
        fp = os.path.join(self.temp_dir.name, 'dotfiles-only.zip')

        with zipfile.ZipFile(fp, mode='w') as zf:
            zf.writestr('.DS_Store', "The world's most beloved file\n")
            zf.writestr('.hidden-file',
                        "You can't see me if I can't see you\n")
            zf.writestr('.hidden-dir/ignored-file',
                        "I'm ignored because I live in a hidden dir :(\n")

        with zipfile.ZipFile(fp, mode='r') as zf:
            expected = {
                '.DS_Store',
                '.hidden-file',
                '.hidden-dir/ignored-file'
            }

            observed = set(zf.namelist())

            self.assertEqual(observed, expected)

        with self.assertRaisesRegex(ValueError, 'visible root directory'):
            Archiver.load(fp)

    def test_load_multiple_root_dirs(self):
        fp = os.path.join(self.temp_dir.name, 'multiple-root-dirs.zip')
        self.archiver.save(fp)

        # Add another semi-valid root dir.
        second_root_dir = str(uuid.uuid4())
        with zipfile.ZipFile(fp, mode='a') as zf:
            zf.writestr('%s/VERSION' % second_root_dir, "foo")

        with zipfile.ZipFile(fp, mode='r') as zf:
            root_dir = str(self.archiver.uuid)
            expected = {
                '%s/VERSION' % root_dir,
                '%s/checksums.sha512' % root_dir,
                '%s/metadata.yaml' % root_dir,
                '%s/data/ints.txt' % root_dir,
                '%s/provenance/metadata.yaml' % root_dir,
                '%s/provenance/VERSION' % root_dir,
                '%s/provenance/citations.bib' % root_dir,
                '%s/provenance/conda-env.yaml' % root_dir,
                '%s/provenance/action/action.yaml' % root_dir,
                '%s/VERSION' % second_root_dir
            }

            observed = set(zf.namelist())

            self.assertEqual(observed, expected)

        with self.assertRaisesRegex(ValueError, 'multiple root directories'):
            Archiver.load(fp)

    def test_load_invalid_uuid4_root_dir(self):
        _uuid = uuid.uuid4()
        fp = pathlib.Path(self.temp_dir.name) / 'invalid-uuid4'
        zp = pathlib.Path(self.temp_dir.name) / 'bad.zip'
        (fp / str(_uuid)).mkdir(parents=True)
        # Invalid uuid4 taken from https://gist.github.com/ShawnMilo/7777304
        root_dir = '89eb3586-8a82-47a4-c911-758a62601cf7'

        record = _ZipArchive.setup(_uuid, fp / str(_uuid), 'foo', 'bar')
        (fp / str(record.uuid)).rename(fp / root_dir)
        _ZipArchive.save(fp, zp)

        with self.assertRaisesRegex(ValueError,
                                    'root directory.*valid version 4 UUID'):
            _ZipArchive(zp)

    def test_is_uuid4_valid(self):
        uuid_str = str(uuid.uuid4())

        self.assertTrue(is_uuid4(uuid_str))

    def test_parse_uuid_invalid(self):
        # Invalid uuid4 taken from https://gist.github.com/ShawnMilo/7777304
        uuid_str = '89eb3586-8a82-47a4-c911-758a62601cf7'
        self.assertFalse(is_uuid4(uuid_str))

        # Not a UUID.
        uuid_str = 'abc123'
        self.assertFalse(is_uuid4(uuid_str))

        # Other UUID versions.
        for uuid_ in (uuid.uuid1(), uuid.uuid3(uuid.NAMESPACE_DNS, 'foo'),
                      uuid.uuid5(uuid.NAMESPACE_DNS, 'bar')):
            uuid_str = str(uuid_)
            self.assertFalse(is_uuid4(uuid_str))

    def test_checksums_match(self):
        diff = self.archiver.validate_checksums()

        self.assertEqual(diff.added, {})
        self.assertEqual(diff.removed, {})
        self.assertEqual(diff.changed, {})

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_checksums_mismatch(self):
        # We set everything in the artifact to be read-only. This test needs to
        # mimic if the user were to somehow write it anyway, so we set write
        # for self and group
        set_permissions(self.archiver.root_dir, OTHER_NO_WRITE, OTHER_NO_WRITE)
        with (self.archiver.root_dir / 'data' / 'ints.txt').open('w') as fh:
            fh.write('999\n')
        with (self.archiver.root_dir / 'tamper.txt').open('w') as fh:
            fh.write('extra file')

        (self.archiver.root_dir / 'VERSION').unlink()

        diff = self.archiver.validate_checksums()
        # NOTE: as of 4/17/25 the 'expected' checksums have been modified
        # from the md5sum to the sha512sum to accommodate the updated
        # checksum calculations in archive v7.0
        self.assertEqual(diff.added,
                         {'tamper.txt': '4606a32b183684fd02c813c143683d1c4ac6'
                                        'cc1d14ab1594aaf38dc4ca16034072e90439'
                                        '57474b9e67784c87e3bdc57eea8d4787cf0e'
                                        '9eb74754440c553f4670'})
        # The contents of most files is either stochastic, or has the current
        # version (which is an unknown commit sha1), so just check name
        self.assertEqual(list(diff.removed.keys()), ['VERSION'])
        self.assertEqual(diff.changed,
                         {'data/ints.txt': ('6a8e8f13f75c3dead6c5b542d2282b182'
                                            'd94619292e7c31c551b719a65af7093a6'
                                            '21b008868d47d2e85973ae3fa1df5c8ca'
                                            '23f2bcb27919229ad0c5b9a59c8cc',
                                            '5eaa9c06b3b65d64fde15deeb8bdee993'
                                            '98d403cc1ac0f8face6da4ac71cea8139'
                                            'c99c8dbc6d1709cca6b76e94a246dcc1c'
                                            '00adf512ee3c1bf5fd20f96f7e7b6')
                          })

    def test_checksum_backwards_compat(self):
        self.tearDown()
        with artifact_version(4):
            self.setUp()

        diff = self.archiver.validate_checksums()

        self.assertEqual(diff.added, {})
        self.assertEqual(diff.removed, {})
        self.assertEqual(diff.changed, {})

    def test_archive_check(self):
        """Rough test of our machinery to support showing visualizations in
        Jupyter notebooks without actually spoofing the notebook
        """
        archive = ArchiveCheck(self.archiver.path)

        # Make sure this _get_uuid actually works
        self.assertEqual(archive._get_uuid(), archive.uuid)

        expected = set([
            'metadata.yaml',
            'data',
            'checksums.sha512',
            'provenance',
            'annotations',
            'VERSION'
        ])

        if not self.archiver.annotations_dir.exists():
            expected.discard('annotations')
        observed = set(file for file in archive.relative_iterdir())
        self.assertEqual(observed, expected)

    def test_format_registry(self):
        """
        Deadman switches to assert the following:
            - The archiver's `_FORMAT_REGISTRY` and archive_parser's
            `FORMAT_REGISTRY` match exactly
            - The `CURRENT_FORMAT_VERSION` matches the last key
            in each `FORMAT_REGISTRY`
        """
        self.assertEqual(Archiver._FORMAT_REGISTRY.keys(),
                         FORMAT_REGISTRY.keys())
        self.assertEqual(Archiver.CURRENT_FORMAT_VERSION,
                         list(FORMAT_REGISTRY.keys())[-1])

    def test_annotations_excluded_from_checksum_diff(self):
        fp = os.path.join(self.temp_dir.name, 'archive.qza')
        self.archiver.save(fp)

        artifact = Result.load(fp)
        note = Note(name='mynote', text='my special text')
        artifact.add_annotation(note)

        # confirm that validation doesn't explode
        artifact.validate()

        diff = artifact._archiver.validate_checksums()

        # confirm that all checksum diffs are zero
        self.assertEqual(diff.added, {})
        self.assertEqual(diff.removed, {})
        self.assertEqual(diff.changed, {})

    def test_archive_versions_match_current_format_version(self):
        """
        Another deadman switch to assert that we haven't created a new
        Archive Format without updating the _FORMAT_REGISTRY and the
        CURRENT_FORMAT_VERSION

        If this test fails, it's because a new Archive Format was added
        without being included in the Archiver's format registry
        """
        # pull the listed current archive version from the Archiver
        # syntax for this will look like:
        # rachis.core.archive.format.vwhatever:ArchiveFormat
        current_archive_ver, class_name = \
            Archiver._FORMAT_REGISTRY[
                Archiver.CURRENT_FORMAT_VERSION].split(':')

        # grap all archive format paths
        from rachis.core.archive import format
        paths = os.listdir(format.__path__[0])

        # rip apart each path to grab the format names and import them
        for path in paths:
            if path.endswith('py') and not path.startswith('__init__'):
                importlib.import_module(
                    f'rachis.core.archive.format.{path.split(".py")[0]}'
                    )

        # construct the latest ArchiveFormat based on the format registry
        ArchiveFormat = \
            getattr(importlib.import_module(current_archive_ver), class_name)

        # ensure there are no subclasses (ie new archive versions that
        # havent been added to the format registry)
        self.assertEqual(ArchiveFormat.__subclasses__(), [])


if __name__ == '__main__':
    unittest.main()
