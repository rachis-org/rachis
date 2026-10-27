# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Names and hardlinks for cache ownership.

Directory references link the target's canonical ``.iref``. Pack references
link the pack itself. Filesystem transactions and GC policy belong to the
calling cache; this module supplies the same link/release operations for both.
"""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import uuid


ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_ACQUISITION = re.compile(r"_([a-z2-7]{20})_\.(.+)")


def new_id():
    return "".join(secrets.choice(ALPHABET) for _ in range(20))


def relative_path(value):
    """Validate lexical member/marker names without filesystem coercion."""
    value = str(value)
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(p in ("", ".", "..") for p in value.rstrip("/").split("/"))
    ):
        raise ValueError(f"Invalid cache-relative path: {value!r}")
    return path


def category_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
        raise ValueError(f"Invalid RefDir category: {value!r}")
    return value


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def shared_mkdir(path, parents=False, exist_ok=False):
    """Set group publication permissions before exposing new directories."""
    path = Path(path)
    if parents and not path.parent.exists():
        shared_mkdir(path.parent, parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        if not exist_ok or not path.is_dir():
            raise
    else:
        os.chmod(path, 0o2770)
        fsync_dir(path.parent)


def durable_write(path, data):
    path = Path(path)
    shared_mkdir(path.parent, parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o660)


def target_name(cache_root, target, acquisition=None):
    """Encode a canonical marker or pack pathname in an ownership entry."""
    relative = Path(target).relative_to(cache_root)
    parts = relative_path(relative).parts
    if parts[-1] == ".iref":
        components, suffix = parts[:-1], ".iref"
    elif relative.suffix == ".zip":
        components, suffix = (*parts[:-1], relative.stem), ".zip"
    else:
        raise ValueError(
            "Reference targets must be canonical markers or packs"
        )
    if len(components) != 4 or components[0] not in ("mutable", "immutable"):
        raise ValueError(f"Invalid reference target: {relative}")
    if any("." in part or part.startswith("_") for part in components):
        raise ValueError("Reference path components cannot contain dots")
    category_name(components[1])
    namespace, category, shard, identity = components
    if namespace == "mutable":
        if (
            suffix != ".iref"
            or len(identity) != 20
            or any(char not in ALPHABET for char in identity)
        ):
            raise ValueError("Invalid mutable reference target")
        expected_shard = identity[:2]
    else:
        if (suffix == ".iref" and category != "archives") or (
            suffix == ".zip" and category not in ("annotations", "provenance")
        ):
            raise ValueError("Invalid immutable reference target")
        if str(uuid.UUID(identity)) != identity:
            raise ValueError("Noncanonical immutable UUID")
        expected_shard = identity[:3]
    if shard != expected_shard:
        raise ValueError("Reference shard disagrees with target identity")
    name = ".".join(components) + suffix
    if acquisition is not None:
        if len(acquisition) != 20 or any(
            c not in ALPHABET for c in acquisition
        ):
            raise ValueError("Invalid acquisition ID")
        name = f"_{acquisition}_.{name}"
    return name


def target_path(cache_root, name):
    """Decode a dependency name without reading its marker contents."""
    if name.startswith(".pending."):
        name = name[len(".pending.") :]
    if name.startswith("_"):
        match = _ACQUISITION.fullmatch(name)
        if match is None:
            raise ValueError(f"Invalid acquisition entry: {name!r}")
        name = match[2]
    parts = name.split(".")
    if len(parts) != 5 or parts[0] not in ("mutable", "immutable"):
        raise ValueError(f"Invalid reference entry: {name!r}")
    category_name(parts[1])
    if parts[-1] == "iref":
        relative = PurePosixPath(*parts[:-1], ".iref")
    elif parts[-1] == "zip" and parts[0] == "immutable":
        relative = PurePosixPath(*parts[:3], parts[3] + ".zip")
    else:
        raise ValueError(f"Invalid reference entry: {name!r}")
    relative_path(relative)
    target = Path(cache_root) / relative
    target_name(cache_root, target)
    return target


@dataclass(frozen=True)
class Reference:
    """One exact ownership entry and its canonical target.

    Releasing this token only removes its entry. It never removes the target.
    Callers must hold the cache metadata lock during link and release.
    """

    entry: Path
    target: Path

    @classmethod
    def link(cls, cache_root, owner, target, *, acquisition=False, name=None):
        target = Path(target)
        if name is not None and (
            Path(name).name != name or name in ("", ".", "..")
        ):
            raise ValueError(
                "Ownership entry names must be one path component"
            )
        entry = Path(owner) / (
            name
            or target_name(
                cache_root, target, new_id() if acquisition else None
            )
        )
        try:
            os.link(target, entry)
        except FileExistsError:
            if not entry.samefile(target):
                raise ValueError(
                    f"Ownership entry has a different target: {entry}"
                )
        fsync_dir(entry.parent)
        return cls(entry, target)

    @classmethod
    def read(cls, cache_root, entry, *, named=False):
        """Read logical keys/markers, or decode dependency filenames."""
        entry = Path(entry)
        if entry.name == ".iref" or named:
            value = entry.read_text()
            if not value.endswith("\n") or value.count("\n") != 1:
                raise ValueError(f"Malformed ownership marker: {entry}")
            relative = relative_path(value[:-1])
            target = Path(cache_root) / relative / ".iref"
            target_name(cache_root, target)
        else:
            target = target_path(cache_root, entry.name)
        if not target.resolve().is_relative_to(Path(cache_root).resolve()):
            raise ValueError("Ownership target escapes cache")
        return cls(entry, target)

    def release(self):
        self.entry.unlink(missing_ok=True)
        if self.entry.parent.exists():
            fsync_dir(self.entry.parent)


def directory_references(cache_root, owner):
    """Inspect only root dependency entries, never the owner's own marker."""
    for entry in Path(owner).glob("*.iref"):
        if entry.name != ".iref":
            yield Reference.read(cache_root, entry)


def references(cache_root, owner):
    """Enumerate managed links without traversing payload contents."""
    owner = Path(owner)
    yield from directory_references(cache_root, owner)
    for entry in owner.glob("*.zip"):
        if entry.name.startswith(("immutable.", "_")):
            yield Reference.read(cache_root, entry)
    for category in ("annotations", "provenance"):
        namespace = owner / category
        if namespace.is_symlink():
            continue
        for entry in namespace.glob("*.zip"):
            identity = str(uuid.UUID(entry.stem))
            target = (
                Path(cache_root)
                / "immutable"
                / category
                / identity[:3]
                / entry.name
            )
            yield Reference(entry, target)
