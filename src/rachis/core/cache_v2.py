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
from pathlib import Path, PurePosixPath
import secrets
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
    _cache_version,
)
from .cache_stat import ownership_stat

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def new_id():
    return "".join(secrets.choice(_ALPHABET) for _ in range(20))


def relative_path(value):
    """Validate lexical member/marker names without filesystem coercion."""
    p = PurePosixPath(value)
    if (
        not value
        or p.is_absolute()
        or "\\" in value
        or any(x in ("", ".", "..") for x in value.rstrip("/").split("/"))
    ):
        raise ValueError(f"Invalid cache-relative path: {value!r}")
    return p


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o660)


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


class ScopedDirectory:
    """A concrete workspace and its stable lifetime ref."""

    def __init__(self, cache, ref_id, scope, path):
        self.cache = cache
        self.ref_id = ref_id
        self.scope = scope
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def release(self):
        self.scope.release(self.ref_id)


class Scope:
    def __init__(self, cache, scope_id, owner=None):
        self.cache = cache
        self.scope_id = scope_id
        self.path = cache.object_path("scope", scope_id)
        self.owner = owner
        self.closed = False

    def acquire_directory(self, name="directory_format"):
        if self.closed:
            raise ValueError("Scope is closed")
        relative_path(name)
        ref_id, path = self.cache._publish_owned("ref", self.path)
        directory = path / name
        shared_mkdir(directory)
        fsync_dir(path)
        return ScopedDirectory(self.cache, ref_id, self, directory)

    def child(self):
        if self.closed:
            raise ValueError("Scope is closed")
        sid, _ = self.cache._publish_owned("scope", self.path)
        return Scope(self.cache, sid, self.path)

    def adopt(self, ref_id):
        self.cache.retain(self.path, "ref", ref_id)

    def retain_scope(self, scope):
        self.cache.retain(self.path, "scope", scope.scope_id)

    def release(self, ref_id):
        self.cache.release(self.path, "ref", ref_id)

    def close(self):
        if not self.closed:
            self.closed = True
            if self.owner is not None:
                self.cache.release(self.owner, "scope", self.scope_id)

    def __enter__(self):
        self._previous = getattr(self.cache._scope_local, "scope", None)
        self.cache._scope_local.scope = self
        return self

    def __exit__(self, *args):
        self.cache._scope_local.scope = self._previous
        self.close()


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
                "immutable/artifacts",
                "immutable/provenance",
                "immutable/annotations",
                "mutable/refs",
                "mutable/scopes",
                "mutable/garbage",
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
        sid, _ = self._publish_owned("scope", self.process_root)
        self.root_scope = Scope(self, sid, self.process_root)
        self.process_pool = ScopePool(self, self.root_scope)
        USED_CACHES.add(self)

    @classmethod
    def is_cache(cls, path):
        try:
            return _cache_version(path) == "v2" and all(
                (Path(path) / x).is_dir()
                for x in (
                    "immutable/artifacts",
                    "immutable/provenance",
                    "immutable/annotations",
                    "mutable/refs",
                    "mutable/scopes",
                    "mutable/garbage",
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
        return self.path / "mutable/LOCK"

    @property
    def named_pool(self):
        return self._named_pool_

    __enter__ = CacheV1.__enter__
    __exit__ = CacheV1.__exit__

    def __reduce__(self):
        return (
            _restore_cache,
            (
                str(self.path),
                self.process_pool_lifespan / 86400,
                getattr(self, "_handoff_pid", os.getpid()),
                getattr(self, "_handoff_scope", self.root_scope).scope_id,
                self.named_pool.scope.scope_id
                if self.named_pool is not None
                else None,
            ),
        )

    def object_path(self, kind, identity):
        if kind == "artifact":
            if str(uuid.UUID(identity)) != identity:
                raise ValueError("Noncanonical artifact UUID")
            return self.path / "immutable/artifacts" / identity[:3] / identity
        if kind not in ("ref", "scope") or (
            len(identity) != 20 or any(c not in _ALPHABET for c in identity)
        ):
            raise ValueError(f"Invalid {kind} ID: {identity}")
        return self.path / "mutable" / (kind + "s") / identity[:2] / identity

    def marker_target(self, marker):
        value = marker.read_text()
        if not value.endswith("\n") or value.count("\n") != 1:
            raise ValueError(f"Malformed ownership marker: {marker}")
        relative_path(value[:-1])
        target = self.path / value[:-1]
        if not target.resolve().is_relative_to(self.path.resolve()):
            raise ValueError("Ownership marker escapes cache")
        rel = target.relative_to(self.path).parts
        kind = (
            {"refs": "ref", "scopes": "scope", "artifacts": "artifact"}.get(
                rel[1]
            )
            if len(rel) == 4
            else None
        )
        if kind is None or target != self.object_path(kind, rel[-1]):
            raise ValueError(f"Invalid ownership target: {marker}")
        # Typed filenames must agree, including pending memberships.
        name = marker.name.removeprefix(".pending.")
        if (
            name != ".iref"
            and name != f"{kind}.{rel[-1]}.iref"
            and marker.parent != self.keys
        ):
            raise ValueError(
                f"Ownership entry disagrees with marker: {marker}"
            )
        return kind, rel[-1], target

    def _publish_owned(self, kind, owner):
        with self.lock:
            while True:
                identity = new_id()
                dest = self.object_path(kind, identity)
                staging = owner / ".staging" / identity
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
            pending = owner / f".pending.{kind}.{identity}.iref"
            os.link(staging / ".iref", pending)
            fsync_dir(owner)
            shared_mkdir(dest.parent, parents=True, exist_ok=True)
            staging.rename(dest)
            fsync_dir(dest.parent)
            pending.rename(owner / f"{kind}.{identity}.iref")
            fsync_dir(owner)
        return identity, dest

    def _graph_snapshot(self, kind, identity, wanted):
        """Traverse outside LOCK and record directory identities."""
        states = {}
        seen = set()

        def visit(k, i):
            if (k, i) == wanted:
                return True
            if (k, i) in seen or k == "artifact":
                return False
            seen.add((k, i))
            target = self.object_path(k, i)
            entries = []
            for directory in (target, target / "dependencies"):
                try:
                    before = directory.stat()
                except FileNotFoundError:
                    states[directory] = None
                    continue
                entries.extend(directory.glob("*.iref"))
                after = directory.stat()
                signature = (after.st_dev, after.st_ino, after.st_mtime_ns)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_mtime_ns,
                ) != signature:
                    raise BlockingIOError("Ownership changed during discovery")
                states[directory] = signature
            for entry in entries:
                ck, ci, _ = self.marker_target(entry)
                if visit(ck, ci):
                    return True
            return False

        return visit(kind, identity), states

    def retain(self, owner, kind, identity):
        owner = Path(owner)
        target = self.object_path(kind, identity)
        while True:
            states = {}
            cyclic = False
            graph_owner = owner
            if owner.name == "dependencies":
                graph_owner = owner.parent
            if (graph_owner / ".iref").exists() and kind != "artifact":
                ok, oi, _ = self.marker_target(graph_owner / ".iref")
                try:
                    cyclic, states = self._graph_snapshot(
                        kind, identity, (ok, oi)
                    )
                except (BlockingIOError, FileNotFoundError):
                    continue
            with self.lock:
                marker = target / ".iref"
                if not marker.is_file():
                    raise KeyError(f"{kind} {identity} was reclaimed")
                changed = False
                for directory, signature in states.items():
                    try:
                        current = directory.stat()
                        current = (
                            current.st_dev,
                            current.st_ino,
                            current.st_mtime_ns,
                        )
                    except FileNotFoundError:
                        current = None
                    if current != signature:
                        changed = True
                        break
                if changed:
                    continue
                if cyclic:
                    raise ValueError("Ownership graph must be acyclic")
                entry = owner / f"{kind}.{identity}.iref"
                if not entry.exists():
                    os.link(marker, entry)
                    fsync_dir(owner)
                return

    def release(self, owner, kind, identity):
        with self.lock:
            entry = Path(owner) / f"{kind}.{identity}.iref"
            entry.unlink(missing_ok=True)
            if entry.parent.exists():
                fsync_dir(entry.parent)
        self.schedule(kind, identity)

    def schedule(self, kind, identity):
        with self._candidate_lock:
            self._candidates.add((kind, identity))

    def editable_ref(self, ref_id):
        path = self.object_path("ref", ref_id)
        forwards = list(path.glob("ref.*.iref"))
        if forwards:
            if len(forwards) != 1 or list(path.glob("artifact.*.iref")):
                raise ValueError("Malformed forwarding ref")
            kind, identity, _ = self.marker_target(forwards[0])
            if kind != "ref":
                raise ValueError("Invalid forwarding target")
            if list(self.object_path("ref", identity).glob("ref.*.iref")):
                raise ValueError("Forwarding chains are unsupported")
            return identity
        return ref_id

    def attachment(self, ref_id):
        path = self.object_path("ref", self.editable_ref(ref_id))
        entries = list(path.glob("artifact.*.iref"))
        if len(entries) != 1:
            raise ValueError(
                "Result ref is incomplete; resume or finalize it first"
            )
        kind, identity, target = self.marker_target(entries[0])
        if kind != "artifact" or not target.exists():
            raise ValueError("Dangling artifact attachment")
        return identity, target

    def acquire_directory(self, name="directory_format"):
        return self.scope.acquire_directory(name)

    def get_tmp_path(self):
        return self.acquire_directory("temporary").path

    def forwarding_ref(self, ref_id):
        handle = self.acquire_directory("staging")
        target = self.editable_ref(ref_id)
        self.retain(self.object_path("ref", handle.ref_id), "ref", target)
        return handle.ref_id

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
        from .archive.archiver_v2 import V2Archiver

        if (
            isinstance(ref._archiver, V2Archiver)
            and ref._archiver.cache is self
        ):
            self.scope.adopt(ref._archiver.ref_id)
            return ref
        # Explicit transfer preserves source archive semantics and independent
        # view state, even on another filesystem.
        directory = self.acquire_directory("transfer")
        source = directory.path / "source.qza"
        ref._archiver.save(source)
        return rachis.sdk.Result._from_archiver(self.import_archive(source))

    def save(self, ref, key):
        local = self.retain_result(ref)
        target = self.editable_ref(local._archiver.ref_id)
        self._bind_key(key, "ref", target)
        return self.load(key)

    def load(self, key):
        from .archive.archiver_v2 import V2Archiver

        self.validate_key(key)
        handle = self.acquire_directory("staging")
        try:
            with self.lock:
                marker = self.keys / f"{key}.iref"
                if not marker.exists():
                    raise KeyError(key)
                kind, identity, _ = self.marker_target(marker)
                if kind != "ref":
                    raise ValueError(
                        "Key refers to a scope; use load_collection"
                    )
                target = self.editable_ref(identity)
                self.retain(
                    self.object_path("ref", handle.ref_id), "ref", target
                )
            return rachis.sdk.Result._from_archiver(
                V2Archiver(self, handle.ref_id)
            )
        except BaseException:
            handle.release()
            raise

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
            if kind == "ref":
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
            p = self.keys / f"{key}.iref"
            if not p.exists():
                raise KeyError(key)
            kind, identity, _ = self.marker_target(p)
            p.unlink()
            fsync_dir(self.keys)
            recycle = self.pools / key
            retired = self._retire_root(recycle) if recycle.exists() else None
        self.schedule(kind, identity)
        if retired:
            self._delete_garbage(retired)
        self.garbage_collection()

    def save_collection(self, refs, key):
        if isinstance(refs, rachis.sdk.Results):
            refs = refs.output
        scope = self.root_scope.child()
        order = []
        for name, ref in refs.items():
            local = self.retain_result(ref)
            target = self.editable_ref(local._archiver.ref_id)
            scope.adopt(target)
            order.append([name, target])
        durable_write(
            scope.path / "collection.json", json.dumps(order).encode()
        )
        fsync_dir(scope.path)
        self._bind_key(key, "scope", scope.scope_id)
        scope.close()
        return self.load_collection(key)

    def load_collection(self, key):
        from .archive.archiver_v2 import V2Archiver

        self.validate_key(key)
        with self.lock:
            kind, identity, target = self.marker_target(
                self.keys / f"{key}.iref"
            )
            if kind != "scope":
                raise ValueError("Key does not refer to a collection")
            self.root_scope.retain_scope(Scope(self, identity))
            order = json.loads((target / "collection.json").read_text())
        result = rachis.sdk.ResultCollection()
        for name, ref_id in order:
            result[name] = rachis.sdk.Result._from_archiver(
                V2Archiver(self, self.forwarding_ref(ref_id))
            )
        return result

    def create_pool(self, key, reuse=False):
        self.validate_key(key)
        with self.lock:
            container = self.pools / key
            if container.exists():
                if not reuse:
                    raise ValueError("Pool already exists; use reuse=True")
                markers = list(container.glob("scope.*.iref"))
                if len(markers) != 1:
                    raise ValueError("Malformed recycle group")
                _, identity, _ = self.marker_target(markers[0])
                scope = Scope(self, identity)
                self.root_scope.retain_scope(scope)
            else:
                shared_mkdir(container)
                scope = self.root_scope.child()
                self.retain(container, "scope", scope.scope_id)
                self._bind_key(key, "scope", scope.scope_id)
        return ScopePool(self, scope, key)

    def get_pools(self):
        return sorted(p.name for p in self.pools.iterdir() if p.is_dir())

    def get_processes(self):
        return sorted(p.name for p in self.processes.iterdir() if p.is_dir())

    def get_data(self):
        return sorted(
            p.name
            for p in (self.path / "immutable/artifacts").glob("*/*")
            if p.is_dir()
        )

    def _load_uuid(self, identity):
        # A UUID hit never substitutes for interpretation of an archive view.
        return None

    def import_archive(self, filepath, replay=False):
        from .archive.archiver_v2 import import_archive

        return import_archive(self, filepath, replay=replay)

    def broadcast_annotation(self, results, annotation, reference_uuid=None):
        from .archive.archiver_v2 import broadcast_annotation

        archivers = [r._archiver for r in results]
        if any(a.cache is not self for a in archivers):
            raise ValueError("Broadcast targets must belong to this cache")
        broadcast_annotation(archivers, annotation, reference_uuid)

    def broadcast_whiteouts(self, results, records):
        from .archive.archiver_v2 import commit_whiteouts

        archivers = [r._archiver for r in results]
        if any(a.cache is not self for a in archivers):
            raise ValueError("Broadcast targets must belong to this cache")
        commit_whiteouts(archivers, records)

    def _deallocate(self, ref_id):
        # Scopes own refs independently of Python objects. A finalizer only
        # schedules discovery; it cannot invalidate retained pipeline outputs.
        self.schedule("ref", ref_id)

    def reserve_result(self, reserved_uuid=None, **execution):
        directory = self.acquire_directory("cells")
        record = dict(execution, uuid=str(reserved_uuid or uuid.uuid4()))
        durable_write(
            self.object_path("ref", directory.ref_id) / "resumption.json",
            json.dumps(record).encode(),
        )
        fsync_dir(self.object_path("ref", directory.ref_id))
        return directory

    def retain_incomplete(self, directory, key):
        self._bind_key(key, "ref", directory.ref_id)

    def resume(self, key):
        """Acquire exclusive producer authority for an incomplete result."""
        self.validate_key(key)
        with self.lock:
            kind, identity, path = self.marker_target(
                self.keys / f"{key}.iref"
            )
            if kind != "ref" or list(path.glob("artifact.*.iref")):
                raise ValueError("Key does not identify an incomplete result")
            self.scope.adopt(identity)
        return ProducerLease(self, identity)

    def _retire_root(self, path):
        if not path.exists():
            return None
        dest = self.path / "mutable/garbage" / f"root.{new_id()}"
        path.rename(dest)
        fsync_dir(path.parent)
        fsync_dir(dest.parent)
        return dest

    def _delete_garbage(self, path):
        candidates = set()
        try:
            for root, dirs, files in os.walk(path):
                for name in files:
                    if name.endswith(".zip"):
                        try:
                            identity = str(uuid.UUID(name[:-4]))
                        except ValueError:
                            pass
                        else:
                            category = (
                                "annotation_pack"
                                if "annotations" in Path(root).parts
                                else "provenance_pack"
                            )
                            candidates.add((category, identity))
                    if name.endswith(".iref") and name != ".iref":
                        marker = Path(root) / name
                        try:
                            candidates.add(self.marker_target(marker)[:2])
                        except FileNotFoundError:
                            pass
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        for candidate in candidates:
            self.schedule(*candidate)

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
        if (
            not self.path.exists()
            or getattr(self.lock._local, "depth", 0)
        ):
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
            for kind, identity in candidates:
                self._collect_candidate(kind, identity)
        if deep:
            self._collect_orphan_packs()

    def _drain_garbage(self):
        garbage = self.path / "mutable/garbage"
        for tree in list(garbage.iterdir()):
            if tree.is_dir():
                self._delete_garbage(tree)
            else:
                tree.unlink(missing_ok=True)

    def _discover_gc_candidates(self):
        for kind in ("scope", "ref", "artifact"):
            namespace = self.object_path(
                kind, new_id() if kind != "artifact" else str(uuid.uuid4())
            ).parent.parent
            for path in namespace.glob("*/*"):
                if path.is_dir():
                    self.schedule(kind, path.name)
        self._recover_pending()
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
                discovered.st_dev, discovered.st_ino,
            ) or current.st_nlink != 1:
                return None
            garbage = self.path / "mutable/garbage"
            retired = garbage / f"{label}.{new_id()}"
            path.rename(retired)
            fsync_dir(path.parent)
            fsync_dir(garbage)
        return retired

    def _collect_candidate(self, kind, identity):
        is_pack = kind in ("provenance_pack", "annotation_pack")
        if is_pack:
            from .archive.archiver_v2 import pack_path

            category = (
                "provenance" if kind == "provenance_pack" else "annotations"
            )
            path = pack_path(self, category, identity)
        else:
            path = self.object_path(kind, identity)
        marker = path if is_pack else path / ".iref"
        retired = self._retire_unowned(path, marker, f"{kind}.{identity}")
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
        from .archive.archiver_v2 import pack_path

        artifact = self.object_path("artifact", artifact_id)
        if node_id is None:
            bindings = (artifact / "provenance").glob("*.zip")
        else:
            pack_path(self, "provenance", node_id)  # Validate the node UUID.
            bindings = [artifact / "provenance" / (node_id + ".zip")]
        for binding in bindings:
            self._refresh_provenance_binding(artifact, binding)

    def _refresh_provenance_binding(self, artifact, binding):
        from .archive.archiver_v2 import pack_path, pack_revision

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
        """Create an independent editable view with retained pack revisions."""
        source = self.object_path("ref", self.editable_ref(ref_id))
        directory = self.acquire_directory("staging")
        dest = self.object_path("ref", directory.ref_id)
        with self.lock:
            artifact_id, _ = self.attachment(ref_id)
            pending = dest / f".pending.artifact.{artifact_id}.iref"
            os.link(
                self.object_path("artifact", artifact_id) / ".iref", pending
            )
            fsync_dir(dest)
            shared_mkdir(dest / "archive-metadata")
            for file in (source / "archive-metadata").iterdir():
                # Root metadata is immutable per ref; hardlinks are safe.
                os.link(file, dest / "archive-metadata" / file.name)
            order = source / "annotations-order.json"
            if order.exists():
                os.link(order, dest / order.name)
            for algorithm in ("sha512", "md5"):
                file = source / ("archive-checksums." + algorithm)
                if file.exists():
                    os.link(file, dest / file.name)
            from .archive.archiver_v2 import read_whiteouts, whiteout_bytes

            durable_write(
                dest / "whiteout.jsonl", whiteout_bytes(read_whiteouts(source))
            )
            if (source / "annotations").exists():
                shared_mkdir(dest / "annotations")
                for pack in (source / "annotations").glob("*.zip"):
                    os.link(pack, dest / "annotations" / pack.name)
                fsync_dir(dest / "annotations")
            fsync_dir(dest / "archive-metadata")
            pending.rename(dest / f"artifact.{artifact_id}.iref")
            fsync_dir(dest)
        return directory.ref_id

    def _recover_pending(self):
        for base in ("mutable/refs", "mutable/scopes", "keys/proc"):
            for owner in (self.path / base).glob(
                "*/*" if base != "keys/proc" else "*"
            ):
                for marker in owner.glob(".pending.*.iref"):
                    kind, identity, target = self.marker_target(marker)
                    # Pending artifact attachments require explicit view
                    # completion. They must never be promoted by GC.
                    if kind == "artifact":
                        continue
                    with self.lock:
                        if target.exists() and marker.exists():
                            marker.rename(owner / marker.name[9:])
                            fsync_dir(owner)
                if list(owner.glob("artifact.*.iref")):
                    for name in ("cells", "dependencies", "staging"):
                        redundant = owner / name
                        with self.lock:
                            retired = self._retire_root(redundant)
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
        from .archive.archiver_v2 import seal_tree

        reserved = json.loads((self.path / "resumption.json").read_text())[
            "uuid"
        ]
        import yaml

        metadata = yaml.safe_load(
            (Path(archive_path) / "metadata.yaml").read_text()
        )
        if metadata["uuid"] != reserved:
            raise ValueError("Finalization must preserve the reserved UUID")
        return seal_tree(self.cache, self.ref_id, Path(archive_path))

    def __exit__(self, *args):
        self._done.set()
        self.lock.unlock()


