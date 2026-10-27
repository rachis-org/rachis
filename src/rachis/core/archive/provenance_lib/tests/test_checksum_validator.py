# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import os
import shutil
import tempfile
import unittest

import pytest

from rachis import Artifact
from rachis.core.archive.archiver import ChecksumDiff
from rachis.sdk.plugin_manager import PluginManager

from .._checksum_validator import validate_checksums, ValidationCode


class ValidateChecksumTests(unittest.TestCase):
    def setUp(self):
        self.pm = PluginManager()
        self.dp = self.pm.plugins['dummy-plugin']
        self.tempdir = tempfile.mkdtemp(
            prefix='rachis-test-checksum-validator-temp-'
        )

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_validate_checksums(self):
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])

        is_valid, diff = validate_checksums(int_seq._archiver)
        self.assertEqual(is_valid, ValidationCode.VALID)
        self.assertEqual(diff, ChecksumDiff({}, {}, {}))

    @pytest.mark.filterwarnings('ignore::UserWarning')
    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_validate_checksums_invalid(self):
        '''
        Mangle an intact v5 Archive so that its checksums.md5 is invalid,
        and then confirm that we're catching all the changes we've made
        Specifically:
        - remove the root `<uuid>/metadata.yaml`
        - add a new file called '<uuid>/tamper.txt`
        - overwrite `<uuid>/provenance/citations.bib`
        '''
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])
        os.remove(int_seq._archiver.path / 'metadata.yaml')

        with open(int_seq._archiver.path / 'tamper.txt', 'w') as fh:
            pass
        with open(
                int_seq._archiver.provenance_dir / 'citations.bib', 'w') as fh:
            fh.write('file overwritten\n')

        is_valid, diff = validate_checksums(int_seq._archiver)

        self.assertEqual(is_valid, ValidationCode.INVALID)
        self.assertEqual(list(diff.added.keys()), ['tamper.txt'])
        self.assertEqual(list(diff.removed.keys()), ['metadata.yaml'])
        self.assertEqual(list(diff.changed.keys()),
                         ['provenance/citations.bib'])

    @pytest.mark.filterwarnings('ignore::UserWarning')
    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_validate_checksums_checksums_missing(self):
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])
        os.remove(int_seq._archiver.path / 'checksums.sha512')

        is_valid, diff = validate_checksums(int_seq._archiver)

        self.assertEqual(is_valid, ValidationCode.INVALID)
        self.assertEqual(diff, None)
