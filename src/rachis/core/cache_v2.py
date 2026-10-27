# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Hardlink-owned cache storage with short metadata transactions.

The marker inode is the identity of an object; its contents are never replaced.
The catalogue of packs is allowed to replace revisions, but consumers retain
opened files or hardlinks, never a promise that a pathname will stay unchanged.
"""

import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import threading
import time
import uuid
import warnings
from datetime import timedelta

import psutil

import rachis
from .cache import (
    Cache,
    CacheV1,
    MEGALock,
    USED_CACHES,
    _CACHE,
    _check_pool_selection,
    _cache_version,
)
from .cache_stat import ownership_stat

from .reference import (
    ALPHABET as _ALPHABET,
    Reference,
    category_name,
    directory_references,
    durable_write,
    fsync_dir,
    new_id,
    relative_path,
    shared_mkdir,
    target_name,
)
from .refdir import RefDir

_ACTIVE_POOL = object()


def flush_tree(path):
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            with open(Path(root) / name, "rb") as stream:
                os.fsync(stream.fileno())
        fsync_dir(root)


class MetadataLock:
    """Owner-aware reentrancy, lease refresh, and thread exclusion."""

    def __init__(self, path):
        self.path = str(path)
        self._threads = threading.RLock()
        self._local = threading.local()
        self._lock = MEGALock(self.path, timedelta(minutes=3))

    def __enter__(self):
        self._threads.acquire()
        depth = getattr(self._local, "depth", 0)
        try:
            if depth == 0:
                self._lock.__enter__()
            self._local.depth = depth + 1
        except BaseException:
            self._threads.release()
            raise
        return self

    def __exit__(self, *args):
        self._local.depth -= 1
        try:
            if self._local.depth == 0:
                self._lock.__exit__(*args)
        finally:
            self._threads.release()


class Scope(RefDir):
    """One lifetime boundary, retained directly by process/recycle roots."""

    def __init__(self, cache, scope_id, owner=None):
        super().__init__(cache, scope_id, category="scopes")
        self.scope_id = scope_id
        self.closed = False
        self.owners = set()
        if owner is not None:
            self.owners.add(Path(owner))

    def acquire_directory(
        self, name="directory_format", *, category="tempdirs"
    ):
        if self.closed:
            raise ValueError("Scope is closed")
        return self.cache.create_refdir(category, name=name, scope=self)

    def child(self, *, recycle=True):
        # Context nesting belongs to the call stack, not the ownership graph.
        return self.cache.create_scope(recycle=recycle)

    def adopt(self, ref_id):
        if self.closed:
            raise ValueError("Scope is closed")
        target = (
            ref_id if isinstance(ref_id, RefDir) else self.cache.refdir(ref_id)
        )
        return self.reference(target)

    def reference(self, target, *, acquisition=True):
        return super().reference(target, acquisition=acquisition)

    def close(self, success=True):
        if self.closed:
            return
        self.closed = True
        for owner in self.owners:
            if success or owner.parent == self.cache.processes:
                self.cache.release(owner, "scope", self.scope_id)

    def __enter__(self):
        self._previous = getattr(self.cache._scope_local, "scope", None)
        self.cache._scope_local.scope = self
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.cache._scope_local.scope = self._previous
        self.close(success=exc_type is None)


class CacheV2(Cache):
    CURRENT_FORMAT_VERSION = "v2"
    base_cache_contents = {"VERSION", "immutable", "mutable", "keys"}

    def __init__(self, path=None, process_pool_lifespan=45):
        if getattr(self, "_pid", None) == os.getpid() and not self._closed:
            return
        self.path = self._selected_path
        created = False
        if not self.path.exists():
            shared_mkdir(self.path, parents=True)
            created = True
        elif not any(self.path.iterdir()):
            created = True
        if created:
            # VERSION is the last publication. An incomplete root is an error
            # for another opener, never an invitation to repair it.
            for name in (
                "immutable/archives",
                "immutable/provenance",
                "immutable/annotations",
                "mutable/archives",
                "mutable/collections",
                "mutable/views",
                "mutable/tempdirs",
                "mutable/scopes",
                "mutable/.garbage",
                "keys/recycle",
                "keys/proc",
            ):
                shared_mkdir(self.path / name, parents=True)
            durable_write(
                self.path / "VERSION",
                (
                    "QIIME 2\ncache: v2\nframework: "
                    + rachis.__version__
                    + "\n"
                ).encode(),
            )
            fsync_dir(self.path)
        if not self.is_cache(self.path):
            raise ValueError(f"Invalid V2 cache at {self.path}")
        self._pid = os.getpid()
        self._closed = False
        self.lock = MetadataLock(self.lockfile)
        self._scope_local = threading.local()
        self._candidates = set()
        self._candidate_lock = threading.Lock()
        self._named_pool_ = None
        self.process_pool_lifespan = process_pool_lifespan * 86400
        host = socket.gethostname().replace("/", "_")
        start = psutil.Process().create_time()
        self.process_root = (
            self.processes / f"{host}.{self._pid}.{start}.{new_id()}"
        )
        with self.lock:
            shared_mkdir(self.process_root)
            durable_write(
                self.process_root / "process.json",
                json.dumps(
                    {
                        "host": host,
                        "pid": self._pid,
                        "start": start,
                        "created": time.time(),
                    }
                ).encode(),
            )
            fsync_dir(self.processes)
        sid, _, _ = self._publish_owned("scope", self.process_root)
        self.root_scope = Scope(self, sid, self.process_root)
        self.process_pool = ScopePool(self, self.process_root)
        USED_CACHES.add(self)

    @classmethod
    def is_cache(cls, path):
        try:
            return _cache_version(path) == "v2" and all(
                (Path(path) / x).is_dir()
                for x in (
                    "immutable/archives",
                    "immutable/provenance",
                    "immutable/annotations",
                    "mutable/archives",
                    "mutable/collections",
                    "mutable/views",
                    "mutable/tempdirs",
                    "mutable/scopes",
                    "mutable/.garbage",
                    "keys/proc",
                    "keys/recycle",
                )
            )
        except (ValueError, OSError):
            return False

    @property
    def scope(self):
        return getattr(self._scope_local, "scope", None) or self.root_scope

    @property
    def keys(self):
        return self.path / "keys"

    @property
    def processes(self):
        return self.keys / "proc"

    @property
    def pools(self):
        return self.keys / "recycle"

    @property
    def version(self):
        return self.path / "VERSION"

    @property
    def lockfile(self):
        return self.path / "mutable/.LOCK"

    @property
    def named_pool(self):
        return getattr(self._scope_local, "pool", self._named_pool_)

    __enter__ = CacheV1.__enter__
    __exit__ = CacheV1.__exit__

    def __reduce__(self):
        return (
            _restore_cache,
            (
                str(self.path),
                self.process_pool_lifespan / 86400,
                getattr(self, "_handoff_pid", os.getpid()),
                getattr(self, "_handoff_scope", self.scope).scope_id,
                str(self.named_pool.path)
                if self.named_pool is not None
                else None,
                str(getattr(self, "_handoff_process_root", self.process_root)),
            ),
        )

    def refdir_path(self, category, identity):
        category_name(category)
        if len(identity) != 20 or any(c not in _ALPHABET for c in identity):
            raise ValueError(f"Invalid RefDir ID: {identity}")
        return self.path / "mutable" / category / identity[:2] / identity

    def object_path(self, kind, identity):
        if kind == "artifact":
            if str(uuid.UUID(identity)) != identity:
                raise ValueError("Noncanonical artifact UUID")
            return self.path / "immutable/archives" / identity[:3] / identity
        if kind == "ref":
            if len(identity) != 20 or any(
                c not in _ALPHABET for c in identity
            ):
                raise ValueError(f"Invalid RefDir ID: {identity}")
            candidates = list(
                (self.path / "mutable").glob(f"*/{identity[:2]}/{identity}")
            )
            if len(candidates) != 1:
                raise KeyError(f"Unknown or ambiguous RefDir: {identity}")
            return candidates[0]
        return self.refdir_path(
            "scopes" if kind == "scope" else kind, identity
        )

    def refdir(self, identity, category=None):
        return RefDir(self, identity, category=category)

    def marker_target(self, marker):
        marker = Path(marker)
        reference = Reference.read(
            self.path,
            marker,
            named=marker.parent == self.keys and marker.name != ".iref",
        )
        target = reference.target.parent
        relative = target.relative_to(self.path).parts
        if len(relative) != 4:
            raise ValueError(f"Invalid ownership target: {marker}")
        if relative[:2] == ("immutable", "archives"):
            kind = "artifact"
        elif relative[0] == "mutable":
            kind = "scope" if relative[1] == "scopes" else relative[1]
        else:
            raise ValueError(f"Invalid ownership target: {marker}")
        expected = (
            self.object_path("artifact", relative[-1])
            if relative[0] == "immutable"
            else self.refdir_path(relative[1], relative[-1])
        )
        if target != expected:
            raise ValueError(f"Invalid ownership target: {marker}")
        return kind, relative[-1], target

    def _publish_owned(self, kind, owner, *, acquisition=False, category=None):
        with self.lock:
            while True:
                identity = new_id()
                dest = (
                    self.refdir_path(category, identity)
                    if category
                    else self.object_path(kind, identity)
                )
                staging = Path(owner) / ".staging" / identity
                if dest.exists():
                    continue
                shared_mkdir(staging.parent, exist_ok=True)
                try:
                    shared_mkdir(staging)
                except FileExistsError:
                    continue
                break
            durable_write(
                staging / ".iref",
                (dest.relative_to(self.path).as_posix() + "/\n").encode(),
            )
            fsync_dir(staging)
            name = target_name(
                self.path, dest / ".iref", new_id() if acquisition else None
            )
            pending = Path(owner) / (".pending." + name)
            os.link(staging / ".iref", pending)
            fsync_dir(owner)
            shared_mkdir(dest.parent, parents=True, exist_ok=True)
            staging.rename(dest)
            fsync_dir(dest.parent)
            if not any(staging.parent.iterdir()):
                staging.parent.rmdir()
            entry = Path(owner) / name
            pending.rename(entry)
            fsync_dir(owner)
        return identity, dest, Reference(entry, dest / ".iref")

    def create_refdir(
        self, category="tempdirs", *, name=None, scope=None, cls=RefDir
    ):
        category_name(category)
        if category == "scopes":
            raise ValueError("Use create_scope for scopes")
        if name is not None:
            relative_path(name)
        scope = scope or self.scope
        if scope.closed:
            raise ValueError("Scope is closed")
        identity, root, acquisition = self._publish_owned(
            category, scope.root, acquisition=True, category=category
        )
        path = root if name is None else root / name
        if path != root:
            shared_mkdir(path, parents=True)
        return cls(
            self,
            identity,
            scope,
            path,
            category=category,
            acquisition=acquisition,
        )

    def create_scope(self, *, recycle=True, pool=_ACTIVE_POOL):
        identity, _, _ = self._publish_owned("scope", self.process_root)
        scope = Scope(self, identity, self.process_root)
        handoff = getattr(self, "_handoff_process_root", None)
        if handoff is not None and handoff != self.process_root:
            self.retain(handoff, "scope", identity)
            scope.owners.add(handoff)
        pool = self.named_pool if pool is _ACTIVE_POOL else pool
        if recycle and pool is not None:
            pool.retain_scope(scope)
        return scope

    def _graph_snapshot(self, target, wanted):
        """Discover dependencies and directory identities outside LOCK."""
        states, seen = {}, set()
        pending = [target]
        while pending:
            path = pending.pop()
            if path == wanted:
                return True, states
            if (
                path in seen
                or path.relative_to(self.path).parts[0] != "mutable"
            ):
                continue
            seen.add(path)
            try:
                before = path.stat()
                entries = list(directory_references(self.path, path))
                after = path.stat()
            except FileNotFoundError:
                raise BlockingIOError("Ownership changed during discovery")
            signature = (after.st_dev, after.st_ino, after.st_mtime_ns)
            if (before.st_dev, before.st_ino, before.st_mtime_ns) != signature:
                raise BlockingIOError("Ownership changed during discovery")
            states[path] = signature
            pending.extend(reference.target.parent for reference in entries)
        return False, states

    def reference(self, owner, target, *, acquisition=False):
        owner = Path(owner)
        marker = (
            target.root / ".iref"
            if isinstance(target, RefDir)
            else Path(target)
        )
        is_directory = marker.name == ".iref"
        graph_owner = owner / ".iref"
        if (
            is_directory
            and marker.parent.parent.parent.name == "scopes"
            and graph_owner.exists()
        ):
            raise ValueError("Scopes are retained by pools, not other RefDirs")
        while True:
            states, cyclic = {}, False
            if (
                is_directory
                and graph_owner.exists()
                and owner.parent.parent.name != "scopes"
            ):
                try:
                    cyclic, states = self._graph_snapshot(marker.parent, owner)
                except BlockingIOError:
                    continue
            with self.lock:
                if not marker.is_file():
                    raise KeyError(f"Ownership target was reclaimed: {marker}")
                if any(
                    not path.exists()
                    or (
                        path.stat().st_dev,
                        path.stat().st_ino,
                        path.stat().st_mtime_ns,
                    )
                    != signature
                    for path, signature in states.items()
                ):
                    continue
                if cyclic:
                    raise ValueError("Ownership graph must be acyclic")
                return Reference.link(
                    self.path, owner, marker, acquisition=acquisition
                )

    def unreference(self, reference):
        with self.lock:
            reference.release()
        self._schedule_target(reference.target)

    def _schedule_target(self, target):
        with self._candidate_lock:
            self._candidates.add(Path(target))

    def retain(self, owner, kind, identity):
        return self.reference(
            owner, self.object_path(kind, identity) / ".iref"
        )

    def release(self, owner, kind, identity):
        target = self.object_path(kind, identity) / ".iref"
        self.unreference(
            Reference(Path(owner) / target_name(self.path, target), target)
        )

    def schedule(self, kind, identity):
        if kind in ("provenance_pack", "annotation_pack"):
            from .archive.pack import pack_path

            category = (
                "provenance" if kind == "provenance_pack" else "annotations"
            )
            target = pack_path(self, category, identity)
        else:
            target = self.object_path(kind, identity) / ".iref"
        self._schedule_target(target)

    def attachment(self, ref_id):
        path = self.object_path("archives", ref_id)
        entries = list(path.glob("immutable.archives.*.iref"))
        if len(entries) != 1:
            raise ValueError(
                "Result ref is incomplete; resume or finalize it first"
            )
        kind, identity, target = self.marker_target(entries[0])
        if kind != "artifact" or not target.exists():
            raise ValueError("Dangling artifact attachment")
        return identity, target

    def acquire_directory(
        self, name="directory_format", *, category="tempdirs"
    ):
        return self.scope.acquire_directory(name, category=category)

    def get_tmp_path(self):
        return self.acquire_directory("temporary").path

    def _bind_key(self, key, kind, identity):
        self.validate_key(key)
        temporary = self.keys / f".pending.{new_id()}"
        target = self.object_path(kind, identity)
        with self.lock:
            os.link(target / ".iref", temporary)
            old = self.keys / f"{key}.iref"
            previous = self.marker_target(old)[:2] if old.exists() else None
            os.replace(temporary, old)
            fsync_dir(self.keys)
        if previous:
            self.schedule(*previous)

    def retain_result(self, ref):
        if ref._archiver.cache is self:
            self.scope.adopt(ref._archiver.ref_id)
            return ref
        # Import the backing adapter directly: V1 supplies its extracted tree,
        # while another V2 cache supplies existing packs and separate masks.
        return rachis.sdk.Result._from_archiver(
            self.import_archive(ref._archiver._archive())
        )

    def save(self, ref, key):
        local = self.retain_result(ref)
        target = local._archiver.ref_id
        self._bind_key(key, "archives", target)
        return self.load(key)

    def load(self, key):
        from .archive.archiver import Archiver
        from .archive.archive import _CacheV2Archive

        self.validate_key(key)
        with self.lock:
            marker = self.keys / f"{key}.iref"
            if not marker.exists():
                raise KeyError(key)
            kind, identity, _ = self.marker_target(marker)
            if kind != "archives":
                raise ValueError("Key does not refer to an archive")
            self.scope.adopt(identity)
        return rachis.sdk.Result._from_archiver(
            Archiver.from_archive(
                _CacheV2Archive(self, identity, scope_owned=True)
            )
        )

    def get_keys(self):
        return sorted(
            p.name[:-5]
            for p in self.keys.glob("*.iref")
            if not p.name.startswith(".")
        )

    def read_key(self, key):
        self.validate_key(key)
        with self.lock:
            p = self.keys / f"{key}.iref"
            if not p.exists():
                raise KeyError(key)
            kind, identity, _ = self.marker_target(p)
            if kind == "archives":
                result = {"origin": key, "data": self.attachment(identity)[0]}
            else:
                result = {"origin": key, "pool": identity}
                order = self.object_path(kind, identity) / "collection.json"
                if order.exists():
                    result["order"] = json.loads(order.read_text())
            return result

    def remove(self, key):
        self.validate_key(key)
        with self.lock:
            marker = self.keys / f"{key}.iref"
            recycle = self.pools / key
            if not marker.exists() and not recycle.exists():
                raise KeyError(key)
            target = (
                self.marker_target(marker)[:2] if marker.exists() else None
            )
            marker.unlink(missing_ok=True)
            fsync_dir(self.keys)
            retired = self._retire_root(recycle) if recycle.exists() else None
        if target:
            self.schedule(*target)
        if retired:
            self._delete_garbage(retired)
        self.garbage_collection()

    def save_collection(self, refs, key):
        if isinstance(refs, rachis.sdk.Results):
            refs = refs.output
        collection = self.create_refdir("collections")
        try:
            order = []
            for name, ref in refs.items():
                local = self.retain_result(ref)
                collection.reference(self.refdir(local._archiver.ref_id))
                order.append([name, local._archiver.ref_id])
            durable_write(
                collection.root / "collection.json", json.dumps(order).encode()
            )
            fsync_dir(collection.root)
            self._bind_key(key, "collections", collection.ref_id)
            return self.load_collection(key)
        finally:
            collection.release()

    def load_collection(self, key):
        from .archive.archiver import Archiver
        from .archive.archive import _CacheV2Archive

        self.validate_key(key)
        with self.lock:
            kind, identity, target = self.marker_target(
                self.keys / f"{key}.iref"
            )
            if kind != "collections":
                raise ValueError("Key does not refer to a collection")
            self.scope.adopt(identity)
            order = json.loads((target / "collection.json").read_text())
            for _, ref_id in order:
                self.scope.adopt(ref_id)
        return rachis.sdk.ResultCollection(
            {
                name: rachis.sdk.Result._from_archiver(
                    Archiver.from_archive(
                        _CacheV2Archive(self, ref_id, scope_owned=True)
                    )
                )
                for name, ref_id in order
            }
        )

    def create_pool(self, key, reuse=False):
        self.validate_key(key)
        with self.lock:
            container = self.pools / key
            if container.exists():
                if not reuse:
                    raise ValueError("Pool already exists; use reuse=True")
            else:
                shared_mkdir(container)
        return ScopePool(self, container, key)

    def get_pools(self):
        return sorted(p.name for p in self.pools.iterdir() if p.is_dir())

    def get_processes(self):
        return sorted(p.name for p in self.processes.iterdir() if p.is_dir())

    def get_data(self):
        return sorted(
            p.name
            for p in (self.path / "immutable/archives").glob("*/*")
            if p.is_dir()
        )

    def _load_uuid(self, identity):
        # A UUID hit never substitutes for interpretation of an archive view.
        return None

    def import_archive(self, filepath, replay=False):
        from .archive.archiver import Archiver
        return Archiver.load(filepath, cache=self, replay=replay)

    def broadcast_annotation(self, results, annotation, reference_uuid=None):
        from .archive.archive import broadcast_annotation

        archives = [r._archiver._storage for r in results]
        if any(a.cache is not self for a in archives):
            raise ValueError("Broadcast targets must belong to this cache")
        broadcast_annotation(archives, annotation, reference_uuid)

    def broadcast_whiteouts(self, results, records):
        from .archive.archive import commit_whiteouts

        archives = [r._archiver._storage for r in results]
        if any(a.cache is not self for a in archives):
            raise ValueError("Broadcast targets must belong to this cache")
        commit_whiteouts(archives, records)

    def _deallocate(self, ref_id):
        # Scopes own RefDirs independently of Python objects. A finalizer only
        # schedules discovery; it cannot invalidate retained pipeline outputs.
        try:
            target = self.object_path("ref", ref_id) / ".iref"
        except KeyError:
            return
        self._schedule_target(target)

    def reserve_result(self, reserved_uuid=None, **execution):
        directory = self.acquire_directory("cells", category="archives")
        record = dict(execution, uuid=str(reserved_uuid or uuid.uuid4()))
        durable_write(
            self.object_path("ref", directory.ref_id) / "resumption.json",
            json.dumps(record).encode(),
        )
        fsync_dir(self.object_path("ref", directory.ref_id))
        return directory

    def retain_incomplete(self, directory, key):
        self._bind_key(key, "archives", directory.ref_id)

    def resume(self, key):
        """Acquire exclusive producer authority for an incomplete result."""
        self.validate_key(key)
        with self.lock:
            kind, identity, path = self.marker_target(
                self.keys / f"{key}.iref"
            )
            if kind != "archives" or list(
                path.glob("immutable.archives.*.iref")
            ):
                raise ValueError("Key does not identify an incomplete result")
            self.scope.adopt(identity)
        return ProducerLease(self, identity)

    def _retire_root(self, path):
        if not path.exists():
            return None
        dest = self.path / "mutable/.garbage" / f"root.{new_id()}"
        path.rename(dest)
        fsync_dir(path.parent)
        fsync_dir(dest.parent)
        return dest

    def _delete_garbage(self, path):
        from .archive.pack import pack_path

        candidates = set()
        try:
            for root, dirs, files in os.walk(path):
                for name in files:
                    entry = Path(root) / name
                    if name.endswith(".iref") and name != ".iref":
                        candidates.add(Reference.read(self.path, entry).target)
                    elif name.endswith(".zip") and (
                        name.startswith("immutable.") or name.startswith("_")
                    ):
                        candidates.add(Reference.read(self.path, entry).target)
                    elif name.endswith(".zip") and Path(root).name in (
                        "annotations",
                        "provenance",
                    ):
                        candidates.add(
                            pack_path(self, Path(root).name, Path(name).stem)
                        )
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        for target in candidates:
            self._schedule_target(target)

    def _expired_roots(self):
        for root in self.processes.iterdir():
            if root == self.process_root or not root.is_dir():
                continue
            try:
                record = json.loads((root / "process.json").read_text())
            except FileNotFoundError:
                continue
            dead = False
            if record["host"] == socket.gethostname().replace("/", "_"):
                try:
                    dead = (
                        psutil.Process(record["pid"]).create_time()
                        != record["start"]
                    )
                except psutil.NoSuchProcess:
                    dead = True
                except psutil.AccessDenied:
                    pass
            # Remote roots use an explicit age expiry policy. Local live
            # producers are never expired merely because they are old.
            elif time.time() - record["created"] > self.process_pool_lifespan:
                dead = True
            if dead:
                with self.lock:
                    retired = self._retire_root(root)
                if retired:
                    self._delete_garbage(retired)

    def garbage_collection(self, deep=False):
        """Discover candidates, retire under LOCK, and delete outside it."""
        if not self.path.exists() or getattr(self.lock._local, "depth", 0):
            return
        self._expired_roots()
        self._drain_garbage()
        if deep:
            self._discover_gc_candidates()
        while True:
            with self._candidate_lock:
                if not self._candidates:
                    break
                candidates, self._candidates = self._candidates, set()
            for target in candidates:
                self._collect_target(target)
        if deep:
            self._collect_orphan_packs()

    def _drain_garbage(self):
        garbage = self.path / "mutable/.garbage"
        for tree in list(garbage.iterdir()):
            if tree.is_dir():
                self._delete_garbage(tree)
            else:
                tree.unlink(missing_ok=True)

    def _refdir_paths(self):
        # Category names are open-ended; payload contents are never traversed.
        for category in (self.path / "mutable").iterdir():
            if category.name.startswith(".") or not category.is_dir():
                continue
            for path in category.glob("*/*"):
                if (path / ".iref").is_file():
                    yield category.name, path

    def _discover_gc_candidates(self):
        self._recover_pending()
        candidates = list(self._refdir_paths())
        candidates.sort(
            key=lambda item: (
                0
                if item[0] == "scopes"
                else 1
                if any(
                    reference.target.relative_to(self.path).parts[0]
                    == "mutable"
                    for reference in directory_references(self.path, item[1])
                )
                else 2
            )
        )
        for category, path in candidates:
            self._collect_target(path / ".iref")
        for path in (self.path / "immutable/archives").glob("*/*"):
            if path.is_dir():
                self.schedule("artifact", path.name)
        for owner in self.keys.glob("*.iref"):
            _, _, target = self.marker_target(owner)
            if not (target / ".iref").exists():
                raise ValueError(f"Dangling committed owner: {owner}")

    def _retire_unowned(self, path, marker, label):
        """Revalidate the same inode's fresh link count before retirement."""
        try:
            discovered = ownership_stat(marker)
        except FileNotFoundError:
            return None
        with self.lock:
            try:
                current = ownership_stat(marker)
            except FileNotFoundError:
                return None
            if (current.st_dev, current.st_ino) != (
                discovered.st_dev,
                discovered.st_ino,
            ) or current.st_nlink != 1:
                return None
            garbage = self.path / "mutable/.garbage"
            retired = garbage / f"{label}.{new_id()}"
            path.rename(retired)
            fsync_dir(path.parent)
            fsync_dir(garbage)
        return retired

    def _collect_target(self, target):
        is_pack = target.name != ".iref"
        path = target if is_pack else target.parent
        label = path.name
        retired = self._retire_unowned(path, target, label)
        if retired is not None:
            if is_pack:
                retired.unlink(missing_ok=True)
            else:
                self._delete_garbage(retired)

    def _collect_orphan_packs(self):
        for category in ("provenance", "annotations"):
            for pack in (self.path / "immutable" / category).glob("*/*.zip"):
                retired = self._retire_unowned(pack, pack, "pack")
                if retired is not None:
                    retired.unlink(missing_ok=True)

    def refresh_provenance(self, artifact_id, node_id=None):
        """Discover enriched bindings outside LOCK and revalidate on commit."""
        from .archive.pack import pack_path

        artifact = self.object_path("artifact", artifact_id)
        if node_id is None:
            bindings = (artifact / "provenance").glob("*.zip")
        else:
            pack_path(self, "provenance", node_id)  # Validate the node UUID.
            bindings = [artifact / "provenance" / (node_id + ".zip")]
        for binding in bindings:
            self._refresh_provenance_binding(artifact, binding)

    def _refresh_provenance_binding(self, artifact, binding):
        from .archive.pack import pack_path, pack_revision

        catalogue = pack_path(self, "provenance", binding.stem)
        with contextlib.ExitStack() as resources:
            try:
                old_file = resources.enter_context(binding.open("rb"))
                new_file = resources.enter_context(catalogue.open("rb"))
            except FileNotFoundError:
                return
            old_stat = os.fstat(old_file.fileno())
            new_stat = os.fstat(new_file.fileno())
            if old_stat.st_ino == new_stat.st_ino:
                return
            old_hashes, sizes, eligible = pack_revision(old_file, binding.stem)
            new_hashes, new_sizes, new_eligible = pack_revision(
                new_file, binding.stem
            )
            if (
                old_hashes.keys() != new_hashes.keys()
                or eligible != new_eligible
            ):
                raise ValueError("Conflicting catalogue provenance revision")
            for name in old_hashes:
                if old_hashes[name] != new_hashes[name] and (
                    name not in eligible or sizes[name] and new_sizes[name]
                ):
                    raise ValueError(
                        "Conflicting catalogue provenance originals"
                    )
            if any(sizes[n] and not new_sizes[n] for n in eligible):
                return
            with self.lock:
                if (
                    not artifact.exists()
                    or not binding.exists()
                    or not catalogue.exists()
                ):
                    return
                if (
                    binding.stat().st_ino != old_stat.st_ino
                    or catalogue.stat().st_ino != new_stat.st_ino
                ):
                    return
                temporary = binding.parent / ("." + new_id())
                os.link(catalogue, temporary)
                os.replace(temporary, binding)
                fsync_dir(binding.parent)

    def snapshot_ref(self, ref_id):
        """Clone mutable membership/masks, sharing the immutable archive."""
        source = self.object_path("archives", ref_id)
        directory = self.create_refdir("archives")
        with self.lock:
            artifact_id, _ = self.attachment(ref_id)
            directory.reference(
                self.object_path("artifact", artifact_id) / ".iref"
            )
            from .archive.view import read_whiteouts, whiteout_bytes

            whiteouts = read_whiteouts(source)
            if whiteouts:
                durable_write(
                    directory.root / "whiteout.jsonl",
                    whiteout_bytes(whiteouts),
                )
            if (source / "annotations").exists():
                shared_mkdir(directory.root / "annotations")
                for pack in (source / "annotations").glob("*.zip"):
                    Reference.link(
                        self.path,
                        directory.root / "annotations",
                        pack,
                        name=pack.name,
                    )
            fsync_dir(directory.root)
        return directory.ref_id

    def _recover_pending(self):
        owners = [path for _, path in self._refdir_paths()]
        owners.extend(self.processes.iterdir())
        owners.extend(self.pools.iterdir())
        for owner in owners:
            for marker in owner.glob(".pending.*.iref"):
                _, _, target = self.marker_target(marker)
                if target.relative_to(self.path).parts[0] == "immutable":
                    continue
                with self.lock:
                    if target.exists() and marker.exists():
                        marker.rename(owner / marker.name[len(".pending.") :])
                        fsync_dir(owner)
            if list(owner.glob("immutable.archives.*.iref")):
                for name in ("cells", "staging"):
                    with self.lock:
                        retired = self._retire_root(owner / name)
                    if retired:
                        self._delete_garbage(retired)

    def close(self):
        if self._closed:
            return
        with self.lock:
            retired = self._retire_root(self.process_root)
        if retired:
            self._delete_garbage(retired)
        self._closed = True
        self.garbage_collection()

    def clear_lock(self):
        warnings.warn(
            "Breaking the metadata lock can invalidate active writers"
        )
        self.lockfile.unlink(missing_ok=True)

    def create_archive(self, identity=None):
        from .archive.archive import _CacheV2Archive
        return _CacheV2Archive.create(self, identity)


