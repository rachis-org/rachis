# ----------------------------------------------------------------------------
# Copyright (c) 2022-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------


from pathlib import Path
import tempfile
import unittest

from rachis.plugin import model
from rachis.plugin.model import SingleFileDirectoryFormat
import rachis.util

from rachis.core.testing.format import IntSequenceFormat
from rachis.core.exceptions import ValidationError, RachisWarning


# Define dummy plugin formats to test with

class AllRequiredDirFmt(model.DirectoryFormat):
    file1 = model.File(r'test_text1.txt', format=IntSequenceFormat,
                       optional=False)
    file2 = model.File(r'test_text2.txt', format=IntSequenceFormat,
                       optional=False)
    file3 = model.File(r'test_text3.txt', format=IntSequenceFormat,
                       optional=False)


class AllRequiredDefaultDirFmt(model.DirectoryFormat):
    file1 = model.File(r'test_text1.txt', format=IntSequenceFormat)
    file2 = model.File(r'test_text2.txt', format=IntSequenceFormat)
    file3 = model.File(r'test_text3.txt', format=IntSequenceFormat)


class OptionalDirFmt(model.DirectoryFormat):
    file1 = model.File(r'test_text1.txt', format=IntSequenceFormat,
                       optional=False)
    file2 = model.File(r'test_text2.txt', format=IntSequenceFormat,
                       optional=False)
    file3 = model.File(r'test_text3.txt', format=IntSequenceFormat,
                       optional=True)

class SingleFileDirFmt(model.DirectoryFormat):
    file1 = model.File(r'test.txt', format=IntSequenceFormat, optional=False)


class TestDirectoryFormat(unittest.TestCase):
    package = 'rachis.plugin.model.tests'

    def get_data_path(self, filename):
        """Convenience method for getting a data asset while testing.

        Test data stored in the ``data/`` dir local to the running test
        can be accessed via this method.

        Parameters
        ----------
        filename : str
            The name of the file to look up.

        Returns
        -------
        filepath : str
            The materialized filepath to the requested test data.

        """
        fp = rachis.util.get_filepath_from_package(
            self.package, 'data/%s' % filename)
        return str(fp)

    def test_fails_missing_required(self):
        files_dir_fp = self.get_data_path('test_text_files/')

        with self.assertRaisesRegex(
            ValidationError, "Missing one or more files for"
                             " AllRequiredDirFmt"):

            format_object = AllRequiredDirFmt(
                                files_dir_fp,
                                mode='r',
                                )

            format_object.validate()

    def test_fails_missing_with_optional_default(self):
        files_dir_fp = self.get_data_path('test_text_files/')

        with self.assertRaisesRegex(ValidationError,
                                    "Missing one or more files for "
                                    "AllRequiredDefaultDirFmt"):
            format_object = AllRequiredDefaultDirFmt(
                                files_dir_fp,
                                mode='r',
                                )
            format_object.validate()

    def test_passes_with_missing_optional(self):
        files_dir_fp = self.get_data_path('test_text_files/')

        format_object = OptionalDirFmt(
                            files_dir_fp,
                            mode='r',
                            )

        format_object.validate()

    def test_fails_on_unknown_file(self):
        files_dir_fp = self.get_data_path('test_text_files_extra/')
        with self.assertRaisesRegex(ValidationError,
                                    ".*Unrecognized file.*"):

            format_object = AllRequiredDirFmt(
                                files_dir_fp,
                                mode='r',
                                )
            format_object.validate()

    def test_single_file_dirfmt_errors_with_more_than_one_file(self):
        DummySingleFileDirFmt = SingleFileDirectoryFormat(
            'DummySingleFileDirFmt', r'ints.\.txt', IntSequenceFormat
        )

        with tempfile.TemporaryDirectory() as tempdir:
            with open(Path(tempdir) / 'ints1.txt', 'w') as fh:
                fh.write('1\n5\n')
            with open(Path(tempdir) / 'ints2.txt', 'w') as fh:
                fh.write('7\n8\n')

            with self.assertRaisesRegex(
                ValidationError, r'should contain exactly one file.*found 2'
            ):
                DummySingleFileDirFmt(path=tempdir, mode='r').validate()

    def test_single_empty_file_warns(self):
        files_dir_fp = self.get_data_path('test_text_files_single/')
        with self.assertWarns(RachisWarning):
            format = SingleFileDirFmt(files_dir_fp, mode='r')
            format.validate()
