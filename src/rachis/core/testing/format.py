# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from rachis.plugin import TextFileFormat, ValidationError
import rachis.plugin.model as model


class IntSequenceFormat(TextFileFormat):
    """
    A sequence of integers stored on new lines in a file. Since this is a
    sequence, the integers have an order and repetition of elements is
    allowed. Sequential values must have an inter-value distance other than 3
    to be valid.

    """
    def _validate_n_ints(self, n):
        with self.open() as fh:
            last_val = None
            for idx, line in enumerate(fh, 1):
                if n is not None and idx >= n:
                    break
                try:
                    val = int(line.rstrip('\n'))
                except (TypeError, ValueError):
                    raise ValidationError("Line %d is not an integer." % idx)
                if last_val is not None and last_val + 3 == val:
                    raise ValidationError("Line %d is 3 more than line %d"
                                          % (idx, idx-1))
                last_val = val

    def _validate_(self, level):
        record_map = {'min': 5, 'max': None}
        self._validate_n_ints(record_map[level])


class IntSequenceFormatV2(IntSequenceFormat):
    """
    Same as IntSequenceFormat, but has a header "VERSION 2"

    """
    def _validate_(self, level):
        with self.open() as fh:
            if fh.readline() != 'VERSION 2\n':
                raise ValidationError("Missing header: VERSION 2")


class MappingFormat(TextFileFormat):
    """
    A mapping of keys to values stored in a TSV file. Since this is a
    mapping, key-value pairs do not have an order and duplicate keys are
    disallowed.

    """
    def _validate_(self, level):
        with self.open() as fh:
            for line, idx in zip(fh, range(1, 6)):
                cells = line.rstrip('\n').split('\t')
                if len(cells) != 2:
                    raise ValidationError("Line %d does not have exactly 2 "
                                          "elements seperated by a tab." % idx)


class SingleIntFormat(TextFileFormat):
    """
    Exactly one int on a single line in the file.

    """
    def _validate_(self, level):
        with self.open() as fh:
            try:
                int(fh.readline().rstrip('\n'))
            except (TypeError, ValueError):
                raise ValidationError("File does not contain an integer")
            if fh.readline():
                raise ValidationError("Too many lines in file.")


IntSequenceDirectoryFormat = model.SingleFileDirectoryFormat(
    'IntSequenceDirectoryFormat', 'ints.txt', IntSequenceFormat)

IntSequenceV2DirectoryFormat = model.SingleFileDirectoryFormat(
    'IntSequenceV2DirectoryFormat', 'integers.txt', IntSequenceFormatV2)


class IntSequenceMultiFileDirectoryFormat(model.DirectoryFormat):
    pass


# This could have been a `SingleFileDirectoryFormat`, but isn't for testing
# purposes
class MappingDirectoryFormat(model.DirectoryFormat):
    mapping = model.File('mapping.tsv', format=MappingFormat)


class FourIntsDirectoryFormat(model.DirectoryFormat):
    """
    A sequence of exactly four integers stored across multiple files, some of
    which are in a nested directory. Each file contains a single integer.
    Since this is a sequence, the integers have an order (corresponding to
    filename) and repetition of elements is allowed.

    """
    single_ints = model.FileCollection(
        r'file[1-2]\.txt|nested/file[3-4]\.txt', format=SingleIntFormat)

    @single_ints.set_path_maker
    def single_ints_path_maker(self, num):
        if not 0 < num < 5:
            raise ValueError("`num` must be 1-4, not %r." % num)
        if num > 2:
            return 'nested/file%d.txt' % num
        else:
            return 'file%d.txt' % num


class RedundantSingleIntDirectoryFormat(model.DirectoryFormat):
    """
    Two files of SingleIntFormat which are exactly the same.

    """
    int1 = model.File('file1.txt', format=SingleIntFormat)
    int2 = model.File('file2.txt', format=SingleIntFormat)

    def _validate_(self, level):
        if self.int1.view(int) != self.int2.view(int):
            raise ValidationError("file1.txt does not match file2.txt")


class UnimportableFormat(TextFileFormat):
    """
    Unimportable format used for testing.
    """


UnimportableDirectoryFormat = model.SingleFileDirectoryFormat(
    'UnimportableDirectoryFormat', 'ints.txt', UnimportableFormat)


class EchoFormat(TextFileFormat):
    def _validate_(self, level):
        pass  # Anything is a valid echo file


EchoDirectoryFormat = model.SingleFileDirectoryFormat(
    'EchoDirectoryFormat', 'echo.txt', EchoFormat)


class Cephalapod(TextFileFormat):
    """
    Class that inherits from text file format.
    Used for testing validator sorting.
    """


CephalapodDirectoryFormat = model.SingleFileDirectoryFormat(
    'CephalapodDirectoryFormat', 'squids.tsv', Cephalapod)


class ImportableOnlyFormat(TextFileFormat):
    """
    A format that can only be transformed from.
    """


class ExportableOnlyFormat(TextFileFormat):
    """
    A format that can only be transformed to.
    """

class FirstStepFormat(model.DirectoryFormat):
    """
    A format for testing transitive transformers
    """

class SecondStepFormat(model.DirectoryFormat):
    """
    A format for testing transitive transformers
    """

class ThirdStepFormat(model.DirectoryFormat):
    """
    A format for testing transitive transformers
    """

class FourthStepFormat(model.DirectoryFormat):
    """
    A format for testing transitive transformers
    """

class FifthStepFormat(model.DirectoryFormat):
    """
    A format for testing transitive transformers
    """