class ProducerLease:
    """Single producer authority, separate from lifetime ownership."""

    def __init__(self, cache, ref_id):
        from .cache import _FluflLock

        self.cache = cache
        self.ref_id = ref_id
        self.path = cache.object_path("ref", ref_id)
        self.lock = _FluflLock(
            str(self.path / "PRODUCER"), lifetime=timedelta(minutes=3)
        )

    def __enter__(self):
        from .cache import lock_thread

        self.lock.lock(timeout=timedelta(seconds=0))
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=lock_thread,
            args=(self.lock, timedelta(minutes=3), self._done),
            daemon=True,
        )
        self._thread.start()
        return self

    def commit_cell(self, coordinate, attempt, data):
        relative_path(coordinate)
        relative_path(attempt)
        dest = self.path / "cells" / coordinate / attempt
        if dest.exists():
            raise ValueError("Attempt is already committed")
        staged = self.path / "staging" / new_id()
        durable_write(staged / "payload.chunk", data)
        durable_write(staged / "outcome.json", b'{"status":"success"}\n')
        fsync_dir(staged)
        shared_mkdir(dest.parent, parents=True, exist_ok=True)
        staged.rename(dest)
        fsync_dir(dest.parent)

    def finalize(self, archive_path):
        from .archive.archive import _CacheV2Archive, _NoOpArchive
        from .archive.archiver import Archiver
        archive = _CacheV2Archive.create(
            self.cache, ref_id=self.ref_id
        )
        source = _NoOpArchive(Path(archive_path))
        archive.import_from(source)
        archive.finalize()
        return Archiver.from_archive(archive)

    def __exit__(self, *args):
        self._done.set()
        self.lock.unlock()