class ScopePool:
    """Pool API implemented as scope retention and recycle roots."""

    def __init__(self, cache, scope, name=None):
        self.cache = cache
        self.scope = scope
        self.path = scope.path
        self.name = name or scope.scope_id
        self.index = {}

    def __enter__(self):
        self._previous = getattr(_CACHE, "cache", None)
        if (
            self._previous is not None
            and self._previous.path != self.cache.path
        ):
            raise ValueError("Pool belongs to another cache")
        if self.cache.named_pool is not None:
            raise ValueError("Cannot enter multiple pools")
        _CACHE.cache = self.cache
        self.cache._named_pool_ = self
        return self

    def __exit__(self, *args):
        _CACHE.cache = self._previous
        self.cache._named_pool_ = None

    def save(self, ref):
        local = self.cache.retain_result(ref)
        self.scope.adopt(local._archiver.ref_id)
        return local

    def load(self, identity):
        from .archive.archiver_v2 import V2Archiver

        for marker in self.path.glob("ref.*.iref"):
            _, ref_id, _ = self.cache.marker_target(marker)
            try:
                artifact_id, _ = self.cache.attachment(ref_id)
            except ValueError:
                continue
            if identity in (ref_id, artifact_id):
                return rachis.sdk.Result._from_archiver(
                    V2Archiver(self.cache, self.cache.forwarding_ref(ref_id))
                )
        raise KeyError(identity)

    def remove(self, ref):
        identity = (
            ref._archiver.ref_id if hasattr(ref, "_archiver") else str(ref)
        )
        for marker in list(self.path.glob("ref.*.iref")):
            _, ref_id, _ = self.cache.marker_target(marker)
            if (
                identity == ref_id
                or identity == self.cache.attachment(ref_id)[0]
            ):
                self.scope.release(ref_id)
        self.cache.garbage_collection()

    def get_data(self):
        result = set()
        for marker in self.path.glob("ref.*.iref"):
            _, ref_id, _ = self.cache.marker_target(marker)
            try:
                result.add(self.cache.attachment(ref_id)[0])
            except ValueError:
                pass
        return result

    def create_index(self):
        from .archive.archiver_v2 import V2Archiver
        from .util import load_action_yaml
        from .type import HashableInvocation
        from .cache import Pool

        self.index = {}
        for marker in list(self.path.glob("ref.*.iref")):
            _, ref_id, _ = self.cache.marker_target(marker)
            handle = V2Archiver(self.cache, self.cache.forwarding_ref(ref_id))
            if handle.provenance_dir is None:
                continue
            with handle.snapshot() as view:
                action = load_action_yaml(view.root)["action"]
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


def _restore_cache(path, lifespan, source_pid, source_scope, named_scope):
    cache = Cache(path, lifespan)
    if source_pid != os.getpid():
        cache._handoff_pid = source_pid
        cache._handoff_scope = Scope(cache, source_scope)
        if named_scope is not None:
            scope = Scope(cache, named_scope)
            cache.root_scope.retain_scope(scope)
            cache._named_pool_ = ScopePool(cache, scope)
    return cache
