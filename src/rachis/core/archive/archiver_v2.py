# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Pack storage and archive-format preserving streaming export."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import uuid
import weakref
import zipfile

import yaml

from .archiver import Archiver, ArchiveRecord
from .view import LogicalPath, LiveArchiveView, materialize_tree
from ..cache_v2 import (
    new_id,
    relative_path,
    durable_write,
    flush_tree,
    fsync_dir,
    shared_mkdir,
)
from ..util import to_checksum_format, from_checksum_format, checksum_python
from ..cite import Citations


def manifest_bytes(digests):
    """Serialize newly generated manifests in deterministic path order."""
    return (
        (
            "\n".join(
                to_checksum_format(p, h) for p, h in sorted(digests.items())
            )
            + "\n"
        ).encode()
        if digests
        else b""
    )


def parse_manifest(data):
    return dict(
        from_checksum_format(line)
        for line in data.decode().splitlines()
        if line
    )


def metadata_members(members):
    """Identify eligible metadata from the node's actual action schema."""
    action = members.get("action/action.yaml")
    if action is None:
        return set()
    data = action() if callable(action) else action
    tree = yaml.compose(data)
    result = set()

    def visit(node):
        if node is None:
            return
        if isinstance(node, yaml.ScalarNode) and node.tag == "!metadata":
            name = node.value.split(":", 1)[-1]
            relative_path(name)
            result.add("action/" + name)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                visit(key)
                visit(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                visit(value)

    visit(tree)
    return result


def pack_path(cache, category, identity):
    if str(uuid.UUID(identity)) != identity:
        raise ValueError("Noncanonical pack UUID")
    return (
        cache.path
        / "immutable"
        / category
        / identity[:3]
        / (identity + ".zip")
    )


def write_pack(path, identity, files, hashes=None, md5=None, payload=None):
    hashes = {} if hashes is None else hashes
    index = {}
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as pack:
        for member, source in sorted(files.items()):
            name = identity + "/" + member
            relative_path(name)
            digest = hashes.get(member)
            hasher = hashlib.sha512() if digest is None else None
            with source.open("rb") as stream, pack.open(name, "w") as output:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    output.write(chunk)
                    if hasher:
                        hasher.update(chunk)
            index[name] = digest if digest is not None else hasher.hexdigest()
        pack.writestr(
            "checksums.sha512",
            manifest_bytes(index),
            compress_type=zipfile.ZIP_STORED,
        )
        if md5:
            pack.writestr(
                "checksums.md5",
                manifest_bytes(
                    {identity + "/" + k: v for k, v in md5.items()}
                ),
                compress_type=zipfile.ZIP_STORED,
            )
        if payload:
            pack.writestr(
                "payload-checksums.sha512",
                manifest_bytes(payload),
                compress_type=zipfile.ZIP_STORED,
            )
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    os.chmod(path, 0o660)


def pack_revision(path, identity):
    with zipfile.ZipFile(path) as pack:
        prefix = identity + "/"
        names = [
            n
            for n in pack.namelist()
            if n.startswith(prefix) and not n.endswith("/")
        ]
        digests = parse_manifest(pack.read("checksums.sha512"))
        members = {n[len(prefix) :]: lambda n=n: pack.read(n) for n in names}
        eligible = metadata_members(members)
        sizes = {n[len(prefix) :]: pack.getinfo(n).file_size for n in names}
    return ({n[len(prefix) :]: digests[n] for n in names}, sizes, eligible)


def _provenance_replacement(previous, incoming, identity):
    """Allow restoration of metadata, rejecting changes to known originals."""
    old_hashes, old_sizes, old_eligible = previous
    new_hashes, new_sizes, eligible = incoming
    if old_hashes.keys() != new_hashes.keys() or old_eligible != eligible:
        raise ValueError(f"Conflicting provenance members for {identity}")
    replace = False
    for member in old_hashes:
        if old_hashes[member] == new_hashes[member]:
            continue
        if member not in eligible or (old_sizes[member] and new_sizes[member]):
            raise ValueError(
                f"Conflicting provenance originals for {identity}/{member}"
            )
        if new_sizes[member]:
            replace = True
    # Initial implementation permits only complete redaction.
    if replace and any(old_sizes[n] and not new_sizes[n] for n in eligible):
        raise ValueError("Unsupported complementary redacted view")
    return replace


def publish_pack(cache, category, identity, incoming):
    """Optimistic pack revision selection; comparison stays outside LOCK."""
    catalogue = pack_path(cache, category, identity)
    incoming_revision = pack_revision(incoming, identity)
    while True:
        # Retain a revision with an opened inode while preparing comparison.
        try:
            retained = catalogue.open("rb")
        except FileNotFoundError:
            retained = None
        if retained is not None:
            with retained:
                original_stat = os.fstat(retained.fileno())
                previous = pack_revision(retained, identity)
            if category == "annotations":
                replace = previous[0] != incoming_revision[0]
            else:
                replace = _provenance_replacement(
                    previous, incoming_revision, identity
                )
        else:
            original_stat = None
            replace = True
        with cache.lock:
            try:
                current = catalogue.stat()
            except FileNotFoundError:
                current = None
            if (
                (current is None) != (original_stat is None)
                or current is not None
                and (current.st_dev, current.st_ino)
                != (original_stat.st_dev, original_stat.st_ino)
            ):
                continue
            shared_mkdir(catalogue.parent, parents=True, exist_ok=True)
            if replace:
                temporary = catalogue.parent / ("." + new_id())
                os.link(incoming, temporary)
                os.replace(temporary, catalogue)
                fsync_dir(catalogue.parent)
            # Retain the selected inode into a private workspace before unlock.
            selected = incoming.parent / (
                "selected." + identity + "." + new_id()
            )
            # Annotations with conflicting UUID bytes keep the importing
            # revision even if the catalogue selects another revision later.
            os.link(
                incoming if category == "annotations" else catalogue, selected
            )
        return selected


def _files(path):
    return {
        p.relative_to(path).as_posix(): p
        for p in path.rglob("*")
        if p.is_file()
    }


def import_archive(cache, filepath, replay=False):
    archive = Archiver.get_archive(filepath)
    Format = Archiver.get_format_class(archive.version)
    if Format is None:
        Archiver._futuristic_archive_error(filepath, archive)
    Format.load_metadata(archive)
    workspace = cache.acquire_directory("staging")
    root = workspace.path / str(archive.uuid)
    root.mkdir()
    try:
        # Validate lexical ZIP members before extraction.
        if zipfile.is_zipfile(filepath):
            with zipfile.ZipFile(filepath) as source:
                for item in source.infolist():
                    relative_path(item.filename)
                    if item.external_attr >> 16 & 0o170000 == 0o120000:
                        raise ValueError("Archive symlinks are unsupported")
        if Path(filepath).is_dir():
            shutil.copytree(filepath, root, dirs_exist_ok=True)
        else:
            archive.mount(root)
        return seal_tree(cache, workspace.ref_id, root, replay=replay)
    except BaseException:
        workspace.release()
        cache.garbage_collection()
        raise


def seal_tree(cache, ref_id, root, replay=False, generated=False):
    return _ArchiveSealer(cache, ref_id, root, replay, generated).seal()


class _ArchiveSealer:
    """Prepare private storage, publish the artifact, then commit its view."""

    def __init__(self, cache, ref_id, root, replay, generated):
        self.cache = cache
        self.ref_id = ref_id
        self.root = root
        self.replay = replay
        self.generated = generated
        self.ref = cache.object_path("ref", ref_id)
        self.provenance = root / "provenance"
        self.inherited = self.provenance / ".cache-v2-ancestry"
        self.whiteouts = set()
        self.selected_packs = {}
        self.annotations = {}

    def seal(self):
        self._read_source()
        self._prepare_payload()
        self._prepare_provenance()
        self._prepare_annotations()
        self._build_candidate()
        self._publish_artifact()
        self._publish_view()
        self._cleanup_construction()
        result = V2Archiver(self.cache, self.ref_id, replay=self.replay)
        if self.cache.named_pool is not None:
            self.cache.named_pool.scope.adopt(self.ref_id)
        return result

    def _read_source(self):
        if self.generated:
            self._remove_hidden_payload_files()
        if list(self.ref.glob("artifact.*.iref")):
            raise ValueError("Ref is already finalized")
        source_archive = Archiver.get_archive(self.root)
        metadata = yaml.safe_load((self.root / "metadata.yaml").read_text())
        self.identity = metadata["uuid"]
        source_archive.uuid = uuid.UUID(self.identity)
        Format = Archiver.get_format_class(source_archive.version)
        if Format is None:
            Archiver._futuristic_archive_error(self.root, source_archive)
        # Parse metadata using the selected adapter before publication.
        Format(
            ArchiveRecord(
                self.root,
                self.root / "VERSION",
                source_archive.uuid,
                source_archive.version,
                source_archive.framework_version,
            ),
            replay=self.replay,
        )
        self.stage = self.ref / "staging"
        self.stage.mkdir(exist_ok=True)
        self.hashes = {}
        self.manifests = {}
        for algorithm in ("md5", "sha512"):
            manifest = self.root / ("checksums." + algorithm)
            if manifest.exists():
                self.manifests[algorithm] = manifest.read_bytes()
                self.hashes[algorithm] = parse_manifest(
                    self.manifests[algorithm]
                )

    def _remove_hidden_payload_files(self):
        # Generated archives exclude hidden plugin outputs before sealing.
        for parent, dirs, files in os.walk(self.root / "data", topdown=False):
            for name in files:
                if name.startswith("."):
                    (Path(parent) / name).unlink()
            for name in dirs:
                if name.startswith("."):
                    shutil.rmtree(Path(parent) / name)

    def _prepare_payload(self):
        self.artifact = self.cache.object_path("artifact", self.identity)
        self.pending = self.ref / f".pending.artifact.{self.identity}.iref"
        with self.cache.lock:
            self.hit = self.artifact.exists()
            if self.hit and not self.pending.exists():
                os.link(self.artifact / ".iref", self.pending)
                fsync_dir(self.ref)
        if self.hit:
            self.payload = parse_manifest(
                (self.artifact / "data-checksums.sha512").read_bytes()
            )
        else:
            self.payload = {}
            for member, file in _files(self.root / "data").items():
                name = "data/" + member
                self.payload[name] = self.hashes.get("sha512", {}).get(name)
                if self.payload[name] is None:
                    self.payload[name] = checksum_python(file, "sha512")

    def _provenance_nodes(self):
        if not self.provenance.exists():
            return []
        nodes = [(self.identity, self.provenance, "provenance/")]
        ancestors = self.provenance / "artifacts"
        if ancestors.exists():
            nodes.extend(
                (p.name, p, "provenance/artifacts/" + p.name + "/")
                for p in ancestors.iterdir() if p.is_dir()
            )
        return nodes

    def _prepare_provenance(self):
        # Prepare every node before exposing any catalogue revisions.
        prepared = [
            self._prepare_provenance_node(*node)
            for node in self._provenance_nodes()
        ]
        for node_id, pack in prepared:
            self.selected_packs[node_id] = publish_pack(
                self.cache, "provenance", node_id, pack
            )
        self._retain_inherited_provenance()

    def _prepare_provenance_node(self, node_id, node, prefix):
        files = _files(node)
        files = {
            n: p
            for n, p in files.items()
            if not n.startswith("artifacts/")
            and not n.startswith(".cache-v2-ancestry/")
        }
        eligible = metadata_members(
            {k: p.read_bytes for k, p in files.items()}
        )
        empty = {
            n
            for n in eligible
            if n in files and files[n].stat().st_size == 0
        }
        if empty and empty != eligible:
            raise ValueError(
                "Unsupported partially redacted provenance node"
            )
        self.whiteouts.update((node_id, n) for n in empty)
        pack = self.stage / (node_id + ".zip")
        write_pack(
            pack,
            node_id,
            files,
            hashes=self._member_hashes(files, prefix, "sha512"),
            md5=self._member_hashes(files, prefix, "md5"),
            payload=self.payload if node_id == self.identity else None,
        )
        return node_id, pack

    def _member_hashes(self, files, prefix, algorithm):
        hashes = self.hashes.get(algorithm, {})
        return {
            name: hashes[prefix + name] for name in files
            if prefix + name in hashes
        }

    def _retain_inherited_provenance(self):
        if not self.inherited.exists():
            return
        self.whiteouts.update(read_whiteouts(self.inherited))
        for pack in self.inherited.glob("*.zip"):
            # Captures carry sufficient immutable packs from input snapshots.
            self.selected_packs.setdefault(
                pack.stem,
                publish_pack(self.cache, "provenance", pack.stem, pack),
            )
        self._extend_manifest_with_ancestors()

    def _extend_manifest_with_ancestors(self):
        if "sha512" not in self.manifests:
            return
        extended = dict(self.hashes["sha512"])
        for node_id, pack in self.selected_packs.items():
            if node_id == self.identity:
                continue
            with zipfile.ZipFile(pack) as z:
                index = parse_manifest(z.read("checksums.sha512"))
            for member, digest in index.items():
                relative = member[len(node_id) + 1 :]
                name = "provenance/artifacts/" + node_id + "/" + relative
                if (node_id, relative) in self.whiteouts:
                    digest = hashlib.sha512(b"").hexdigest()
                extended[name] = digest
        if extended != self.hashes["sha512"]:
            self.hashes["sha512"] = extended
            self.manifests["sha512"] = manifest_bytes(extended)

    def _prepare_annotations(self):
        if (self.root / "annotations").exists():
            for node in (self.root / "annotations").iterdir():
                if not node.is_dir():
                    continue
                pack = self.stage / ("annotation." + node.name + ".zip")
                write_pack(pack, node.name, _files(node))
                self.annotations[node.name] = publish_pack(
                    self.cache, "annotations", node.name, pack
                )
        for pack in (self.inherited / "annotations").glob("*.zip"):
            self.annotations.setdefault(
                pack.stem,
                publish_pack(self.cache, "annotations", pack.stem, pack),
            )

    def _build_candidate(self):
        # Canonical UUID hits reuse the retained payload.
        self.candidate = self.stage / ("artifact." + new_id())
        if self.hit:
            return
        shared_mkdir(self.candidate)
        marker_path = self.artifact.relative_to(self.cache.path).as_posix()
        durable_write(self.candidate / ".iref", (marker_path + "/\n").encode())
        shutil.copytree(self.root / "data", self.candidate / "data")
        # Establish group permissions on private payload copies before
        # publication, leaving shared pack inodes and source files untouched.
        for parent, dirs, files in os.walk(self.candidate / "data"):
            os.chmod(parent, 0o2770)
            for name in files:
                os.chmod(Path(parent) / name, 0o660)
        durable_write(
            self.candidate / "data-checksums.sha512",
            manifest_bytes(self.payload),
        )
        if self.selected_packs:
            shared_mkdir(self.candidate / "provenance")
            for node_id, pack in self.selected_packs.items():
                os.link(
                    pack, self.candidate / "provenance" / (node_id + ".zip")
                )
        flush_tree(self.candidate)

    def _publish_artifact(self):
        with self.cache.lock:
            if self.pending.exists():
                _, old_id, _ = self.cache.marker_target(self.pending)
                if old_id != self.identity:
                    raise ValueError("Pending artifact identity mismatch")
            elif self.artifact.exists():
                os.link(self.artifact / ".iref", self.pending)
            else:
                shared_mkdir(self.artifact.parent, parents=True, exist_ok=True)
                os.link(self.candidate / ".iref", self.pending)
                fsync_dir(self.ref)
                self.candidate.rename(self.artifact)
                fsync_dir(self.artifact.parent)
            # Selected files retain sufficient revisions through publication.
            for node_id, pack in self.selected_packs.items():
                dest = self.artifact / "provenance" / (node_id + ".zip")
                shared_mkdir(dest.parent, exist_ok=True)
                if dest.exists():
                    # A later publisher may already have enriched it. Never
                    # replace that revision with an earlier redacted selection.
                    current = pack_path(self.cache, "provenance", node_id)
                    pack = current
                temporary = dest.parent / ("." + new_id())
                os.link(pack, temporary)
                os.replace(temporary, dest)
            if self.selected_packs:
                fsync_dir(self.artifact / "provenance")
            fsync_dir(self.ref)

    def _write_metadata(self):
        metadata_dir = self.ref / "archive-metadata"
        shared_mkdir(metadata_dir, exist_ok=True)
        for name in ("VERSION", "metadata.yaml"):
            durable_write(metadata_dir / name, (self.root / name).read_bytes())
        # Preserve extra root-level archive members independently of UUID hits.
        for file in self.root.iterdir():
            if file.is_file() and file.name not in (
                "VERSION",
                "metadata.yaml",
                "checksums.sha512",
                "checksums.md5",
            ):
                durable_write(metadata_dir / file.name, file.read_bytes())
        for algorithm, data in self.manifests.items():
            durable_write(self.ref / ("archive-checksums." + algorithm), data)
        return metadata_dir

    def _publish_view(self):
        metadata_dir = self._write_metadata()
        durable_write(
            self.ref / "whiteout.jsonl", whiteout_bytes(self.whiteouts)
        )
        if self.annotations:
            shared_mkdir(self.ref / "annotations", exist_ok=True)
            for node_id, pack in self.annotations.items():
                os.link(pack, self.ref / "annotations" / (node_id + ".zip"))
        flush_tree(metadata_dir)
        if self.annotations:
            fsync_dir(self.ref / "annotations")
            durable_write(
                self.ref / "annotations-order.json",
                json.dumps(list(self.annotations)).encode(),
            )
        with self.cache.lock:
            self.pending.rename(self.ref / f"artifact.{self.identity}.iref")
            fsync_dir(self.ref)

    def _cleanup_construction(self):
        # Remove construction and dependencies after durable commit.
        for name in ("staging", "cells", "dependencies"):
            tree = self.ref / name
            if tree.exists():
                with self.cache.lock:
                    retired = self.cache._retire_root(tree)
                if retired:
                    self.cache._delete_garbage(retired)


def whiteout_bytes(records):
    return "".join(
        json.dumps({"uuid": u, "path": p}, separators=(",", ":")) + "\n"
        for u, p in sorted(records)
    ).encode()


def read_annotation_order(ref, identifiers):
    path = ref / "annotations-order.json"
    available = list(identifiers)
    if not path.exists():
        return available
    order = json.loads(path.read_text())
    if (
        not isinstance(order, list)
        or len(set(order)) != len(order)
        or any(not isinstance(i, str) or str(uuid.UUID(i)) != i for i in order)
    ):
        raise ValueError("Malformed annotation membership order")
    return [i for i in order if i in available] + [
        i for i in available if i not in order
    ]


def write_annotation_order(ref, order):
    staging = ref / "staging"
    shared_mkdir(staging, exist_ok=True)
    temporary = staging / ("annotation-order." + new_id())
    durable_write(temporary, json.dumps(order).encode())
    os.replace(temporary, ref / "annotations-order.json")
    fsync_dir(ref)


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


class ArchiveSnapshot:
    """Retained consistent mutable state plus opened pack revisions."""

    def __init__(self, archiver):
        self.cache = archiver.cache
        self.uuid = str(archiver.uuid)
        self.root = LogicalPath(self)
        self.closed = False
        self.scope = None
        self._resources = contextlib.ExitStack()
        self.sources = {}
        self.digests = {"sha512": {}, "md5": {}}
        self.packs = []
        try:
            self.scope = self.cache.root_scope.child()
            self.scope.adopt(archiver.ref_id)
            artifact_id, _ = self.cache.attachment(archiver.ref_id)
            self.cache.refresh_provenance(artifact_id)
            self._capture_state(archiver)
            self._index_sources()
            self._validate_whiteouts()
            for algorithm in self.original:
                name = "checksums." + algorithm
                self.sources[name] = self.manifest(algorithm)
        except BaseException:
            self.close()
            raise

    def _capture_state(self, archiver):
        """Capture mutable metadata and open revisions in one transaction."""
        with self.cache.lock:
            ref = self.cache.object_path(
                "ref", self.cache.editable_ref(archiver.ref_id)
            )
            _, artifact = self.cache.attachment(archiver.ref_id)
            self.whiteouts = read_whiteouts(ref)
            self.original = {
                algorithm: path.read_bytes()
                for algorithm in ("sha512", "md5")
                if (path := ref / ("archive-checksums." + algorithm)).exists()
            }
            self.metadata = {
                p.name: p.read_bytes()
                for p in (ref / "archive-metadata").iterdir()
            }
            version = self.metadata["VERSION"].decode().splitlines()[1]
            version = version.split(": ", 1)[1]
            # Retain opened files immediately, even if a later open fails.
            self._provenance_files = (
                self._open_bindings(artifact / "provenance")
                if version != "0" else []
            )
            self._annotation_files = self._open_bindings(ref / "annotations")
            self.annotation_ids = read_annotation_order(
                ref, [identity for identity, _ in self._annotation_files]
            )
            self.data_dir = artifact / "data"
            self._payload_manifest = artifact / "data-checksums.sha512"

    def _open_bindings(self, namespace):
        return [
            (p.stem, self._resources.enter_context(p.open("rb")))
            for p in namespace.glob("*.zip")
        ]

    def _index_sources(self):
        """Read pack indexes and build logical sources outside LOCK."""
        self.digests["sha512"].update(
            parse_manifest(self._payload_manifest.read_bytes())
        )
        for algorithm, data in self.original.items():
            self.digests[algorithm].update(parse_manifest(data))
        self.sources.update(self.metadata)
        self.sources.update(
            ("data/" + name, path)
            for name, path in _files(self.data_dir).items()
        )
        for node_id, file in self._provenance_files:
            prefix = (
                "provenance/" if node_id == self.uuid else
                "provenance/artifacts/" + node_id + "/"
            )
            self._add_pack(node_id, file, prefix, True)
        for node_id, file in self._annotation_files:
            self._add_pack(
                node_id, file, "annotations/" + node_id + "/", False
            )

    def _validate_whiteouts(self):
        valid = {
            (node_id, member)
            for node_id, members in self._node_members.items()
            for member in metadata_members(members)
        }
        if not self.whiteouts.issubset(valid):
            raise ValueError("Published whiteout targets ineligible metadata")

    @property
    def _node_members(self):
        result = {}
        for category, node_id, pack in self.packs:
            if category != "provenance":
                continue
            prefix = node_id + "/"
            result[node_id] = {
                n[len(prefix) :]: lambda n=n, pack=pack: pack.read(n)
                for n in pack.namelist()
                if n.startswith(prefix) and not n.endswith("/")
            }
        return result

    def _add_pack(self, node_id, file, prefix, provenance):
        pack = self._resources.enter_context(zipfile.ZipFile(file))
        self.packs.append(
            ("provenance" if provenance else "annotations", node_id, pack)
        )
        # ZipFile doesn't own caller-provided streams; explicitly keep them.
        pack._cache_file = file
        indexes = {"sha512": parse_manifest(pack.read("checksums.sha512"))}
        if "checksums.md5" in pack.namelist():
            indexes["md5"] = parse_manifest(pack.read("checksums.md5"))
        for member in pack.namelist():
            if not member.startswith(node_id + "/") or member.endswith("/"):
                continue
            rel = member[len(node_id) + 1 :]
            logical = prefix + rel
            if provenance and (node_id, rel) in self.whiteouts:
                self.sources[logical] = b""
                for algorithm in ("sha512", "md5"):
                    self.digests[algorithm][logical] = hashlib.new(
                        algorithm, b""
                    ).hexdigest()
            else:
                self.sources[logical] = (pack, member)
                for algorithm, index in indexes.items():
                    if member in index:
                        self.digests[algorithm][logical] = index[member]

    def members(self):
        return sorted(self.sources)

    def is_file(self, member):
        return member in self.sources

    def is_dir(self, member):
        prefix = member.rstrip("/") + "/" if member else ""
        return any(n.startswith(prefix) for n in self.sources)

    def open_member(self, member):
        if self.closed:
            raise ValueError("Snapshot is closed")
        if member not in self.sources:
            raise FileNotFoundError(member)
        source = self.sources[member]
        if isinstance(source, bytes):
            return io.BytesIO(source)
        if isinstance(source, tuple):
            return source[0].open(source[1])
        return source.open("rb")

    def digest(self, member, algorithm):
        digest = self.digests[algorithm].get(member)
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
        if covered == parse_manifest(original):
            return original
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
        try:
            self._resources.close()
        finally:
            if self.scope is not None:
                self.scope.close()
            self.cache.garbage_collection()


def annotation_state(cache, ref):
    retained = []
    membership = ref / "annotations"
    with cache.lock:
        for path in membership.glob("*.zip"):
            stream = path.open("rb")
            retained.append(
                (path.stem, stream, os.fstat(stream.fileno()).st_ino)
            )
    metadata = {}
    try:
        for identity, stream, inode in retained:
            with zipfile.ZipFile(stream) as z:
                metadata[identity] = yaml.safe_load(
                    z.read(identity + "/metadata.yaml")
                )
    finally:
        for _, stream, _ in retained:
            stream.close()
    return {i: inode for i, _, inode in retained}, metadata


def annotation_fingerprint(ref):
    return {
        p.stem: p.stat().st_ino for p in (ref / "annotations").glob("*.zip")
    }


class V2Archiver(Archiver):
    def __init__(self, cache, ref_id, replay=False):
        self.cache = cache
        self._replay = replay
        self.ref_id = ref_id
        self.process_alias = ref_id
        identity, artifact = cache.attachment(ref_id)
        self._uuid = uuid.UUID(identity)
        self.view = LiveArchiveView(self)
        root = self.view.root
        ref = cache.object_path("ref", cache.editable_ref(ref_id))
        version_data = (ref / "archive-metadata/VERSION").read_text()
        version = version_data.splitlines()[1].split(": ", 1)[1]
        framework = version_data.splitlines()[2].split(": ", 1)[1]
        Format = self.get_format_class(version)
        record = ArchiveRecord(
            root, root / "VERSION", self._uuid, version, framework
        )
        self._fmt = Format(record, replay=replay)
        self._fmt.data_dir = artifact / "data"
        self.path = root
        self._memoize_annotations = []
        self._destructor = weakref.finalize(self, cache._deallocate, ref_id)

    def __reduce__(self):
        # Returning from a worker installs parent ownership before that
        # worker can retire its process root.
        owner = getattr(self.cache, "_handoff_scope", self.cache.root_scope)
        owner.adopt(self.ref_id)
        return (_restore_archiver, (self.cache, self.ref_id, self._replay))

    def __copy__(self):
        duplicate = object.__new__(type(self))
        duplicate.__dict__ = self.__dict__.copy()
        return duplicate

    def __deepcopy__(self, memo):
        # Explicit snapshot_ref creates a new editable storage identity.
        return self

    @property
    def uuid(self):
        return self._uuid

    def snapshot(self):
        return ArchiveSnapshot(self)

    def save(self, filepath):
        with (
            self.snapshot() as view,
            zipfile.ZipFile(
                filepath,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as output,
        ):
            for member in view.members():
                with (
                    view.open_member(member) as source,
                    output.open(str(self.uuid) + "/" + member, "w") as target,
                ):
                    shutil.copyfileobj(source, target, 1024 * 1024)

    def get_checksums(self):
        with self.snapshot() as view:
            data = view.manifest(self._fmt.CHECKSUM_TYPE)
            if data is None:
                raise FileNotFoundError(self._fmt.CHECKSUM_FILE)
            return parse_manifest(data)

    def write_checksums(self, checksums):
        raise TypeError(
            "V2 manifests are derived from the logical archive view"
        )

    def validate_checksums(self):
        from .archiver import ChecksumDiff

        if not self.has_checksums():
            return ChecksumDiff({}, {}, {})
        with self.snapshot() as view:
            algorithm = self._fmt.CHECKSUM_TYPE
            if algorithm not in view.original:
                raise FileNotFoundError(self._fmt.CHECKSUM_FILE)
            expected = parse_manifest(view.original[algorithm])
            for node_id, member in view.whiteouts:
                prefix = (
                    "provenance/"
                    if node_id == str(self.uuid)
                    else "provenance/artifacts/" + node_id + "/"
                )
                name = prefix + member
                if name in expected:
                    expected[name] = hashlib.new(algorithm, b"").hexdigest()
            observed = {}
            for member in view.members():
                if member in (
                    "checksums.md5",
                    "checksums.sha512",
                ) or member.startswith("annotations/"):
                    continue
                hasher = hashlib.new(algorithm)
                with view.open_member(member) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                observed[member] = hasher.hexdigest()
            added = {n: observed[n] for n in observed.keys() - expected.keys()}
            removed = {
                n: expected[n] for n in expected.keys() - observed.keys()
            }
            changed = {
                n: (expected[n], observed[n])
                for n in expected.keys() & observed.keys()
                if expected[n] != observed[n]
            }
            return ChecksumDiff(added, removed, changed)

    @property
    def citations(self):
        from copy import copy

        if self.provenance_dir is None:
            return Citations()
        with self.snapshot() as view:
            fmt = copy(self._fmt)
            fmt.provenance_dir = view.root / "provenance"
            return getattr(fmt, "citations", Citations())

    @property
    def _annotations(self):
        from rachis.core.annotate import Annotation

        result = {}
        with self.snapshot() as view:
            annotations = view.root / "annotations"
            if annotations.exists():
                for identity in view.annotation_ids:
                    annotation = Annotation.load(annotations / identity)
                    result[annotation.name] = annotation
        return result

    def add_annotation(self, annotation, reference_uuid=None):
        broadcast_annotation([self], annotation, reference_uuid)

    def _prepare_annotation(self, annotation, reference_uuid=None):
        from rachis.core.annotate import Annotation

        self._validate_annotation_support()
        directory = self.cache.acquire_directory("annotation")
        archive_root = directory.path / str(self.uuid)
        archive_root.mkdir()
        annotations_root = archive_root / "annotations"
        annotations_root.mkdir()
        if annotation.annotation_type == "Signature":
            with self.snapshot() as view:
                durable_write(
                    archive_root / "checksums.sha512", view.manifest("sha512")
                )
        annotation._write(
            annotations_root, str(self.uuid), str(reference_uuid or self.uuid)
        )
        node = annotations_root / str(annotation.id)
        digests = {
            n: checksum_python(p, self._fmt.CHECKSUM_TYPE)
            for n, p in _files(node).items()
        }
        durable_write(node / self._fmt.CHECKSUM_FILE, manifest_bytes(digests))
        pack = directory.path / "pack.zip"
        write_pack(pack, str(annotation.id), _files(node))
        selected = publish_pack(
            self.cache, "annotations", str(annotation.id), pack
        )
        # Read attachment name outside LOCK; revalidate all committed names
        # inside the transaction using metadata alone.
        loaded = Annotation.load(node)
        return directory, selected, loaded

    def remove_annotation(self, name):
        self._validate_annotation_support()
        ref = self.cache.object_path(
            "ref", self.cache.editable_ref(self.ref_id)
        )
        while True:
            fingerprint, metadata = annotation_state(self.cache, ref)
            with self.cache.lock:
                if annotation_fingerprint(ref) != fingerprint:
                    continue
                for identity, md in metadata.items():
                    if md["name"] == name:
                        membership = ref / "annotations"
                        order = read_annotation_order(ref, fingerprint)
                        (membership / (identity + ".zip")).unlink()
                        write_annotation_order(
                            ref, [i for i in order if i != identity]
                        )
                        fsync_dir(membership)
                        return
                raise KeyError(f'No Annotation found with name: "{name}"')

    def verify(self, signature_name):
        # External gpg requires filenames. Materialization is explicit and
        # retained for the duration of verification.
        with self.snapshot() as view, view.materialize() as directory:
            from copy import copy

            concrete = Archiver.__new__(Archiver)
            concrete._fmt = copy(self._fmt)
            concrete._fmt.path = directory.path
            concrete._fmt.annotations_dir = directory.path / "annotations"
            concrete._memoize_annotations = []
            return concrete.verify(signature_name)

    def metadata_paths(self):
        paths, relative = [], []
        with self.snapshot() as view:
            for node_id, members in view._node_members.items():
                prefix = (
                    "provenance/"
                    if node_id == str(self.uuid)
                    else ("provenance/artifacts/" + node_id + "/")
                )
                for member in sorted(metadata_members(members)):
                    paths.append(self.root_dir / (prefix + member))
                    relative.append(member.removeprefix("action/"))
        return [paths, relative]

    def redact_metadata(self):
        from rachis.core.annotate import Note

        records = set()
        with self.snapshot() as view:
            for node_id, members in view._node_members.items():
                records.update((node_id, n) for n in metadata_members(members))
            if not records:
                raise ValueError(
                    "Cannot redact metadata from a Result without metadata."
                )
            if records.issubset(view.whiteouts):
                raise ValueError(
                    "Cannot redact metadata from a Result with only "
                    "redacted metadata files."
                )
        self.commit_whiteouts(records)
        if self.annotations_dir is not None:
            annotation = Note(
                name="Metadata-redaction",
                text=(
                    "Redacted metadata from all Results in provenance.\n"
                    + "\n".join(f"{u}/{p}" for u, p in sorted(records))
                ),
            )
            if annotation.name not in self._annotations:
                self.add_annotation(annotation)

    def commit_whiteouts(self, records):
        commit_whiteouts([self], records)


def commit_whiteouts(archivers, records):
    """Broadcast one union/flush per editable ref, deduplicating handles."""
    archivers = list(archivers)
    groups = {}
    records = set(records)
    for archiver in archivers:
        cache = archiver.cache
        target = cache.editable_ref(archiver.ref_id)
        groups.setdefault(cache, set()).add(target)
    # Validate masks from retained packs before committing shared state.
    for archiver in archivers:
        with archiver.snapshot() as view:
            eligible = {
                (u, p)
                for u, members in view._node_members.items()
                for p in metadata_members(members)
            }
            if not records.issubset(eligible):
                raise ValueError("Whiteout targets ineligible metadata")
            for node in {u for u, _ in records}:
                selected = {p for u, p in records if u == node}
                complete = {p for u, p in eligible if u == node}
                if selected != complete:
                    raise ValueError('Unsupported partially redacted view')
    for cache, targets in groups.items():
        with cache.lock:
            for target in targets:
                ref = cache.object_path("ref", target)
                latest = read_whiteouts(ref)
                merged = latest | records
                if merged == latest:
                    continue
                staging = ref / "staging"
                staging.mkdir(exist_ok=True)
                temporary = staging / ("whiteout." + new_id())
                durable_write(temporary, whiteout_bytes(merged))
                os.replace(temporary, ref / "whiteout.jsonl")
                fsync_dir(ref)


def _restore_archiver(cache, ref_id, replay=False):
    return V2Archiver(cache, cache.forwarding_ref(ref_id), replay=replay)


def broadcast_annotation(archivers, annotation, reference_uuid=None):
    """Prepare packs privately and commit each cache's target batch."""
    groups = {}
    for archiver in archivers:
        cache = archiver.cache
        target = cache.editable_ref(archiver.ref_id)
        groups.setdefault(cache, {})[target] = archiver
    prepared = []
    try:
        for cache, targets in groups.items():
            batch = {}
            for target, archiver in targets.items():
                directory, selected, loaded = archiver._prepare_annotation(
                    annotation, reference_uuid
                )
                prepared.append(directory)
                batch[target] = (selected, loaded)
            while True:
                states = {}
                for target in batch:
                    ref = cache.object_path("ref", target)
                    states[target] = annotation_state(cache, ref)
                with cache.lock:
                    if any(
                        annotation_fingerprint(cache.object_path("ref", t))
                        != state[0]
                        for t, state in states.items()
                    ):
                        continue
                    for target, (_, loaded) in batch.items():
                        if any(
                            md["name"] == loaded.name
                            for md in states[target][1].values()
                        ):
                            raise ValueError(
                                "Duplicate name detected when attempting "
                                "to add "
                                f"Annotation with name: {loaded.name}"
                            )
                    for target, (selected, loaded) in batch.items():
                        membership = (
                            cache.object_path("ref", target) / "annotations"
                        )
                        shared_mkdir(membership, exist_ok=True)
                        temporary = membership / ("." + new_id())
                        os.link(selected, temporary)
                        ref = cache.object_path("ref", target)
                        order = read_annotation_order(ref, states[target][0])
                        os.replace(
                            temporary, membership / (str(loaded.id) + ".zip")
                        )
                        if str(loaded.id) not in order:
                            order.append(str(loaded.id))
                        write_annotation_order(ref, order)
                        fsync_dir(membership)
                    break
    finally:
        for directory in prepared:
            directory.release()
