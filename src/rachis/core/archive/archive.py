# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

"""Archive storage backends and their construction/publication lifecycle."""

import collections
import contextlib
import hashlib
import io
import json
import os
import pathlib
from pathlib import Path
import shutil
import uuid as _uuid
import weakref
import zipfile
from functools import cached_property
from types import SimpleNamespace

import yaml

from rachis.core.annotate import Annotation
from rachis.core.util import (
    is_uuid4, checksum_python, checksum_directory, to_checksum_format,
)
from rachis.core.zip import ZipFile
from rachis.core.reference import (
    Reference, directory_references, target_name, new_id, relative_path,
    durable_write, fsync_dir, shared_mkdir,
)
from .pack import (
    ZipPack, manifest_bytes, parse_manifest, metadata_members, pack_path,
    materialize_pack, _index_materialized_pack, publish_pack,
)
from .view import (
    LiveArchiveView, ConcreteArchiveView, _ArchiveCapture, _files,
    read_whiteouts, whiteout_bytes,
)

_VERSION_TEMPLATE = """\
QIIME 2
archive: %s
framework: %s
"""

ArchiveRecord = collections.namedtuple(
    'ArchiveRecord', ['root', 'version_fp', 'uuid', 'version',
                      'framework_version'])

ChecksumDiff = collections.namedtuple(
    'ChecksumDiff', ['added', 'removed', 'changed'])


