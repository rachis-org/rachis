# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import os
import pytest
import shutil
import tempfile
import unittest
import contextlib

import rachis
from rachis import Artifact
from rachis.sdk.plugin_manager import PluginManager
from rachis.core.archive.archiver import Archiver
from rachis.core.archive.provenance_lib.archive_parser import (FORMAT_REGISTRY,
                                                               ArchiveParser)

from .testing_utilities import (
    monkeypatch_archive_version, monkeypatch_framework_version
)
from ..util import _VERSION_MATCHER, parse_version


@contextlib.contextmanager
def monkey_patch_format_registry(patch):
    original = FORMAT_REGISTRY.copy()

    try:
        FORMAT_REGISTRY.clear()
        FORMAT_REGISTRY.update(patch)
        yield

    finally:
        FORMAT_REGISTRY.clear()
        FORMAT_REGISTRY.update(original)


class TestVersionParser(unittest.TestCase):
    def setUp(self):
        self.pm = PluginManager()
        self.dp = self.pm.plugins['dummy-plugin']
        self.tempdir = tempfile.mkdtemp(
            prefix='rachis-test-version-parser-temp-'
        )
        self.framework_version_exp = rachis.__version__
        self.archive_version_exp = Archiver.CURRENT_FORMAT_VERSION

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_parse_version(self):
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])

        actual = parse_version(int_seq._archiver)
        self.assertEqual(
            actual, (self.archive_version_exp, self.framework_version_exp)
        )

    def test_parse_version_old_archive_format(self):
        archive_version_exp = '2'

        with monkeypatch_archive_version(archive_version_exp):
            int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])

        actual = parse_version(int_seq._archiver)
        self.assertEqual(
            actual, (archive_version_exp, self.framework_version_exp)
        )

    def test_artifact_with_commit_version(self):
        framework_version_exp = '2022.8.0+29.gb053440'

        with monkeypatch_framework_version(framework_version_exp):
            int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])

        actual = parse_version(int_seq._archiver)
        self.assertEqual(
            actual, (self.archive_version_exp, framework_version_exp)
        )

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_parse_version_no_VERSION_file(self):
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])
        os.remove(int_seq._archiver.path / 'VERSION')

        with self.assertRaisesRegex(FileNotFoundError,
                                    'No such file or directory:.*VERSION'):
            parse_version(int_seq._archiver)

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_parse_version_VERSION_file_missing_archive_field(self):
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])

        with open(
                os.path.join(int_seq._archiver.path / 'VERSION'), 'r+') as fh:
            lines = fh.readlines()
            missing_archive_lines = [lines[0], lines[2]]

            fh.seek(0)
            for line in missing_archive_lines:
                fh.write(line)

        with self.assertRaisesRegex(ValueError, 'VERSION.*out of spec.*'):
            parse_version(int_seq._archiver)

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_parse_version_VERSION_file_extra_field(self):
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])

        with open(
                os.path.join(int_seq._archiver.path / 'VERSION'), 'a') as fh:
            fh.write('fourth line\n')

        with self.assertRaisesRegex(ValueError, 'VERSION.*out of spec.*'):
            parse_version(int_seq._archiver)

    '''
    Tests of the regex match itself below
    '''
    def test_version_too_short(self):
        short = (
            r'QIIME 2\n'
            r'archive: 4'
        )
        self.assertNotRegex(short, _VERSION_MATCHER)

    def test_version_too_long(self):
        long = (
            r'QIIME 2\n'
            r'archive: 4\n'
            r'framework: 2019.8.1.dev0\n'
            r'This line should not be here'
        )
        self.assertNotRegex(long, _VERSION_MATCHER)

    splitvm = _VERSION_MATCHER.split(sep=r'\n')
    re_l1, re_l2, re_l3 = splitvm

    def test_line1_good(self):
        self.assertRegex('QIIME 2\n', self.re_l1)

    def test_line1_bad(self):
        self.assertNotRegex('SHIMMY 2\n', self.re_l1)

    def test_archive_version_1digit_numeric(self):
        self.assertRegex('archive: 1\n', self.re_l2)

    # modified to accommodate semantic versioning for <=7.0
    def test_archive_version_2digit_numeric(self):
        self.assertRegex('archive: 12.0\n', self.re_l2)

    def test_archive_version_bad(self):
        self.assertNotRegex('agama agama\n', self.re_l2)

    def test_archive_version_3digit_numeric(self):
        self.assertNotRegex('archive: 123\n', self.re_l2)

    def test_archive_version_nonnumeric(self):
        self.assertNotRegex('archive: 1a\n', self.re_l2)

    def test_fmwk_version_good_semver(self):
        self.assertRegex('framework: 2.0.6', self.re_l3)

    def test_fmwk_version_good_semver_dev(self):
        self.assertRegex('framework: 2.0.6.dev0', self.re_l3)

    def test_fmwk_version_good_year_month_patch(self):
        self.assertRegex('framework: 2020.2.0', self.re_l3)

    def test_fmwk_version_good_year_month_patch_2digit_month(self):
        self.assertRegex('framework: 2018.11.0', self.re_l3)

    def test_fmwk_version_good_year_month_patch_dev(self):
        self.assertRegex('framework: 2020.2.0.dev1', self.re_l3)

    def test_fmwk_version_good_ymp_2digit_month_dev(self):
        self.assertRegex('framework: 2020.11.0.dev0', self.re_l3)

    def test_fmwk_version_invalid_month(self):
        self.assertNotRegex('framework: 2020.13.0', self.re_l3)

    def test_fmwk_version_invalid_month_leading_zero(self):
        self.assertNotRegex('framework: 2020.03.0', self.re_l3)

    def test_fmwk_version_invalid_year(self):
        self.assertNotRegex('framework: 1953.3.0', self.re_l3)

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_parser_semantic_versioning_fallback(self):
        int_seq = Artifact.import_data('IntSequence1', [1, 2, 3])

        class DummyParser:
            pass

        # Set this to a really high version so we aren't likely to actually
        # hit it
        with monkey_patch_format_registry({'42.0': DummyParser}):
            with open(int_seq._archiver.path / 'VERSION', 'w') as fh:
                fh.write('QIIME 2\n')
                fh.write('archive: 42.2\n')
                fh.write('framework: 2025.4.0\n')

            parser = ArchiveParser.get_parser(int_seq._archiver)
            self.assertIsInstance(parser, DummyParser)