class ScopePool:
    """A process/recycle root retaining a flat set of scopes."""

    def __init__(self, cache, path, name=None):
        self.cache = cache
        self.path = Path(path)
        self.name = name or self.path.name
        self.index = {}
        self._entries = []

    def retain_scope(self, scope):
        self.cache.retain(self.path, "scope", scope.scope_id)
        scope.owners.add(self.path)

    def _result_refs(self):
        seen = set()
        with self.cache.lock:
            for reference in directory_references(self.cache.path, self.path):
                scope = reference.target.parent
                for member in directory_references(self.cache.path, scope):
                    path = member.target.parent
                    if (
                        path.parent.parent.name == "archives"
                        and path.name not in seen
                    ):
                        if list(path.glob("immutable.archives.*.iref")):
                            seen.add(path.name)
        return sorted(seen)

    def __enter__(self):
        _check_pool_selection()
        previous = getattr(_CACHE, "cache", None)
        if previous is not None and previous.path != self.cache.path:
            raise ValueError("Pool belongs to another cache")
        self._entries.append(
            (previous, self.cache.named_pool, self.cache.scope)
        )
        _CACHE.cache = self.cache
        self.cache._scope_local.pool = self
        self.cache._scope_local.scope = self.cache.create_scope()
        return self

    def __exit__(self, *args):
        _check_pool_selection()
        previous, pool, scope = self._entries.pop()
        _CACHE.cache = previous
        self.cache._scope_local.pool = pool
        self.cache._scope_local.scope = scope

    def save(self, ref):
        local = self.cache.retain_result(ref)
        scope = self.cache.scope
        if (
            self.path.parent == self.cache.pools
            and self.path not in scope.owners
        ):
            scope = self.cache.create_scope(recycle=False)
            scope.adopt(local._archiver.ref_id)
        self.retain_scope(scope)
        return local

    def load(self, identity):
        from .archive.archiver import Archiver
        from .archive.archive import _CacheV2Archive

        for ref_id in self._result_refs():
            artifact_id, _ = self.cache.attachment(ref_id)
            if identity in (ref_id, artifact_id):
                self.cache.scope.adopt(ref_id)
                return rachis.sdk.Result._from_archiver(
                    Archiver.from_archive(
                        _CacheV2Archive(self.cache, ref_id, scope_owned=True)
                    )
                )
        raise KeyError(identity)

    def remove(self, ref):
        identity = (
            ref._archiver.ref_id if hasattr(ref, "_archiver") else str(ref)
        )
        with self.cache.lock:
            for reference in directory_references(self.cache.path, self.path):
                scope = reference.target.parent
                for member in list(
                    directory_references(self.cache.path, scope)
                ):
                    path = member.target.parent
                    if path.parent.parent.name != "archives":
                        continue
                    if identity in (
                        path.name,
                        self.cache.attachment(path.name)[0],
                    ):
                        self.cache.unreference(member)
        self.cache.garbage_collection()

    def get_data(self):
        return {
            self.cache.attachment(ref_id)[0] for ref_id in self._result_refs()
        }

    def create_index(self):
        from .archive.archiver import Archiver
        from .archive.archive import _CacheV2Archive
        from .util import load_action_yaml
        from .type import HashableInvocation
        from .cache import Pool

        self.index = {}
        for ref_id in self._result_refs():
            handle = Archiver.from_archive(
                _CacheV2Archive(self.cache, ref_id, scope_owned=True)
            )
            if handle.provenance_dir is None:
                continue
            action = load_action_yaml(handle.root_dir)["action"]
            if action.get("type") == "import":
                continue
            invocation = HashableInvocation(
                action["plugin"] + ":" + action["action"],
                action["inputs"] + action["parameters"],
            )
            Pool._add_index_output(
                self,
                self.index.setdefault(invocation, {}),
                action["output-name"],
                ref_id,
            )

    _add_collection_index_output = __import__(
        "rachis.core.cache", fromlist=["Pool"]
    ).Pool._add_collection_index_output


def _restore_cache(
    path, lifespan, source_pid, source_scope, named_pool, source_process_root
):
    cache = Cache(path, lifespan)
    if source_pid != os.getpid():
        cache._handoff_pid = source_pid
        cache._handoff_scope = Scope(cache, source_scope)
        cache._handoff_process_root = Path(source_process_root)
        if named_pool is not None:
            cache._named_pool_ = ScopePool(cache, named_pool)
    return cache