class _Archive:
    """Abstraction layer over the archive filesystem.

    Responsible for details concerning manipulating an archive agnostic to its
    format. It is responsible for managing archive UUID, format, and framework
    versions as those are designed to be constant throughout all future
    format implementations. Breaking compatibility with that is a BIG DEAL and
    should avoided at (nearly) any cost.

    Example filesystem::

        <archive root>/
        !--- 770509e6-85f4-432c-9663-cdc04eb07db2
            |--- VERSION
            !--- <whatever format defines>

    VERSION file::

        QIIME 2
        archive: <archive version>
        framework: <framework version>

    This file is itentionally not YAML/INI/An actual format. This is to
    discourage the situation where the format changes from something like YAML
    to another format and VERSION is updated with it "for consistency".

    To emphasize, the VERSION (filepath and content) and root archive structure
    MUST NOT CHANGE. If they change, then there is no longer a consistent way
    to dispatch to an appropriate format.

    """
    VERSION_FILE = 'VERSION'

    @classmethod
    def is_archive_type(cls, filepath):
        raise NotImplementedError

    @classmethod
    def setup(cls, uuid, path, version, framework_version):
        root_dir = path
        version_fp = root_dir / cls.VERSION_FILE

        version_fp.write_text(_VERSION_TEMPLATE % (version, framework_version))

        return ArchiveRecord(root_dir, version_fp, uuid, version,
                             framework_version)

    @classmethod
    def save(cls, source, destination):
        raise NotImplementedError

    def __init__(self, path):
        self.path = path

        self.uuid = self._get_uuid()
        self.version, self.framework_version = self._get_versions()

    def _get_uuid(self):
        if not self.path.exists():
            raise TypeError("%s does not exist or is not a filepath."
                            % self.path)

        roots = set()
        for relpath in self.relative_iterdir():
            if not relpath.startswith('.'):
                roots.add(relpath)

        if len(roots) == 0:
            raise ValueError("Archive does not have a visible root directory.")
        if len(roots) > 1:
            raise ValueError("Archive has multiple root directories: %r"
                             % roots)
        uuid = roots.pop()
        if not is_uuid4(uuid):
            raise ValueError(
                "Archive root directory name %r is not a valid version 4 "
                "UUID." % uuid)
        return uuid

    def _get_versions(self):
        try:
            with self.open(self.VERSION_FILE) as fh:
                header, version_line, framework_version_line, eof = \
                    fh.read().split('\n')
            if header.strip() != 'QIIME 2':
                raise Exception()  # GOTO except Exception
            version = version_line.split(':')[1].strip()
            framework_version = framework_version_line.split(':')[1].strip()
            return version, framework_version
        except Exception:
            # TODO: make a "better" parser which isn't just a catch-all
            raise ValueError("Archive does not contain a correctly formatted"
                             " VERSION file.")

    def relative_iterdir(self, relpath='.'):
        raise NotImplementedError

    def open(self, relpath):
        raise NotImplementedError

    def mount(self, filepath):
        raise NotImplementedError

    def members(self):
        """Return archive-relative file and directory names.

        Returns
        -------
        tuple of str
            Names beneath the artifact UUID; directories end with '/'.
        """
        return tuple(self._entries)

    def open_binary(self, relpath):
        """Open an archive-relative file as a caller-owned binary stream."""
        raise NotImplementedError

    def extract_data(self, destination):
        """Extract only payload content into a caller-provided directory.

        Parameters
        ----------
        destination : path-like
            Destination data directory, without an enclosing UUID directory.

        Returns
        -------
        pathlib.Path
            Destination directory, including any explicit empty directories.
        """
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for member in self.members():
            if not member.startswith('data/') or member == 'data/':
                continue
            target = destination / member[len('data/'):]
            if member.endswith('/'):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with (
                    self.open_binary(member) as source,
                    target.open('wb') as out,
                ):
                    shutil.copyfileobj(source, out, 1024 * 1024)
        return destination

    @cached_property
    def _pack_groups(self):
        # Partition the index once, rather than rescanning for each node.
        own = []
        provenance = collections.defaultdict(list)
        annotations = collections.defaultdict(list)
        for name, entry in self._entries.items():
            if name.startswith('provenance/.cache-v2-ancestry/'):
                continue
            if name.startswith('provenance/artifacts/'):
                identity, _, member = name[
                    len('provenance/artifacts/'):
                ].partition('/')
                if member:
                    provenance[identity].append(entry)
            elif name.startswith('provenance/'):
                own.append(entry)
            elif name.startswith('annotations/'):
                identity, _, member = name[len('annotations/'):].partition('/')
                if member:
                    annotations[identity].append(entry)
        return own, provenance, annotations

    @property
    def self_provenance(self):
        """ZipPack or None: this artifact's own node, without its ancestors."""
        own, _, _ = self._pack_groups
        if own:
            return self._make_pack(str(self.uuid), 'provenance/', own)
        return None

    def iter_provenance_packs(self):
        """Yield ancestral provenance ZipPacks in UUID order.

        Yields
        ------
        ZipPack
            One node per pack, without its own transitive ancestry. Use
            ``self_provenance`` for the current artifact's separate node.
        """
        _, nodes, _ = self._pack_groups
        for identity, entries in sorted(nodes.items()):
            yield self._make_pack(
                identity, 'provenance/artifacts/' + identity + '/', entries
            )

    def iter_annotation_packs(self):
        """Yield annotation ZipPacks in UUID order.

        Yields
        ------
        ZipPack
            One annotation, including its archive-supplied checksum files.
        """
        _, _, nodes = self._pack_groups
        for identity, entries in sorted(nodes.items()):
            yield self._make_pack(
                identity, 'annotations/' + identity + '/', entries
            )

    def close(self):
        """Release resources opened by this adapter and its borrowed packs."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def root(self):
        from .view import LogicalPath
        return LogicalPath(self)

    @property
    def data_dir(self):
        return self.path / "data"

    def open_member(self, member):
        return self.open_binary(member)

    def is_file(self, member):
        return member in self.members() and not member.endswith("/")

    def is_dir(self, member):
        prefix = member.rstrip("/") + "/" if member else ""
        return any(name.startswith(prefix) for name in self.members())

    def finalize(self, *, generated=False):
        """Publish prepared contents and expose the completed archive.

        Parameters
        ----------
        generated : bool, optional
            Whether these contents were produced by an action rather than
            imported from an existing archive.

        Returns
        -------
        _Archive
            This backend, with published paths and established ownership.
        """
        raise NotImplementedError

    def read_annotations(self):
        annotations = self.root / "annotations"
        if not annotations.exists():
            return {}
        result = {}
        for path in annotations.iterdir():
            annotation = Annotation.load(path)
            result[annotation.name] = annotation
        return result

    def read_manifest(self, algorithm):
        return (self.root / ("checksums." + algorithm)).read_bytes()

    def write_manifest(self, algorithm, checksums):
        with (self.root / ("checksums." + algorithm)).open("w") as stream:
            for name, digest in checksums.items():
                stream.write(f"{digest}  {name}\n")

    @contextlib.contextmanager
    def materialize(self):
        with self.snapshot() as view, view.materialize() as directory:
            yield directory

    def _validate_annotation_support(self):
        if float(self.version) < 7:
            raise ValueError(
                'The Artifact or Visualization being used is associated '
                'with a QIIME 2 archive format of < 7.0. Annotation actions '
                'are only supported for QIIME 2 archive formats of 7.0 '
                'and above.'
            )


class _ZipArchive(_Archive):
    """A specific variant of Archive which deals with ZIP64 files."""

    @classmethod
    def is_archive_type(cls, path):
        return zipfile.is_zipfile(str(path))

    @classmethod
    def save(cls, source, destination):
        parent_dir = os.path.split(source)[0]
        with zipfile.ZipFile(str(destination), mode='w',
                             compression=zipfile.ZIP_DEFLATED,
                             allowZip64=True) as zf:
            for root, dirs, files in os.walk(str(source)):
                # Prune hidden directories from traversal. Strategy modified
                # from http://stackoverflow.com/a/13454267/3776794
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    if file.startswith('.'):
                        continue

                    abspath = pathlib.Path(root) / file
                    relpath = abspath.relative_to(parent_dir)

                    zf.write(str(abspath), arcname=cls._as_zip_path(relpath))

    def relative_iterdir(self, relpath=''):
        relpath = self._as_zip_path(relpath)
        seen = set()
        for entry in self._reader.infolist():
            name = entry.filename
            if name.startswith(relpath):
                parts = pathlib.PurePosixPath(name).parts
                if len(parts) > 0:
                    result = parts[0]
                    if result not in seen:
                        seen.add(result)
                        yield result

    def open(self, relpath):
        return io.TextIOWrapper(self.open_binary(relpath))

    def mount(self, filepath):
        # TODO: use FUSE/MacFUSE/Dokany bindings (many Python bindings are
        # outdated, we may need to take up maintenance/fork)

        # We will have already allocated filepath at this point, we check if
        # the VERSION file exists to determine whether or not we have alredy
        # written to the allocated directory. This is relevant when you try to
        # load an artifact that is already in the cache because data/<uuid>
        # will be read only, so attempting to extract there will error. We also
        # just don't need to put the data there again if it is already there
        if not os.path.exists(filepath / 'VERSION'):
            self.extract(filepath)

        root = filepath
        return ArchiveRecord(root, root / self.VERSION_FILE,
                             self.uuid, self.version, self.framework_version)

    def extract(self, filepath):
        filepath = pathlib.Path(filepath)
        assert os.path.basename(filepath) == str(self.uuid)
        with zipfile.ZipFile(str(self.path), mode='r') as zf:
            for name in zf.namelist():
                if name.startswith(str(self.uuid)):
                    # extract removes `..` components, so as long as we extract
                    # into `filepath`, the path won't go backwards.
                    zf.extract(name, path=str(filepath.parent))

        return filepath

    @cached_property
    def _reader(self):
        return ZipFile(self.path)

    @cached_property
    def _entries(self):
        prefix = str(self.uuid) + '/'
        return {
            entry.filename[len(prefix):]: entry
            for entry in self._reader.infolist()
            if entry.filename.startswith(prefix) and entry.filename != prefix
        }

    def open_binary(self, relpath):
        return self._reader.open(self._entries[str(relpath)])

    def _make_pack(self, identity, prefix, entries):
        pack = ZipPack(
            identity, self._reader, select=entries,
            select_prefix=str(self.uuid) + '/' + prefix,
        )
        pack._owner = self
        return pack

    def close(self):
        if '_reader' in self.__dict__:
            self._reader.close()

    @classmethod
    def _as_zip_path(self, path):
        path = str(pathlib.PurePosixPath(path))
        # zip files don't work well with '.' which is the identity of a Path
        # obj, so just convert to empty string which is basically the identity
        # of a zip's entry
        if path == '.':
            path = ''
        return path


class _NoOpArchive(_Archive):
    """For dealing with unzipped artifacts"""

    @classmethod
    def is_archive_type(cls, path):
        return os.path.isdir(str(path))

    def _get_uuid(self):
        """If we are using a _NoOpArchive we are a data element in a pool
        meaning we are unzipped and our name is our uuid
        """
        return os.path.basename(self.path)

    def relative_iterdir(self, relpath=''):
        seen = set()
        for name in os.listdir(str(self.path)):
            if name.startswith(relpath) and name not in seen:
                seen.add(name)
                yield name

    def open(self, relpath):
        return open(os.path.join(self.path, relpath))

    def mount(self, path):
        root = path
        return ArchiveRecord(root, root / self.VERSION_FILE,
                             self.uuid, self.version, self.framework_version)

    @cached_property
    def _entries(self):
        root = Path(self.path)
        return {
            path.relative_to(root).as_posix()
            + ('/' if path.is_dir() else ''): path
            for path in sorted(root.rglob('*'))
        }

    def open_binary(self, relpath):
        return (Path(self.path) / relpath).open('rb')

    def _make_pack(self, identity, prefix, entries):
        pack = ZipPack(
            identity, self.path, select=entries, select_prefix=prefix
        )
        pack._owner = self
        return pack

    def __init__(self, path, cache=None, process_alias=None):
        super().__init__(Path(path))
        self.cache = cache
        self.process_alias = process_alias
        if cache is not None:
            if process_alias is None:
                self.process_alias = cache._alias(str(self.uuid))
            self._destructor = weakref.finalize(
                self, _release_noop_archive, cache, self.process_alias
            )

    @classmethod
    def create(cls, cache, identity=None):
        archive = cls.__new__(cls)
        archive.cache = cache
        archive.uuid = str(identity or _uuid.uuid4())
        archive.path = cache.process_pool._allocate(archive.uuid)
        archive.process_alias = archive.path.parent.name
        archive._destructor = weakref.finalize(
            archive, _release_noop_archive, cache, archive.process_alias
        )
        return archive

    def import_from(self, source):
        if isinstance(source, _NoOpArchive):
            shutil.copytree(source.path, self.path, dirs_exist_ok=True)
        else:
            source.mount(self.path)
        return self

    def finalize(self, *, generated=False):
        self.version, self.framework_version = self._get_versions()
        self.process_alias, self.path = self.cache._rename_to_data(
            self.uuid, self.path, replay=not generated
        )
        self.__dict__.pop('_entries', None)
        self.__dict__.pop('_pack_groups', None)
        return self

    @property
    def root(self):
        return self.path

    def reopen(self):
        return type(self)(self.path, self.cache)

    def snapshot(self):
        return ConcreteArchiveView(self)

    @contextlib.contextmanager
    def materialize(self):
        yield SimpleNamespace(path=self.path)

    def save(self, destination):
        _ZipArchive.save(self.path, destination)

    def close(self):
        if hasattr(self, "_destructor"):
            self._destructor()

    def __reduce__(self):
        return (type(self), (self.path, self.cache))

    def add_annotation(self, annotation, reference_uuid=None):
        """
        Add an Annotation onto a Result object.
        All Result-associated parameters are passed into the sub-class's
        `write` method, while the Annotation instance handles everything else

        Parameters
        ----------
        annotation
            An instantiated Annotation subclass (Note, Signature, etc).

        Raises
        ------
        ValueError
            If the Annotation name matches an existing Annotation name
            attached to the Result in question.

        Notes
        -----
            In Archive Format 7.0, `referenced_result_uuid` is set to
            the same value as `root_result_uuid`, but this will change
            in future versions to allow for Annotations that may reference
            a different Result than the one they are attached to.

        """
        self._validate_annotation_support()

        # Guard to ensure Annotation names are unique per Result object
        if annotation.name in self.read_annotations():
            raise ValueError(
                'Duplicate name detected when attempting to add '
                f'Annotation with name: "{annotation.name}"\n'
                'Annotation names must be unique within each Result '
                'they are attached to.'
            )

        if not reference_uuid:
            annotation._write(annotations_dir=(self.path / "annotations"),
                            root_result_uuid=str(self.uuid),
                            referenced_result_uuid=str(self.uuid))
        else:
            annotation._write(annotations_dir=(self.path / "annotations"),
                            root_result_uuid=str(self.uuid),
                            referenced_result_uuid=str(reference_uuid))

        # now calculate checksums for all files within the newly minted
        # annotation subdir
        # TODO: think about moving these into annotation._write after 7.1
        annotation_dir = Path((self.path / "annotations")) / str(annotation.id)
        checksum_ext = "sha512"
        manifest = "checksums.sha512"

        checksums = checksum_directory(annotation_dir,
                                            checksum_type=checksum_ext)

        with (annotation_dir / manifest).open('w') as fh:
            for item in checksums.items():
                fh.write(to_checksum_format(*item))
                fh.write('\n')

        # this ensures additional attrs on Signature (signer name/email)
        # are present when running Archiver.verify
        Annotation.load(annotation_dir)

    def remove_annotation(self, name):
        """
        Remove an Annotation given by `name` from the Result object.

        Parameters
        ----------
        name : str
            The name of the Annotation to be removed.

        Raises
        ------
        KeyError
            If no Annotation with the specified name is found.

        ValueError
            If the corresponding annotation directory cannot be located.

        """
        self._validate_annotation_support()
        annotations = self.read_annotations()

        if name not in annotations:
            raise KeyError(f'No Annotation found with name: "{name}"')

        annotations_dir = (self.path / "annotations")
        annotation_disk_dir = os.path.join(annotations_dir,
                                           str(annotations[name].id))

        if not os.path.exists(annotation_disk_dir):
            raise ValueError('Unable to locate on-disk directory '
                             f'for Annotation with name: "{name}"')

        shutil.rmtree(annotation_disk_dir)
        del annotations[name]

    def validate_checksums(self, algorithm):
        observed = {
            name: digest for name, digest in
            checksum_directory(self.path, checksum_type=algorithm).items()
            if name != "checksums." + algorithm
            and not name.startswith("annotations/")
        }
        expected = parse_manifest(self.read_manifest(algorithm))
        return checksum_diff(expected, observed)

    def redact_metadata(self, paths, algorithm):
        if not paths:
            raise ValueError(
                'Cannot redact metadata from a Result without metadata.'
            )
        if all(path.stat().st_size == 0 for path in paths):
            raise ValueError(
                'Cannot redact metadata from a Result with only '
                'redacted metadata files.'
            )
        checksums = parse_manifest(self.read_manifest(algorithm))
        for path in paths:
            path.write_bytes(b"")
            name = path.relative_to(self.path).as_posix()
            if name in checksums:
                checksums[name] = checksum_python(path, algorithm)
        self.write_manifest(algorithm, checksums)


class _CacheV2Archive(_Archive):
    """Expose split cache storage through the same archive and pack contract.

    Pack recipes borrow individual streams, with mutable whiteouts supplied
    separately. The adapter retains its RefDir through a lifetime reference;
    no archive snapshot or additional scope is allocated. Explicit close
    releases the reference and borrowed streams.

    Parameters
    ----------
    cache : CacheV2
        Cache containing the split archive storage.
    ref_id : str
        Identity of the archive RefDir to read.
    scope_owned : bool, optional
        Use the existing scope's ownership for an Artifact backend. Independent
        adapters acquire their own lifetime reference by default.
    """

    def __init__(self, cache, ref_id, *, scope_owned=False):
        self._initialize(cache, ref_id, scope_owned=scope_owned)
        self._activate()

    def members(self):
        return tuple(self._view.members(include_directories=True))

    def open_binary(self, relpath):
        return self._view.open_member(str(relpath))

    def relative_iterdir(self, relpath=''):
        return iter(sorted({
            name.split('/', 1)[0] for name in self.members()
            if name.startswith(str(relpath))
        }))

    def open(self, relpath):
        return io.TextIOWrapper(self.open_binary(relpath))

    def _pack(self, path, *, provenance=False):
        cache = self.cache
        identity = path.stem
        if provenance:
            cache.refresh_provenance(self.uuid, node_id=identity)
        with cache.lock:
            source = self._resources.enter_context(path.open('rb'))
            masks = {
                name for node, name in read_whiteouts(self._ref)
                if node == identity
            } if provenance else ()
        pack = ZipPack(identity, source, whiteouts=masks)
        pack._owner = self
        return pack

    @property
    def self_provenance(self):
        if self.version == '0':
            return None
        path = self._artifact / 'provenance' / (self.uuid + '.zip')
        return self._pack(path, provenance=True) if path.exists() else None

    def iter_provenance_packs(self):
        if self.version != '0':
            for path in sorted((self._artifact / 'provenance').glob('*.zip')):
                if path.stem != self.uuid:
                    yield self._pack(path, provenance=True)

    def iter_annotation_packs(self):
        for path in sorted((self._ref / 'annotations').glob('*.zip')):
            yield self._pack(path)

    def mount(self, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for member in self.members():
            target = destination / member
            if member.endswith('/'):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with (
                    self.open_binary(member) as source,
                    target.open('wb') as output,
                ):
                    shutil.copyfileobj(source, output, 1024 * 1024)
        return ArchiveRecord(
            destination, destination / self.VERSION_FILE, self.uuid,
            self.version, self.framework_version,
        )

    def close(self):
        workspace = getattr(self, '_workspace', None)
        if workspace is not None:
            workspace.release()
        self._destructor()


    def _initialize(self, cache, ref_id, *, scope_owned=False):
        self.cache = cache
        self.ref_id = ref_id
        self.process_alias = ref_id
        self._ref = cache.object_path("archives", ref_id)
        self._resources = contextlib.ExitStack()
        if not scope_owned:
            self._resources.callback(
                cache.unreference, cache.root_scope.adopt(ref_id)
            )
        self._resources.callback(cache._deallocate, ref_id)
        self._destructor = weakref.finalize(self, self._resources.close)

    def _activate(self):
        self._view = LiveArchiveView(self.cache, self.ref_id)
        self.path = self._view.root
        self.uuid = self._view.uuid
        self._artifact = self.cache.attachment(self.ref_id)[1]
        self.version, self.framework_version = self._get_versions()

    @classmethod
    def create(cls, cache, identity=None, *, ref_id=None):
        archive = cls.__new__(cls)
        if ref_id is None:
            workspace = cache.reserve_result(reserved_uuid=identity)
            ref_id = workspace.ref_id
            archive._workspace = workspace
        archive._initialize(cache, ref_id, scope_owned=True)
        reserved = json.loads(
            (archive._ref / "resumption.json").read_text()
        )["uuid"]
        archive.uuid = reserved
        archive.path = archive._ref / "staging" / reserved
        archive.path.mkdir(parents=True, exist_ok=True)
        return archive

    def import_from(self, source):
        # ZipPack recipes borrow the input until finalization completes.
        self.source = source
        if isinstance(source, _ZipArchive):
            for entry in source._reader.infolist():
                relative_path(entry.filename)
                if entry.external_attr >> 16 & 0o170000 == 0o120000:
                    raise ValueError("Archive symlinks are unsupported")
        source.extract_data(self.path / "data")
        for member in source.members():
            if "/" not in member:
                with source.open_binary(member) as stream:
                    durable_write(self.path / member, stream.read())
        return self

    def finalize(self, *, generated=False):
        """Install prepared content and complete this RefDir's attachment."""
        self.generated = generated
        self.provenance = self.path / "provenance"
        self.inherited = self.provenance / ".cache-v2-ancestry"
        self.whiteouts = set()
        self.selected_packs = {}
        self.annotations = {}
        self._read_source()
        self._prepare_payload()
        self._prepare_provenance()
        self._prepare_annotations()
        self._build_candidate()
        self._publish_artifact()
        self._publish_view()
        self._cleanup_construction()
        self._activate()
        del self.source, self.hashes, self.manifests
        del self.selected_packs, self.annotations, self.whiteouts
        del self.provenance, self.inherited, self.stage
        del self.pending, self.candidate, self.artifact
        del self.identity, self.hit, self.generated
        if hasattr(self, '_workspace'):
            del self._workspace
        return self

    def _read_source(self):
        if self.generated:
            self._remove_hidden_payload_files()
        if list(self._ref.glob("immutable.archives.*.iref")):
            raise ValueError("Archive is already finalized")
        self.source = getattr(self, "source", None) or _NoOpArchive(
            self.path
        )
        metadata = yaml.safe_load((self.path / "metadata.yaml").read_text())
        self.identity = metadata["uuid"]
        if self.identity != self.uuid:
            raise ValueError("Finalization must preserve the reserved UUID")
        self.stage = self._ref / "staging"
        self.hashes = {}
        self.manifests = {}
        for algorithm in ("md5", "sha512"):
            path = self.path / ("checksums." + algorithm)
            if path.exists():
                self.manifests[algorithm] = path.read_bytes()
                self.hashes[algorithm] = parse_manifest(path.read_bytes())

    @property
    def root(self):
        return self.path

    @property
    def data_dir(self):
        return self._artifact / "data"

    def reopen(self):
        return type(self)(self.cache, self.ref_id)

    def snapshot(self):
        return _ArchiveCapture(self)

    def read_manifest(self, algorithm):
        with self.snapshot() as view:
            data = view.manifest(algorithm)
        if data is None:
            raise FileNotFoundError("checksums." + algorithm)
        return data

    def write_manifest(self, algorithm, checksums):
        raise TypeError(
            "V2 manifests are derived from the logical archive view"
        )

    def save(self, filepath):
        with (
            self.snapshot() as view,
            ZipFile(filepath, "w", compression=zipfile.ZIP_DEFLATED) as output,
        ):
            root = view.uuid + '/'
            # Packed members are spliced below. Other sources contain root
            # metadata, manifests, payload files, and empty whiteouts.
            for member, source in view.sources.items():
                if isinstance(source, bytes):
                    if member in ("checksums.sha512", "checksums.md5"):
                        source = view.manifest(member.split(".", 1)[1])
                    output.writestr(root + member, source)
                elif isinstance(source, Path):
                    output.write(source, arcname=root + member)
            for path in view.data_dir.rglob('*'):
                if path.is_dir():
                    output.write(
                        path, arcname=root + 'data/'
                        + path.relative_to(view.data_dir).as_posix() + '/',
                    )
            for category, identity, source in view.packs:
                if category == 'annotations':
                    prefix = 'annotations/' + identity + '/'
                elif identity == view.uuid:
                    prefix = 'provenance/'
                else:
                    prefix = 'provenance/artifacts/' + identity + '/'
                select_prefix = identity + '/'
                entries = [
                    info for info in source.infolist()
                    if info.filename.startswith(select_prefix)
                    and (
                        category != 'provenance'
                        or (identity, info.filename[len(select_prefix):])
                        not in view.whiteouts
                    )
                ]
                output.splice(
                    source, root + prefix, select=entries,
                    select_prefix=select_prefix,
                )

    def read_annotations(self):
        from rachis.core.annotate import Annotation

        result = {}
        with contextlib.ExitStack() as resources:
            with self.cache.lock:
                files = [
                    (p.stem, resources.enter_context(p.open("rb")))
                    for p in (self._ref / "annotations").glob("*.zip")
                ]
            for identity, file in files:
                with zipfile.ZipFile(file) as pack:
                    annotation = Annotation.load(
                        zipfile.Path(pack, at=identity + "/")
                    )
                annotation._filepath = self.root / "annotations" / identity
                result[annotation.name] = annotation
        return result

    def _metadata_targets(self):
        """Read provenance action schemas without indexing the archive."""
        targets = set()
        self.cache.refresh_provenance(self.uuid)
        with contextlib.ExitStack() as resources:
            with self.cache.lock:
                files = [
                    (p.stem, resources.enter_context(p.open("rb")))
                    for p in (self._artifact / "provenance").glob("*.zip")
                ]
            for identity, file in files:
                with zipfile.ZipFile(file) as pack:
                    name = identity + "/action/action.yaml"
                    if name in pack.NameToInfo:
                        targets.update(
                            (identity, member) for member in metadata_members(
                                {"action/action.yaml": pack.read(name)}
                            )
                        )
        return targets

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
            n: checksum_python(p, "sha512")
            for n, p in _files(node).items()
        }
        durable_write(node / "checksums.sha512", manifest_bytes(digests))
        pack = directory.path / "pack.zip"
        materialize_pack(pack, ZipPack(annotation.id, node), hashes=digests)
        selected = publish_pack(
            self.cache, "annotations", str(annotation.id), pack
        )
        # Read attachment name outside LOCK; revalidate all committed names
        # inside the transaction using metadata alone.
        loaded = Annotation.load(node)
        return directory, selected, loaded

    def remove_annotation(self, name):
        self._validate_annotation_support()
        ref = self.cache.object_path("archives", self.ref_id)
        while True:
            fingerprint, metadata = annotation_state(self.cache, ref)
            with self.cache.lock:
                if annotation_fingerprint(ref) != fingerprint:
                    continue
                for identity, md in metadata.items():
                    if md["name"] == name:
                        membership = ref / "annotations"
                        (membership / (identity + ".zip")).unlink()
                        self.cache.schedule("annotation_pack", identity)
                        fsync_dir(membership)
                        return
                raise KeyError(f'No Annotation found with name: "{name}"')

    def validate_checksums(self, algorithm):
        with self.snapshot() as view:
            if algorithm not in view.original:
                raise FileNotFoundError(("checksums." + algorithm))
            expected = parse_manifest(view.original[algorithm])
            empty = hashlib.new(algorithm, b"").hexdigest()
            # A redacted first import may later gain original metadata through
            # monotonic pack enrichment. Its native root manifest stays intact.
            for member, digest in expected.items():
                if (member.startswith("provenance/") and digest == empty
                        and isinstance(view.sources.get(member), tuple)):
                    expected[member] = view.digest(member, algorithm)
            for node_id, member in view.whiteouts:
                prefix = (
                    "provenance/"
                    if node_id == str(self.uuid)
                    else "provenance/artifacts/" + node_id + "/"
                )
                name = prefix + member
                if name in expected:
                    expected[name] = empty
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

    def redact_metadata(self, paths, algorithm):
        records = self._metadata_targets()
        if not records:
            raise ValueError(
                'Cannot redact metadata from a Result without metadata.'
            )
        with self.cache.lock:
            if records.issubset(read_whiteouts(self._ref)):
                raise ValueError(
                    'Cannot redact metadata from a Result with only '
                    'redacted metadata files.'
                )
        commit_whiteouts([self], records)

    def __reduce__(self):
        # Establish parent ownership before the worker retires its roots.
        owner = getattr(self.cache, "_handoff_scope", self.cache.scope)
        owner.adopt(self.ref_id)
        return (_restore_cache_archive, (self.cache, self.ref_id))

    def _remove_hidden_payload_files(self):
        # Generated archives exclude hidden plugin outputs before publication.
        for parent, dirs, files in os.walk(self.path / "data", topdown=False):
            for name in files:
                if name.startswith("."):
                    (Path(parent) / name).unlink()
            for name in dirs:
                if name.startswith("."):
                    shutil.rmtree(Path(parent) / name)

    def _prepare_payload(self):
        self.artifact = self.cache.object_path("artifact", self.identity)
        self.pending = self._ref / (
            ".pending." + target_name(self.cache.path, self.artifact / ".iref")
        )
        with self.cache.lock:
            self.hit = self.artifact.exists()
            if self.hit and not self.pending.exists():
                os.link(self.artifact / ".iref", self.pending)
                fsync_dir(self._ref)

    def _prepare_provenance(self):
        # Prepare every node before exposing any catalogue revisions.
        prepared = []
        own = self.source.self_provenance
        if own is not None:
            prepared.append(self._prepare_provenance_node(own, "provenance/"))
        for recipe in self.source.iter_provenance_packs():
            prefix = "provenance/artifacts/" + recipe.uuid + "/"
            prepared.append(self._prepare_provenance_node(recipe, prefix))
        for node_id, pack in prepared:
            self.selected_packs[node_id] = publish_pack(
                self.cache, "provenance", node_id, pack
            )
        self._retain_inherited_provenance()

    def _prepare_provenance_node(self, recipe, prefix):
        node_id = recipe.uuid
        pack = self.stage / (node_id + ".zip")
        recipe.materialize(pack)
        with ZipFile(pack) as source:
            files = {
                item.filename[len(node_id) + 1:]: item
                for item in source.infolist()
                if not item.is_dir()
                and item.filename.startswith(node_id + "/")
            }
            eligible = metadata_members({
                name: lambda item=item: source.read(item)
                for name, item in files.items()
            })
        empty = eligible & files.keys() & recipe.whiteouts
        if empty and empty != eligible:
            raise ValueError("Unsupported partially redacted provenance node")
        self.whiteouts.update((node_id, n) for n in empty)
        # The recipe has already materialized this private path. Index it using
        # the same helper without linking/copying its source a second time.
        _index_materialized_pack(
            pack, node_id,
            hashes=self._member_hashes(files, prefix, "sha512"),
            md5=self._member_hashes(files, prefix, "md5"),
        )
        return node_id, pack

    def _member_hashes(self, files, prefix, algorithm):
        hashes = self.hashes.get(algorithm, {})
        return {
            name: hashes[prefix + name]
            for name in files
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
        for recipe in self.source.iter_annotation_packs():
            pack = self.stage / ("annotation." + recipe.uuid + ".zip")
            materialize_pack(pack, recipe)
            self.annotations[recipe.uuid] = publish_pack(
                self.cache, "annotations", recipe.uuid, pack
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
        shutil.copytree(self.path / "data", self.candidate / "data")
        # Establish group permissions on private payload copies before
        # publication, leaving shared pack inodes and source files untouched.
        for parent, dirs, files in os.walk(self.candidate / "data"):
            os.chmod(parent, 0o2770)
            for name in files:
                os.chmod(Path(parent) / name, 0o660)
        for file in self.path.iterdir():
            if file.is_file() and not file.name.startswith("checksums."):
                durable_write(self.candidate / file.name, file.read_bytes())
        for algorithm, data in self.manifests.items():
            durable_write(self.candidate / ("checksums." + algorithm), data)
        if self.selected_packs:
            shared_mkdir(self.candidate / "provenance")
            for node_id, pack in self.selected_packs.items():
                os.link(
                    pack, self.candidate / "provenance" / (node_id + ".zip")
                )
        from ..cache_v2 import flush_tree

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
                fsync_dir(self._ref)
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
            fsync_dir(self._ref)

    def _publish_view(self):
        if self.whiteouts:
            durable_write(
                self._ref / "whiteout.jsonl", whiteout_bytes(self.whiteouts)
            )
        if self.annotations:
            membership = self._ref / "annotations"
            shared_mkdir(membership, exist_ok=True)
            for node_id, pack in self.annotations.items():
                Reference.link(
                    self.cache.path, membership, pack, name=node_id + ".zip"
                )
            fsync_dir(membership)
        with self.cache.lock:
            self.pending.rename(
                self._ref
                / target_name(self.cache.path, self.artifact / ".iref")
            )
            fsync_dir(self._ref)

    def _cleanup_construction(self):
        # Retire construction only after the attachment commit is durable.
        for reference in directory_references(self.cache.path, self._ref):
            if (
                reference.target.relative_to(self.cache.path).parts[0]
                == "mutable"
            ):
                self.cache.unreference(reference)
        (self._ref / "resumption.json").unlink(missing_ok=True)
        for name in ("staging", "cells"):
            tree = self._ref / name
            if tree.exists():
                with self.cache.lock:
                    retired = self.cache._retire_root(tree)
                if retired:
                    self.cache._delete_garbage(retired)


class ArchiveCheck(_Archive):
    """Used by the Jupyter handlers"""

    # TODO: make this part of the archiver API at some point
    def open(self, relpath):
        path = (self.path if hasattr(self.path, 'open')
                else pathlib.Path(self.path))
        return (path / relpath).open()

    def relative_iterdir(self, relpath='.'):
        path = (self.path if hasattr(self.path, 'iterdir')
                else pathlib.Path(self.path))
        for p in path.iterdir():
            yield str(p.relative_to(path))

    def _get_uuid(self):
        return (self.path.name if hasattr(self.path, 'name')
                else os.path.basename(self.path))




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


def commit_whiteouts(archives, records):
    """Broadcast one union/flush per editable RefDir, deduplicating handles."""
    archives = list(archives)
    groups = {}
    records = set(records)
    for archive in archives:
        cache = archive.cache
        target = archive.ref_id
        groups.setdefault(cache, set()).add(target)
    # Read only action schemas, then merge masks into the latest shared state.
    for archive in archives:
        eligible = getattr(archive, "_storage", archive)._metadata_targets()
        if not records.issubset(eligible):
            raise ValueError("Whiteout targets ineligible metadata")
        for node in {u for u, _ in records}:
            selected = {p for u, p in records if u == node}
            complete = {p for u, p in eligible if u == node}
            if selected != complete:
                raise ValueError("Unsupported partially redacted view")
    for cache, targets in groups.items():
        with cache.lock:
            for target in targets:
                ref = cache.object_path("archives", target)
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


def broadcast_annotation(archives, annotation, reference_uuid=None):
    """Prepare packs privately and commit each cache's target batch."""
    groups = {}
    for archive in archives:
        cache = archive.cache
        target = archive.ref_id
        groups.setdefault(cache, {})[target] = archive
    prepared = []
    try:
        for cache, targets in groups.items():
            batch = {}
            for target, archive in targets.items():
                directory, selected, loaded = archive._prepare_annotation(
                    annotation, reference_uuid
                )
                prepared.append(directory)
                batch[target] = (selected, loaded)
            while True:
                states = {}
                for target in batch:
                    ref = cache.object_path("archives", target)
                    states[target] = annotation_state(cache, ref)
                with cache.lock:
                    if any(
                        annotation_fingerprint(
                            cache.object_path("archives", t)
                        )
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
                            cache.object_path("archives", target)
                            / "annotations"
                        )
                        shared_mkdir(membership, exist_ok=True)
                        temporary = membership / ("." + new_id())
                        os.link(selected, temporary)
                        ref = cache.object_path("archives", target)
                        os.replace(
                            temporary, membership / (str(loaded.id) + ".zip")
                        )
                        fsync_dir(membership)
                    break
    finally:
        for directory in prepared:
            directory.release()


def _restore_cache_archive(cache, ref_id):
    cache.scope.adopt(ref_id)
    return _CacheV2Archive(cache, ref_id, scope_owned=True)


def _release_noop_archive(cache, process_alias):
    # Before publication the alias is a private construction directory. After
    # publication it is a symlink and uses the ordinary V1 deallocation path.
    target = cache.process_pool.path / process_alias
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        cache._deallocate(process_alias)


def checksum_diff(expected, observed):
    return ChecksumDiff(
        {n: observed[n] for n in observed.keys() - expected.keys()},
        {n: expected[n] for n in expected.keys() - observed.keys()},
        {n: (expected[n], observed[n])
         for n in expected.keys() & observed.keys()
         if expected[n] != observed[n]},
    )
