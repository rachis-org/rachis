# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Read ownership attributes using the freshest available stat operation."""

import ctypes
import errno
import os
import sys
from typing import NamedTuple


class OwnershipStat(NamedTuple):
    st_dev: int
    st_ino: int
    st_nlink: int


class _Statx(ctypes.Structure):
    # Linux's fixed 256-byte statx ABI. Unused timestamps and trailing fields
    # remain opaque so newer kernels can fill them without changing our layout.
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("_spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("_timestamps", ctypes.c_uint64 * 8),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("_tail", ctypes.c_uint64 * 14),
    ]


_AT_FDCWD = -100
_AT_STATX_FORCE_SYNC = 0x2000
_STATX_NLINK = 0x0004
_STATX_INO = 0x0100
_OWNERSHIP_MASK = _STATX_NLINK | _STATX_INO
_UNSUPPORTED = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}


def _load_statx():
    if sys.platform != "linux":
        return None
    try:
        statx = ctypes.CDLL(None, use_errno=True).statx
    except (AttributeError, OSError):
        return None
    statx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_Statx),
    ]
    statx.restype = ctypes.c_int
    return statx


_statx = _load_statx()


def ownership_stat(path):
    """Prefer forced-sync statx, then ordinary statx, then portable stat.

    Fall back only for unavailable operations or missing requested fields.
    Filesystem errors propagate, so a failed fresh read never authorizes GC
    using a potentially cached read instead. All attributes come from the
    same attempt; callers still serialize acquisition and retirement.
    """
    if _statx is not None:
        encoded = os.fsencode(path)
        if b"\0" in encoded:
            raise ValueError("embedded null byte")
        for flags in (_AT_STATX_FORCE_SYNC, 0):
            result = _Statx()
            if _statx(
                _AT_FDCWD, encoded, flags, _OWNERSHIP_MASK,
                ctypes.byref(result),
            ) == 0:
                if result.stx_mask & _OWNERSHIP_MASK == _OWNERSHIP_MASK:
                    return OwnershipStat(
                        os.makedev(result.stx_dev_major, result.stx_dev_minor),
                        result.stx_ino,
                        result.stx_nlink,
                    )
                continue
            error = ctypes.get_errno()
            if error == errno.ENOSYS:
                break
            if error not in _UNSUPPORTED:
                raise OSError(error, os.strerror(error), os.fsdecode(encoded))
    result = os.stat(path)
    return OwnershipStat(result.st_dev, result.st_ino, result.st_nlink)
