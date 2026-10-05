# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import abc
import shutil

from rachis.core import transform
from .base import FormatBase, ValidationError, _check_validation_level


class _FileFormat(FormatBase, metaclass=abc.ABCMeta):

    # Valid levels are 0 through 9. _ZipArchive.save passes 1 through 9 to
    # ZipFile.write as `compresslevel`; 1 is fastest and 9 makes the
    # smallest archive. 0 selects `ZIP_STORED`, leaving the file uncompressed.
    COMPRESSION = None

    @classmethod
    def _validate_compression(cls):
        level = cls.COMPRESSION
        if level is not None and (
                isinstance(level, bool) or not isinstance(level, int)
                or not 0 <= level <= 9):
            raise ValueError(
                f'{cls.__name__}.COMPRESSION must be an integer from 0 to 9, '
                f'not {level!r}.')

    def validate(self, level='max'):
        _check_validation_level(level)

        if not self.path.is_file():
            raise ValidationError("%s is not a file." % self.path)

        if hasattr(self, '_validate_'):
            try:
                self._validate_(level)
            except ValidationError as e:
                raise ValidationError(
                    "%s is not a(n) %s file:\n\n%s"
                    % (self.path, self.__class__.__name__, str(e))
                    ) from e
        # TODO: remove this branch
        elif hasattr(self, 'sniff'):
            if not self.sniff():
                raise ValidationError("%s is not a(n) %s file"
                                      % (self.path, self.__class__.__name__))

        # TODO: define an abc.abstractmethod for `validate` when sniff is
        # removed instead of this
        else:
            raise NotImplementedError("%r does not implement validate."
                                      % type(self))

    def view(self, view_type):
        from_type = transform.ModelType.from_view_type(self.__class__)
        to_type = transform.ModelType.from_view_type(view_type)

        transformation = from_type.make_transformation(to_type)
        return transformation(self)

    def save(self, path, ext=None):
        path = str(path)  # in case of pathlib.Path
        path = path.rstrip('.')

        if ext is not None:
            ext = '.' + ext.lstrip('.')
            if not path.endswith(ext):
                path += ext

        shutil.copyfile(self.path, path)
        return path


class TextFileFormat(_FileFormat):
    def open(self):
        mode = 'r' if self._mode == 'r' else 'r+'
        # ignore BOM only when reading, do not emit BOM on write
        encoding = 'utf-8-sig' if mode == 'r' else 'utf-8'
        return self.path.open(mode=mode, encoding=encoding)


class BinaryFileFormat(_FileFormat):
    def open(self):
        mode = 'rb' if self._mode == 'r' else 'r+b'
        return self.path.open(mode=mode)
