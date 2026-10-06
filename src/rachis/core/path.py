# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import sys
import pathlib
import shutil
import distutils
import tempfile
import weakref

_SHIM_PATHLIB = False
if sys.version_info.major == 3 and sys.version_info.minor < 12:
    _SHIM_PATHLIB = True


# Works in py3.11 and 3.12, same logic as __new__ in 3.12
_ConcretePath = pathlib.WindowsPath if os.name == 'nt' else pathlib.PosixPath


def _party_parrot(self, *args):
    raise TypeError("Cannot mutate %r." % self)


def _init_path(cls, is_dir, prefix=None, scoped=False):
    from rachis.core.cache import get_cache

    cache = get_cache()
    if cache.CURRENT_FORMAT_VERSION == 'v2':
        directory = cache.acquire_directory()
        if is_dir:
            path = directory.path
        else:
            path = directory.path / 'file'
            path.touch()
        return (str(path), directory) if scoped else str(path)
    tmp_path = cache.get_tmp_path()

    if hasattr(cls, 'DEFAULT_PREFIX'):
        default_prefix = cls.DEFAULT_PREFIX
    else:
        default_prefix = f'rachis-{cls.__name__}-'

    if prefix is None:
        prefix = default_prefix
    elif not prefix.startswith(default_prefix):
        prefix = default_prefix + prefix

    if is_dir:
        path = tempfile.mkdtemp(prefix=prefix, dir=tmp_path)
    else:
        fd, path = tempfile.mkstemp(prefix=prefix, dir=tmp_path)
        # fd is now assigned to our process table, but we don't need to do
        # anything with the file. We will call `open` on the `name` later
        # producing a different file descriptor, so close this one to
        # prevent a resource leak.
        os.close(fd)

    return (path, None) if scoped else path


class OwnedPath(_ConcretePath):

    if _SHIM_PATHLIB:
        def __new__(cls, *args, **kwargs):
            obj = super().__new__(cls, *args, **kwargs)
            obj.__init__(*args, **kwargs)
            return obj

    def __init__(self, *args, **kwargs):
        if not _SHIM_PATHLIB:
            super().__init__(*args, **kwargs)
        self._user_owned = True

    def _copy_dir_or_file(self, other):
        if self.is_dir():
            return distutils.dir_util.copy_tree(str(self), str(other))
        else:
            return shutil.copy(str(self), str(other))

    def _destruct(self):
        if self.is_dir():
            distutils.dir_util.remove_tree(str(self))
        else:
            self.unlink()

    def _move_or_copy(self, other):
        if self._user_owned:
            return self._copy_dir_or_file(other)
        else:
            # Certain networked filesystems will experience a race
            # condition on `rename`, so fall back to copying.
            try:
                return _ConcretePath.rename(self, other)
            except (FileExistsError, OSError) as e:
                # OSError errno 18 is cross device link, if we have this error
                # we can solve it by copying. If we have a different OSError we
                # still want to explode. FileExistsErrors are apparently
                # instances of OSError, so we also make sure we don't have one
                # of them when we explode
                if isinstance(e, OSError) and e.errno != 18 and \
                        not isinstance(e, FileExistsError):
                    raise e
                copied = self._copy_dir_or_file(other)
                self._destruct()
                return copied

    def with_segments(self, *args):
        path = os.path.join(*args)
        return self.__class__(path)


class InPath(OwnedPath):
    def __init__(self, path):
        super().__init__(path)
        self.__backing_path = path
        self._directory = getattr(path, '_directory', None)
        if hasattr(path, '_user_owned'):
            self._user_owned = path._user_owned

    chmod = lchmod = rename = replace = rmdir = symlink_to = touch = unlink = \
        write_bytes = write_text = _party_parrot

    def open(self, mode='r', buffering=-1, encoding=None, errors=None,
             newline=None):
        if 'w' in mode or '+' in mode or 'a' in mode:
            _party_parrot(self)
        return super().open(mode=mode, buffering=buffering, encoding=encoding,
                            errors=errors, newline=newline)


class OutPath(OwnedPath):
    @classmethod
    def _destruct(cls, path):
        if not os.path.exists(path):
            return

        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)

    if _SHIM_PATHLIB:
        def __new__(cls, dir=False):
            path, directory = _init_path(cls, is_dir=dir, scoped=True)
            obj = super().__new__(cls, path)
            obj._directory = directory
            if directory is None:
                obj._destructor = weakref.finalize(
                    obj, obj._destruct, str(obj))
            else:
                obj._destructor = weakref.finalize(
                    obj, directory.cache._deallocate, directory.ref_id)
            return obj
    else:
        def __init__(self, dir=False):
            """
            Create a tempfile, return pathlib.Path reference to it.
            """
            path, directory = _init_path(
                self.__class__, is_dir=dir, scoped=True)
            super().__init__(path)
            self._directory = directory
            if directory is None:
                self._destructor = weakref.finalize(
                    self, self._destruct, str(self))
            else:
                self._destructor = weakref.finalize(
                    self, directory.cache._deallocate, directory.ref_id)

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        self._destructor()

    def with_segments(self, *args):
        path = os.path.join(*args)
        return _ConcretePath(path)


class InternalDirectory(_ConcretePath):
    DEFAULT_PREFIX = 'rachis-'

    @classmethod
    def _validate_init(cls, *args, prefix=None):
        if args and prefix is not None:
            raise TypeError("Cannot pass a path and a prefix at the same time")

    if _SHIM_PATHLIB:
        def __new__(cls, *args, prefix=None):
            cls._validate_init(*args, prefix=prefix)
            if args == ():
                path, directory = _init_path(cls, is_dir=True, prefix=prefix,
                                             scoped=True)
                obj = super().__new__(cls, path)
                obj._directory = directory
                return obj
            else:
                # pickle's reduce is happening and we are py3.11
                return super().__new__(cls, *args)
    else:
        def __init__(self, *args, prefix=None):
            self._validate_init(*args, prefix=prefix)
            if args:
                super().__init__(*args)
                self._directory = None
            else:
                path, directory = _init_path(self.__class__, is_dir=True,
                                             prefix=prefix, scoped=True)
                super().__init__(path)
                self._directory = directory

    def __truediv__(self, path):
        # We don't want to create self-destructing paths when using the join
        # operator
        return _ConcretePath(str(self), path)

    def __rtruediv__(self, path):
        # Same reasoning as truediv
        return _ConcretePath(path, str(self))

    def with_segments(self, *args):
        path = os.path.join(*args)
        return _ConcretePath(path)


class ArchivePath(InternalDirectory):
    DEFAULT_PREFIX = 'rachis-archive-'


class ProvenancePath(InternalDirectory):
    DEFAULT_PREFIX = 'rachis-provenance-'
