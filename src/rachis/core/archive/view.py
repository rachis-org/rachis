# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Logical archive paths. No implicit extraction or filesystem conversion."""

import contextlib
from functools import cached_property
import fnmatch
import io
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import uuid
import zipfile

from .pack import manifest_bytes, parse_manifest
from ..reference import relative_path
from ..util import from_checksum_format



class LogicalPath:
    def __init__(self, view, member=""):
        self.view = view
        self.member = PurePosixPath(member).as_posix() if member else ""
        if self.member == ".":
            self.member = ""
        if self.member.startswith("/") or ".." in PurePosixPath(member).parts:
            raise ValueError("Archive path escapes logical root")

    def __truediv__(self, member):
        return LogicalPath(self.view, str(PurePosixPath(self.member) / member))

    @property
    def name(self):
        return (
            PurePosixPath(self.member).name if self.member else self.view.uuid
        )

    @property
    def parent(self):
        return LogicalPath(self.view, str(PurePosixPath(self.member).parent))

    @property
    def parts(self):
        return PurePosixPath(self.member).parts

    @property
    def suffix(self):
        return PurePosixPath(self.member).suffix

    def __str__(self):
        return f"{self.view.uuid}/{self.member}"

    def relative_to(self, other):
        value = other.member if isinstance(other, LogicalPath) else str(other)
        return PurePosixPath(self.member).relative_to(value)

    def is_relative_to(self, other):
        try:
            self.relative_to(other)
            return True
        except ValueError:
            return False

    def open(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        if mode not in ("r", "rb", "rt"):
            raise TypeError("Logical archive paths are read-only")
        stream = self.view.open_member(self.member)
        if mode != "rb":
            stream = io.TextIOWrapper(
                stream,
                encoding=encoding or "utf-8",
                errors=errors,
                newline=newline,
            )
        stream.view_parent = self.parent
        return stream

    def read_bytes(self):
        with self.open("rb") as stream:
            return stream.read()

    def read_text(self, encoding="utf-8", errors=None):
        with self.open(encoding=encoding, errors=errors) as stream:
            return stream.read()

    def exists(self):
        return self.view.is_file(self.member) or self.view.is_dir(self.member)

    def is_file(self):
        return self.view.is_file(self.member)

    def is_dir(self):
        return self.view.is_dir(self.member)

    def iterdir(self):
        if not self.is_dir():
            raise NotADirectoryError(str(self))
        prefix = self.member + "/" if self.member else ""
        children = {
            m[len(prefix) :].split("/")[0]
            for m in self.view.members()
            if m.startswith(prefix) and m != self.member
        }
        for name in sorted(children):
            yield self / name

    def rglob(self, pattern):
        prefix = self.member + "/" if self.member else ""
        for member in self.view.members():
            if member.startswith(prefix) and fnmatch.fnmatch(
                PurePosixPath(member).name, pattern
            ):
                yield LogicalPath(self.view, member)

    def glob(self, pattern):
        yield from (
            p for p in self.iterdir() if fnmatch.fnmatch(p.name, pattern)
        )


class LiveArchiveView:
    """Read current members without allocating a retained archive snapshot.

    Pack selection and mutable masks use short metadata transactions. ZIP I/O
    happens after releasing the lock, using the opened revision. Consistency
    across multiple queries belongs to an explicit operation-wide snapshot.
    """

    def __init__(self, cache, ref_id):
        self.cache = cache
        self.ref_id = ref_id
        self.uuid, artifact = cache.attachment(ref_id)
        version_line = (artifact / "VERSION").read_text().splitlines()[1]
        self.version = version_line.split(": ", 1)[1]
        self.root = LogicalPath(self)

    def _locations(self):
        ref = self.cache.object_path("archives", self.ref_id)
        _, artifact = self.cache.attachment(self.ref_id)
        return ref, artifact

    def _locate(self, member):
        from ..cache_v2 import relative_path

        if not member:
            return None
        relative_path(member)
        ref, artifact = self._locations()
        if member == "data" or member.startswith("data/"):
            return "file", artifact / member, None, None
        if member in ("provenance", "annotations"):
            return None
        if "/" not in member:
            if member in ("checksums.sha512", "checksums.md5"):
                algorithm = member.split(".", 1)[1]
                return (
                    "manifest",
                    artifact / ("checksums." + algorithm),
                    algorithm,
                    None,
                )
            return "file", artifact / member, None, None
        category, relative = member.split("/", 1)
        return self._locate_pack(category, relative, ref, artifact)

    def _locate_pack(self, category, relative, ref, artifact):
        if category == "provenance" and self.version != "0":
            if relative == "artifacts":
                return None
            if relative.startswith("artifacts/"):
                identity, _, relative = relative[10:].partition("/")
                # The result's own node is exposed at provenance/, never as
                # an ancestor under provenance/artifacts/.
                if identity == self.uuid:
                    return None
            else:
                identity = self.uuid
            namespace = artifact / "provenance"
        elif category == "annotations":
            identity, _, relative = relative.partition("/")
            namespace = ref / "annotations"
        else:
            return None
        try:
            if str(uuid.UUID(identity)) != identity:
                return None
        except ValueError:
            return None
        return category, namespace / (identity + ".zip"), identity, relative

    def _root_members(self, artifact):
        return {
            p.name
            for p in artifact.iterdir()
            if p.is_file() and p.name != ".iref"
        }

    def _pack_bindings(self, ref, artifact):
        if self.version != "0":
            for path in (artifact / "provenance").glob("*.zip"):
                prefix = (
                    "provenance/"
                    if path.stem == self.uuid
                    else "provenance/artifacts/" + path.stem + "/"
                )
                yield path, prefix
        for path in (ref / "annotations").glob("*.zip"):
            yield path, "annotations/" + path.stem + "/"

    @contextlib.contextmanager
    def _opened_pack(self, location, *, masks=False, refresh=False):
        category, path, identity, relative = location
        cache = self.cache
        if refresh and category == "provenance":
            cache.refresh_provenance(self.uuid, node_id=identity)
        with contextlib.ExitStack() as resources:
            with cache.lock:
                hidden = False
                if masks and category == "provenance":
                    ref, _ = self._locations()
                    hidden = (identity, relative) in read_whiteouts(ref)
                file = resources.enter_context(path.open("rb"))
            pack = resources.enter_context(zipfile.ZipFile(file))
            yield pack, identity + "/" + relative, hidden, resources

    def members(self, *, include_directories=False):
        cache = self.cache
        with contextlib.ExitStack() as resources:
            with cache.lock:
                ref, artifact = self._locations()
                names = self._root_members(artifact)
                files = [
                    (p.stem, prefix, resources.enter_context(p.open("rb")))
                    for p, prefix in self._pack_bindings(ref, artifact)
                ]
            names.update("data/" + name for name in _files(artifact / "data"))
            if include_directories:
                names.update(
                    "data/" + path.relative_to(artifact / "data").as_posix()
                    + "/"
                    for path in (artifact / "data").rglob("*")
                    if path.is_dir()
                )
            for identity, prefix, file in files:
                with zipfile.ZipFile(file) as pack:
                    node_prefix = identity + "/"
                    names.update(
                        prefix + n[len(node_prefix) :]
                        for n in pack.namelist()
                        if n.startswith(node_prefix) and n != node_prefix
                        and (include_directories or not n.endswith("/"))
                    )
        return sorted(names)

    def is_file(self, member):
        location = self._locate(member)
        if location is None:
            return False
        category, path, _, _ = location
        if category in ("file", "manifest"):
            return path.is_file()
        try:
            with self._opened_pack(location) as (pack, name, _, resources):
                return not pack.getinfo(name).is_dir()
        except (FileNotFoundError, KeyError):
            return False

    def is_dir(self, member):
        if not member:
            return True
        location = self._locate(member)
        if location is not None:
            category, path, _, _ = location
            if category in ("file", "manifest"):
                return category == "file" and path.is_dir()
            try:
                with self._opened_pack(location) as (pack, name, _, resources):
                    prefix = name.rstrip("/") + "/"
                    return any(n.startswith(prefix) for n in pack.namelist())
            except FileNotFoundError:
                return False
        if member in ("provenance", "provenance/artifacts", "annotations"):
            with self.cache.lock:
                ref, artifact = self._locations()
                if member == "annotations":
                    return any((ref / "annotations").glob("*.zip"))
                if self.version == "0":
                    return False
                return any(
                    member == "provenance" or p.stem != self.uuid
                    for p in (artifact / "provenance").glob("*.zip")
                )
        return False

    def open_member(self, member):
        location = self._locate(member)
        if location is None:
            raise FileNotFoundError(member)
        category, path, algorithm, _ = location
        if category == "file":
            return path.open("rb")
        if category == "manifest":
            if not path.is_file():
                raise FileNotFoundError(member)
            # A derived manifest is a compound read of the archive's hashes
            # and masks. Capture it once, then return independent bytes.
            with _ArchiveCapture(self) as capture:
                data = capture.manifest(algorithm)
            if data is None:
                raise FileNotFoundError(member)
            return io.BytesIO(data)
        with self._opened_pack(location, masks=True, refresh=True) as (
            pack,
            name,
            hidden,
            resources,
        ):
            try:
                pack.getinfo(name)
                if hidden:
                    return io.BytesIO(b"")
                source = resources.enter_context(pack.open(name))
            except KeyError:
                raise FileNotFoundError(member) from None
            stream = RetainedStream(source, resources)
            stream.resources = resources.pop_all()
            return stream


class RetainedStream(io.BufferedReader):
    """Keep only the selected member, ZIP reader, and backing file alive."""

    def __init__(self, source, resources):
        self.resources = resources
        super().__init__(source)

    def close(self):
        try:
            super().close()
        finally:
            self.resources.close()


@contextlib.contextmanager
def materialize_tree(view, member=""):
    scope = view.cache.root_scope.child(recycle=False)
    directory = scope.acquire_directory("materialized")
    try:
        prefix = member.rstrip("/") + "/" if member else ""
        for name in view.members():
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix) :]
            if not relative:
                continue
            dest = directory.path / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            with view.open_member(name) as source, dest.open("wb") as output:
                shutil.copyfileobj(source, output)
        yield directory
    finally:
        scope.close()
        view.cache.garbage_collection()


