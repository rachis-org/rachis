# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""ZIP pack materialization, checksum indexes, and cache publication."""

import errno
import hashlib
import uuid
import yaml
from functools import cached_property
import os
from pathlib import Path
import shutil
import zipfile

from ..zip import ZipFile
from ..cache_stat import ownership_stat
from ..reference import new_id, relative_path, fsync_dir, shared_mkdir
from ..util import to_checksum_format, from_checksum_format


class ZipPack:
    """A recipe for placing one literal pack ZIP at a destination.

    Parameters
    ----------
    uuid : str or uuid.UUID
        Identity of the node or annotation stored beneath '<uuid>/'.
    source : path-like, binary file-like object, or zipfile.ZipFile
        An extracted directory, a complete pack ZIP path/stream, or an open ZIP
        containing a selected subtree. Open sources are borrowed and must
        remain open while the recipe is used.
    select : iterable or None, optional
        Source ZipInfo objects or filesystem paths. None selects all entries.
        Applies to directories and open ZIP partitions. A prepartitioned
        selection avoids rescanning a QZA for each pack.
    select_prefix : str or None, optional
        Source directory to strip. None uses the directory root for extracted
        content and '<uuid>/' for ZIP content.
    whiteouts : iterable of str, optional
        Pack-relative masks already stored separately from the source bytes.
        These are reported alongside zero-byte files without rewriting an
        existing canonical pack.

    Notes
    -----
    Whiteouts are zero-byte files, without any interpretation of their names.
    Empty directories are preserved and are not whiteouts. The receiving
    archive/cache decides which whiteouts have meaning and where to retain
    them. Materialization preserves zero-byte source entries and transfers
    separately supplied masks as metadata through ``whiteouts``.

    A ZIP partition is spliced without recompression. An existing complete
    pack is hardlinked or copied unchanged. An extracted directory is zipped
    when materialized. The caller supplies a new destination path and manages
    its lifetime and any partial output on failure.
    """

    def __init__(
        self, uuid, source, *, select=None, select_prefix=None, whiteouts=()
    ):
        self.uuid = str(uuid)
        self._owner = None
        self._source = source if (
            isinstance(source, zipfile.ZipFile) or hasattr(source, 'read')
        ) else Path(source)
        self._literal = not isinstance(source, zipfile.ZipFile) and not (
            isinstance(self._source, Path) and self._source.is_dir()
        )
        self._select = tuple(select) if select is not None else None
        if select_prefix is None:
            select_prefix = self.uuid if (
                self._literal or isinstance(source, zipfile.ZipFile)
            ) else ""
        self._prefix = select_prefix.rstrip("/") + "/" if select_prefix else ""
        self._whiteouts = frozenset(whiteouts)

    def _entries(self, source):
        is_zip = isinstance(source, zipfile.ZipFile)
        selected = self._select
        if selected is None:
            selected = (
                source.infolist() if is_zip else sorted(source.rglob("*"))
            )
        for entry in selected:
            name = entry.filename if is_zip else (
                entry.relative_to(source).as_posix()
                + ("/" if entry.is_dir() else "")
            )
            if name.startswith(self._prefix) and name != self._prefix:
                yield name[len(self._prefix):], entry

    def _empty_files(self, source):
        is_zip = isinstance(source, zipfile.ZipFile)
        return frozenset(
            name for name, entry in self._entries(source)
            if not entry.is_dir() and (
                entry.file_size if is_zip else entry.stat().st_size
            ) == 0
        )

    @cached_property
    def whiteouts(self):
        """frozenset of str: zero-byte file paths and separately held masks."""
        if self._literal:
            with ZipFile(self._source) as source:
                empty = self._empty_files(source)
        else:
            empty = self._empty_files(self._source)
        return self._whiteouts | empty

    def materialize(self, destination):
        """Place this pack's literal ZIP file at a caller-provided path.

        Parameters
        ----------
        destination : path-like
            New ZIP filepath. Its parent directory must already exist.

        Returns
        -------
        pathlib.Path
            Materialized pack, with member paths rooted at '<uuid>/'.

        Notes
        -----
        Existing pack files are linked when possible; unsupported hardlinks,
        cross-filesystem transfers and exhausted link counts fall back to a
        byte-for-byte copy. Materialization neither publishes cache objects nor
        applies separately supplied whiteouts to canonical source bytes.
        """
        destination = Path(destination)
        if self._literal and isinstance(self._source, Path):
            try:
                os.link(self._source, destination)
            except OSError as error:
                if error.errno not in (
                    errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP,
                    errno.EOPNOTSUPP,
                    errno.ENOSYS,
                ):
                    raise
                shutil.copyfile(self._source, destination)
        elif self._literal:
            self._source.seek(0)
            with destination.open('wb') as output:
                shutil.copyfileobj(self._source, output, 1024 * 1024)
        else:
            with ZipFile(
                destination, "w", compression=zipfile.ZIP_DEFLATED
            ) as output:
                if isinstance(self._source, zipfile.ZipFile):
                    output.splice(
                        self._source, self.uuid + "/", select=self._select,
                        select_prefix=self._prefix,
                    )
                else:
                    for name, entry in self._entries(self._source):
                        output.write(entry, arcname=self.uuid + "/" + name)
        return destination


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


def materialize_pack(path, recipe, hashes=None, md5=None):
    """Materialize incoming content, then add cache indexes if absent.

    Complete cache packs already carry indexes and may share an inode with
    their source. Do not rewrite those ZIPs. QZA partitions and V1 trees
    create private ZIPs; hash missing values from streams, without extraction.
    """
    recipe.materialize(path)
    _index_materialized_pack(path, recipe.uuid, hashes, md5)
    return path


def _index_materialized_pack(path, identity, hashes=None, md5=None):
    hashes = {} if hashes is None else hashes
    prefix = identity + "/"
    with ZipFile(path) as source:
        if "checksums.sha512" in source.namelist():
            if ownership_stat(path).st_nlink == 1:
                os.chmod(path, 0o660)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            return path
        index = {}
        for entry in source.infolist():
            if entry.is_dir() or not entry.filename.startswith(prefix):
                continue
            name = entry.filename[len(prefix):]
            digest = hashes.get(name)
            if digest is None:
                hasher = hashlib.sha512()
                with source.open(entry) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
            index[entry.filename] = digest
    # A complete unindexed pack may have been hardlinked by its recipe. Adding
    # cache-only records must never change the incoming source inode.
    if ownership_stat(path).st_nlink > 1:
        private = path.with_name(".indexed." + new_id())
        shutil.copyfile(path, private)
        os.replace(private, path)
    with ZipFile(path, "a") as output:
        output.writestr(
            "checksums.sha512", manifest_bytes(index),
            compress_type=zipfile.ZIP_STORED,
        )
        if md5:
            output.writestr(
                "checksums.md5",
                manifest_bytes({
                    prefix + name: digest for name, digest in md5.items()
                }),
                compress_type=zipfile.ZIP_STORED,
            )
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    os.chmod(path, 0o660)
    return path


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
