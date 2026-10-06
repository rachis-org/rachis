"""Ownership stat selection, ABI conversion, and fallback semantics."""

import ctypes
import errno
import os
from types import SimpleNamespace

import pytest

from rachis.core import cache_stat


def successful_statx(calls, mask=cache_stat._OWNERSHIP_MASK):
    def statx(dirfd, path, flags, requested, buffer):
        calls.append((dirfd, path, flags, requested))
        result = ctypes.cast(
            buffer, ctypes.POINTER(cache_stat._Statx)
        ).contents
        result.stx_mask = mask
        result.stx_dev_major = 8
        result.stx_dev_minor = 17
        result.stx_ino = 2**40 + 123
        result.stx_nlink = 2
        return 0

    return statx


def test_force_sync_returns_fresh_count_without_portable_stat(monkeypatch):
    calls = []
    monkeypatch.setattr(cache_stat, "_statx", successful_statx(calls))

    def stale_stat(path):
        pytest.fail("successful forced-sync statx must not use cached stat")

    monkeypatch.setattr(cache_stat.os, "stat", stale_stat)
    result = cache_stat.ownership_stat("marker")
    assert result == (os.makedev(8, 17), 2**40 + 123, 2)
    assert calls == [(-100, b"marker", 0x2000, 0x0104)]


@pytest.mark.parametrize("error", [errno.EINVAL, errno.EOPNOTSUPP])
def test_force_sync_unsupported_retries_ordinary_statx(monkeypatch, error):
    calls = []
    success = successful_statx(calls)

    def statx(dirfd, path, flags, requested, buffer):
        if flags:
            calls.append((dirfd, path, flags, requested))
            ctypes.set_errno(error)
            return -1
        return success(dirfd, path, flags, requested, buffer)

    monkeypatch.setattr(cache_stat, "_statx", statx)
    assert cache_stat.ownership_stat("marker").st_nlink == 2
    assert [call[2] for call in calls] == [0x2000, 0]


@pytest.mark.parametrize("error", [errno.ENOSYS, errno.EOPNOTSUPP])
def test_unsupported_statx_falls_back_to_stat(monkeypatch, error):
    calls = []

    def statx(dirfd, path, flags, requested, buffer):
        calls.append(flags)
        ctypes.set_errno(error)
        return -1

    monkeypatch.setattr(cache_stat, "_statx", statx)
    monkeypatch.setattr(
        cache_stat.os, "stat",
        lambda path: SimpleNamespace(st_dev=1, st_ino=3, st_nlink=4),
    )
    assert cache_stat.ownership_stat("marker") == (1, 3, 4)
    assert calls == ([0x2000] if error == errno.ENOSYS else [0x2000, 0])


@pytest.mark.parametrize("mask", [0, 0x0004, 0x0100])
def test_incomplete_statx_fields_fall_back_without_mixing(monkeypatch, mask):
    calls = []
    monkeypatch.setattr(cache_stat, "_statx", successful_statx(calls, mask))
    monkeypatch.setattr(
        cache_stat.os, "stat",
        lambda path: SimpleNamespace(st_dev=1, st_ino=3, st_nlink=4),
    )
    assert cache_stat.ownership_stat("marker") == (1, 3, 4)
    assert [call[2] for call in calls] == [0x2000, 0]


@pytest.mark.parametrize(
    "error", [errno.ENOENT, errno.EACCES, errno.EIO, errno.ESTALE]
)
def test_filesystem_errors_propagate_without_fallback(monkeypatch, error):
    calls = []

    def statx(dirfd, path, flags, requested, buffer):
        calls.append(flags)
        ctypes.set_errno(error)
        return -1

    monkeypatch.setattr(cache_stat, "_statx", statx)
    with pytest.raises(OSError) as exc:
        cache_stat.ownership_stat("marker")
    assert exc.value.errno == error
    assert exc.value.filename == "marker"
    assert calls == [0x2000]


def test_missing_statx_uses_portable_stat(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_stat, "_statx", None)
    marker = tmp_path / "marker"
    marker.touch()
    owner = tmp_path / "owner"
    owner.hardlink_to(marker)
    expected = marker.stat()
    assert cache_stat.ownership_stat(marker) == (
        expected.st_dev, expected.st_ino, 2,
    )
    owner.unlink()
    assert cache_stat.ownership_stat(marker).st_nlink == 1
    marker.unlink()
    with pytest.raises(FileNotFoundError):
        cache_stat.ownership_stat(marker)


def test_native_ownership_stat_tracks_links(tmp_path):
    marker = tmp_path / "marker"
    marker.touch()
    owner = tmp_path / "owner"
    owner.hardlink_to(marker)
    expected = marker.stat()
    assert cache_stat.ownership_stat(marker) == (
        expected.st_dev, expected.st_ino, expected.st_nlink,
    )
    owner.unlink()
    assert cache_stat.ownership_stat(marker).st_nlink == 1
    marker.unlink()
    with pytest.raises(FileNotFoundError):
        cache_stat.ownership_stat(marker)


def test_embedded_null_path_rejected_before_statx(monkeypatch):
    calls = []
    monkeypatch.setattr(cache_stat, "_statx", successful_statx(calls))
    with pytest.raises(ValueError, match="null"):
        cache_stat.ownership_stat(b"marker\0other")
    assert calls == []


def test_statx_abi_layout():
    assert ctypes.sizeof(cache_stat._Statx) == 256
    assert cache_stat._Statx.stx_nlink.offset == 16
    assert cache_stat._Statx.stx_ino.offset == 32
    assert cache_stat._Statx.stx_dev_major.offset == 136
    assert cache_stat._Statx.stx_dev_minor.offset == 140


def test_load_statx_on_other_platform(monkeypatch):
    monkeypatch.setattr(cache_stat.sys, "platform", "darwin")
    assert cache_stat._load_statx() is None


def test_load_statx_without_libc_symbol(monkeypatch):
    monkeypatch.setattr(cache_stat.sys, "platform", "linux")
    monkeypatch.setattr(cache_stat.ctypes, "CDLL", lambda *a, **kw: object())
    assert cache_stat._load_statx() is None