def copy_tree(source, destination, exclude=()):
    """Copy a concrete or logical read tree using its path protocol."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name in exclude:
            continue
        target = destination / child.name
        if child.is_dir():
            copy_tree(child, target)
        else:
            with child.open("rb") as stream, target.open("wb") as output:
                shutil.copyfileobj(stream, output)


class ConcreteArchiveView:
    """V1 concrete tree retained through its existing archiver owner."""

    def __init__(self, archive):
        self.archive = archive
        self.root = archive.path
        self.cache = archive.cache
        self.uuid = str(archive.uuid)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextlib.contextmanager
    def materialize(self, member=""):
        from types import SimpleNamespace
        import tempfile

        workspace = self.cache.get_tmp_path()
        with tempfile.TemporaryDirectory(dir=workspace) as path:
            copy_tree(self.root / member, path)
            yield SimpleNamespace(path=Path(path))


def _files(path):
    return {
        p.relative_to(path).as_posix(): p
        for p in path.rglob("*")
        if p.is_file()
    }


def whiteout_bytes(records):
    return "".join(
        json.dumps({"uuid": u, "path": p}, separators=(",", ":")) + "\n"
        for u, p in sorted(records)
    ).encode()


def read_whiteouts(ref):
    path = ref / "whiteout.jsonl"
    if not path.exists():
        return set()
    records = set()
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if set(record) != {"uuid", "path"}:
            raise ValueError("Malformed published whiteout")
        node = record["uuid"]
        if str(uuid.UUID(node)) != node:
            raise ValueError("Malformed whiteout UUID")
        relative_path(record["path"])
        records.add((node, record["path"]))
    return records


class _ArchiveCapture:
    """Operation-local selections and opened packs; index only when needed.

    The caller's archive owns the RefDir. Open files retain the selected pack
    inodes while membership and whiteouts may change. No scope is allocated.
    """

    def __init__(self, archive):
        self.archive = archive
        self.cache = archive.cache
        self.ref_id = archive.ref_id
        self.closed = False
        self._resources = contextlib.ExitStack()
        self._indexes = {}
        self.packs = []
        try:
            self.uuid = str(archive.uuid)
            self.root = LogicalPath(self)
            self.cache.refresh_provenance(self.uuid)
            for category, identity, file in self._capture_state():
                pack = self._resources.enter_context(zipfile.ZipFile(file))
                pack._cache_file = file
                self.packs.append((category, identity, pack))
        except BaseException:
            self.close()
            raise

    def _capture_state(self):
        """Capture mutable metadata and open revisions in one transaction."""
        with self.cache.lock:
            ref = self.cache.object_path("archives", self.ref_id)
            _, artifact = self.cache.attachment(self.ref_id)
            self.whiteouts = read_whiteouts(ref)
            self.original = {
                algorithm: path.read_bytes()
                for algorithm in ("sha512", "md5")
                if (path := artifact / ("checksums." + algorithm)).exists()
            }
            self.metadata = {
                p.name: p.read_bytes()
                for p in artifact.iterdir()
                if p.is_file()
                and p.name != ".iref"
                and not p.name.startswith("checksums.")
            }
            version = self.metadata["VERSION"].decode().splitlines()[1]
            version = version.split(": ", 1)[1]
            # Retain opened files immediately, even if a later open fails.
            namespaces = [("annotations", ref / "annotations")]
            if version != "0":
                namespaces.append(("provenance", artifact / "provenance"))
            files = [
                (category, p.stem,
                 self._resources.enter_context(p.open("rb")))
                for category, namespace in namespaces
                for p in namespace.glob("*.zip")
            ]
            self.data_dir = artifact / "data"
        return files

    @cached_property
    def sources(self):
        sources = dict(self.metadata)
        sources.update(
            ("checksums." + algorithm, data)
            for algorithm, data in self.original.items()
        )
        sources.update(
            ("data/" + name, path)
            for name, path in _files(self.data_dir).items()
        )
        for category, node_id, pack in self.packs:
            if category == "annotations":
                prefix = "annotations/" + node_id + "/"
            elif node_id == self.uuid:
                prefix = "provenance/"
            else:
                prefix = "provenance/artifacts/" + node_id + "/"
            for member in pack.namelist():
                if (member.startswith(node_id + "/")
                        and not member.endswith("/")):
                    relative = member[len(node_id) + 1:]
                    sources[prefix + relative] = (
                        b"" if category == "provenance"
                        and (node_id, relative) in self.whiteouts
                        else (pack, member)
                    )
        return sources

    @cached_property
    def digests(self):
        result = {"sha512": {}, "md5": {}}
        for algorithm, data in self.original.items():
            result[algorithm].update(parse_manifest(data))
        return result

    def members(self):
        return sorted(self.sources)

    def is_file(self, member):
        return member in self.sources

    def is_dir(self, member):
        prefix = member.rstrip("/") + "/" if member else ""
        return any(n.startswith(prefix) for n in self.sources)

    def open_member(self, member):
        if self.closed:
            raise ValueError("Archive capture is closed")
        if member not in self.sources:
            raise FileNotFoundError(member)
        if member in ("checksums.sha512", "checksums.md5"):
            return io.BytesIO(self.manifest(member.split(".", 1)[1]))
        source = self.sources[member]
        if isinstance(source, bytes):
            return io.BytesIO(source)
        if isinstance(source, tuple):
            return source[0].open(source[1])
        return source.open("rb")

    def digest(self, member, algorithm):
        source = self.sources[member]
        if isinstance(source, bytes):
            return hashlib.new(algorithm, source).hexdigest()
        digest = self.digests[algorithm].get(member)
        if isinstance(source, tuple):
            pack, entry = source
            key = pack, algorithm
            if key not in self._indexes:
                name = "checksums." + algorithm
                self._indexes[key] = (
                    parse_manifest(pack.read(name))
                    if name in pack.NameToInfo else {}
                )
            digest = self._indexes[key].get(entry, digest)
        if digest is None:
            hasher = hashlib.new(algorithm)
            with self.open_member(member) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            self.digests[algorithm][member] = digest
        return digest

    def manifest(self, algorithm):
        original = self.original.get(algorithm)
        if original is None:
            return None
        # Archive manifest excludes annotations and itself, and retains exact
        # supplied bytes (including order/escaping) for unchanged covered data.
        covered = {
            n: self.digest(n, algorithm)
            for n in self.members()
            if not n.startswith("annotations/")
            and n not in ("checksums.sha512", "checksums.md5")
        }
        previous = parse_manifest(original)
        if covered == previous:
            return original
        if covered.keys() == previous.keys():
            # Redaction/restoration changes digests, not the manifest's layout.
            lines = []
            for line in original.splitlines(keepends=True):
                if line.strip():
                    member, digest = from_checksum_format(
                        line.decode().rstrip("\r\n")
                    )
                    line = line.replace(
                        digest.encode(), covered[member].encode(), 1
                    )
                lines.append(line)
            return b"".join(lines)
        return manifest_bytes(covered)

    def materialize(self, member=""):
        return materialize_tree(self, member)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._resources.close()
