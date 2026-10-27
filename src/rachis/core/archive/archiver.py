# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import uuid as _uuid
import pathlib
import importlib
import os
import re
import subprocess

import rachis
from rachis.core.cite import Citations

from rachis.core.util import (
    sha512_file_hex, gpg_find_key, normalize_fingerprint,
    unix_gpg_terminal_helper
)
from rachis.core.archive.format.v0 import ArchiveFormat
from rachis.core.annotate import Note, ANNOTATION_TYPE_DICT

from .archive import (
    _Archive, _ZipArchive, _NoOpArchive, ArchiveRecord, ChecksumDiff,
    _CacheV2Archive as _CacheV2Archive, ArchiveCheck as ArchiveCheck,
)
from .pack import parse_manifest, metadata_members

class Archiver:
    """Interpret an archive format over a published storage backend."""

    CURRENT_FORMAT_VERSION = '7.1'
    _FORMAT_REGISTRY = {
        # NOTE: add more archive formats as things change
        '0': 'rachis.core.archive.format.v0:ArchiveFormat',
        '1': 'rachis.core.archive.format.v1:ArchiveFormat',
        '2': 'rachis.core.archive.format.v2:ArchiveFormat',
        '3': 'rachis.core.archive.format.v3:ArchiveFormat',
        '4': 'rachis.core.archive.format.v4:ArchiveFormat',
        '5': 'rachis.core.archive.format.v5:ArchiveFormat',
        '6': 'rachis.core.archive.format.v6:ArchiveFormat',
        '7.0': 'rachis.core.archive.format.v7_0:ArchiveFormat',
        '7.1': 'rachis.core.archive.format.v7_1:ArchiveFormat'
    }

    @classmethod
    def get_format_class(cls, version) -> ArchiveFormat | None:
        if '.' in version:
            major, minor = version.split('.')
            minor = int(minor)

            for minor_version in range(minor, -1, -1):
                ver = f'{major}.{minor_version}'
                if ver in cls._FORMAT_REGISTRY:
                    imp, fmt_cls = cls._FORMAT_REGISTRY[ver].split(':')
                    return getattr(importlib.import_module(imp), fmt_cls)
            # explicitly handle when no version match is found
            else:
                return None
        else:
            try:
                imp, fmt_cls = cls._FORMAT_REGISTRY[version].split(':')
            except KeyError:
                return None
            return getattr(importlib.import_module(imp), fmt_cls)

    @classmethod
    def get_archive(cls, filepath) -> _Archive:
        if isinstance(filepath, _Archive):
            return filepath
        filepath = pathlib.Path(filepath)
        if not filepath.exists():
            raise ValueError("%s does not exist." % filepath)

        if _ZipArchive.is_archive_type(filepath):
            archive = _ZipArchive(filepath)
        elif _NoOpArchive.is_archive_type(filepath):
            archive = _NoOpArchive(filepath)
        else:
            raise ValueError("%s is not a QIIME archive." % filepath)

        return archive

    @classmethod
    def _futuristic_archive_error(cls, filepath, archive: _Archive):
        raise ValueError("%s was created by 'QIIME %s'. The currently"
                         " installed framework cannot interpret archive"
                         " version %r."
                         % (filepath, archive.framework_version,
                            archive.version))

    @classmethod
    def peek(cls, filepath):
        archive = cls.get_archive(filepath)
        Format = cls.get_format_class(archive.version)
        if Format is None:
            cls._futuristic_archive_error(filepath, archive)
        # NOTE: in the future, we may want to manipulate the results so that
        # older formats provide the "new" API even if they don't support it.
        # e.g. a new format has a new property that peek should describe. We
        # add some compatability code here to return a default for that
        # property on older formats.
        return Format.load_metadata(archive)

    @classmethod
    def extract(cls, filepath, dest):
        archive = cls.get_archive(filepath)

        if isinstance(archive, _NoOpArchive):
            raise ValueError('Can not extract archive of type _NoOpArchive')

        dest = os.path.join(dest, str(archive.uuid))
        os.makedirs(dest)
        # Format really doesn't matter, the archive knows how to extract so
        # that is sufficient, furthermore it would suck if something was wrong
        # with an archive's format and extract failed to actually extract.
        return str(archive.extract(dest))

    @classmethod
    def load(cls, filepath, *args, replay=False, cache=None):
        from rachis.core.cache import get_cache
        cache = cache or get_cache()
        source = cls.get_archive(filepath)
        owned = not isinstance(filepath, _Archive)
        archive = None
        try:
            Format = cls.get_format_class(source.version)
            if Format is None:
                cls._futuristic_archive_error(filepath, source)
            Format(ArchiveRecord(
                source.root, source.root / source.VERSION_FILE,
                source.uuid, source.version, source.framework_version,
            ), replay=replay)
            archive = cache.create_archive(source.uuid)
            archive.import_from(source)
            archive.finalize()
            return cls.from_archive(archive, replay=replay)
        except BaseException:
            if archive is not None:
                archive.close()
            cache.garbage_collection()
            raise
        finally:
            if owned:
                source.close()

    @classmethod
    def load_raw(cls, filepath, cache, *args, replay=False):
        return cls.from_archive(
            _NoOpArchive(filepath, cache), replay=replay
        )

    @classmethod
    def from_data(cls, type, format, data_initializer, provenance_capture):
        from rachis.core.cache import get_cache
        cache = get_cache()
        archive = cache.create_archive()
        try:
            for ref_id, source_cache in getattr(
                provenance_capture, "_cache_dependencies", {}
            ).items():
                if source_cache is cache:
                    cache.refdir(archive.ref_id, "archives").reference(
                        cache.refdir(ref_id, "archives")
                    )
            record = _Archive.setup(
                _uuid.UUID(archive.uuid), archive.path,
                cls.CURRENT_FORMAT_VERSION, rachis.__version__,
            )
            Format = cls.get_format_class(cls.CURRENT_FORMAT_VERSION)
            Format.write(record, type, format, data_initializer,
                         provenance_capture)
            archive.finalize(generated=True)
            return cls.from_archive(archive)
        except BaseException:
            archive.close()
            cache.garbage_collection()
            raise

    def __init__(self, archive, *, replay=False):
        self._storage = archive
        self._replay = replay
        Format = self.get_format_class(archive.version)
        if Format is None:
            self._futuristic_archive_error(archive.path, archive)
        record = ArchiveRecord(
            archive.root, archive.root / archive.VERSION_FILE,
            _uuid.UUID(str(archive.uuid)), archive.version,
            archive.framework_version,
        )
        self._fmt = Format(record, replay=replay)
        self._fmt.data_dir = archive.data_dir

    @property
    def path(self):
        return self._fmt.path

    @property
    def process_alias(self):
        return self._storage.process_alias

    @property
    def _cache(self):
        return self._storage.cache

    @property
    def _destructor(self):
        return self._storage._destructor

    @property
    def uuid(self):
        return self._fmt.uuid

    @property
    def type(self):
        return self._fmt.type

    @property
    def format(self):
        return self._fmt.format

    @property
    def archive_version(self):
        return self._fmt.version

    @property
    def data_dir(self):
        return self._storage.data_dir

    @property
    def root_dir(self):
        return self._fmt.path

    @property
    def provenance_dir(self):
        return getattr(self._fmt, 'provenance_dir', None)

    @property
    def annotations_dir(self):
        return getattr(self._fmt, 'annotations_dir', None)

    @property
    def citations(self):
        if self.provenance_dir is None:
            return Citations()
        return getattr(self._fmt, "citations", Citations())

    def snapshot(self):
        return self._storage.snapshot()

    def _archive(self):
        return self._storage.reopen()

    @property
    def self_provenance(self):
        """ZipPack or None: this result's own provenance, without ancestors."""
        return self._archive().self_provenance

    def iter_provenance_packs(self):
        """Yield ancestral packs from the backing archive adapter.

        Yields
        ------
        ZipPack
            A recipe for one ancestral node. The current result's own node is
            available separately through ``self_provenance``.
        """
        yield from self._archive().iter_provenance_packs()

    def iter_annotation_packs(self):
        """Yield annotation packs from the backing archive adapter.

        Yields
        ------
        ZipPack
            A recipe for one annotation, including native checksum files.
        """
        yield from self._archive().iter_annotation_packs()

    def save(self, filepath):
        self._storage.save(filepath)

    def get_checksums(self):
        return parse_manifest(
            self._storage.read_manifest(self._fmt.CHECKSUM_TYPE)
        )

    @property
    def _annotations(self):
        return self._storage.read_annotations()


    def _validate_annotation_support(self):
        # Checks for the existance of `annotations_dir` on a Result's
        # format class to guard against annotation actions being called on
        # Results with versions < 7.0.

        # Raises
        # ------
        # ValueError
        #     If the Result's format class has no `annotations_dir` and is
        #     thus a format version < 7.0.

        if self.annotations_dir is None:
            raise ValueError(
                'The Artifact or Visualization being used is associated with '
                'a QIIME 2 archive format of < 7.0. '
                'Annotation actions are only supported for QIIME 2 archive '
                'formats of 7.0 and above.'
            )

    def add_annotation(self, annotation, reference_uuid=None):
        self._validate_annotation_support()
        self._storage.add_annotation(annotation, reference_uuid)

    def get_annotation(self, name):
        """
        Retrieve an Annotation given by `name` from the Result object.

        Parameters
        ----------
        name : str
            The name of the Annotation to retrieve.

        Returns
        -------
        Annotation : obj
            The Annotation object associated with the provided name.

        Raises
        ------
        KeyError
            If no Annotation with the provided name is found.

        """
        self._validate_annotation_support()

        if name in self._annotations:
            return self._annotations[name]

        raise KeyError(f'No Annotation with name: "{name}" was found.')

    def iter_annotations(self, filter_by_type):
        """
        Constructs an iterable containing all Annotations associated with
        the Result object.
        """
        self._validate_annotation_support()

        if filter_by_type is None:
            yield from self._annotations.values()
        elif filter_by_type not in ANNOTATION_TYPE_DICT:
            raise ValueError(f'Unknown annotation type: "{filter_by_type}". '
                             'Supported annotation types are: '
                             f'{ANNOTATION_TYPE_DICT.keys()}')
        else:
            for annotation in self._annotations.values():
                if getattr(annotation, 'annotation_type') == filter_by_type:
                    yield annotation

    def remove_annotation(self, name):
        self._validate_annotation_support()
        self._storage.remove_annotation(name)

    def merge_annotations(self, other):
        import warnings
        annotation_uuids = \
            [str(annotation.id) for annotation in self._annotations.values()]

        for other_annotation in other._archiver._annotations.values():
            if str(other_annotation.id) not in annotation_uuids:
                try:
                    self.add_annotation(other_annotation)
                except ValueError as e:
                    if 'Duplicate name' in str(e):
                        warnings.warn(f'Duplicate name {other_annotation.name}'
                                      ' found. The annotation UUID will be'
                                      ' prepended to the name of the new'
                                      ' annotation.')
                        # It should not be possible for this to collide because
                        # we only get here if this annotation.id isn't present
                        # on the artifact.
                        other_annotation.name = \
                            f'{other_annotation.name}-{other_annotation.id}'
                        self.add_annotation(other_annotation)
                    else:
                        raise e

    def verify(self, signature_name):
        signature = self.get_annotation(signature_name)
        with self._storage.materialize() as directory:
            return self._verify_signature(signature, directory.path)

    def _iter_checksums(self, checksums_fp):
        checksum_line_regex = re.compile(r"^([0-9a-f]{128})\s\s(.+)$")
        with checksums_fp.open('r', encoding='utf-8') as fh:
            for line in fh:
                line = line.rstrip('\n')
                if not line:
                    continue
                match = checksum_line_regex.match(line)
                if match:
                    yield (match.group(1), match.group(2))

    def write_checksums(self, checksums):
        self._storage.write_manifest(self._fmt.CHECKSUM_TYPE, checksums)

    def has_checksums(self):
        return hasattr(self._fmt, 'CHECKSUM_FILE')

    def validate_checksums(self):
        if not self.has_checksums():
            return ChecksumDiff({}, {}, {})
        return self._storage.validate_checksums(self._fmt.CHECKSUM_TYPE)

    def metadata_paths(self):
        """Return metadata paths identified by the provenance action schema."""
        paths, relative = [], []
        if self.provenance_dir is not None:
            for action in self.provenance_dir.rglob('action.yaml'):
                members = metadata_members({
                    'action/action.yaml': action.read_bytes()
                })
                for member in sorted(members):
                    name = member.removeprefix('action/')
                    paths.append(action.parent / name)
                    relative.append(name)
        return [paths, relative]

    def redact_metadata(self):
        paths, _ = self.metadata_paths()
        self._storage.redact_metadata(paths, self._fmt.CHECKSUM_TYPE)
        if self.annotations_dir is not None:
            annotation = Note(
                name="Metadata-redaction",
                text="Redacted metadata from all Results in provenance.\n"
                     + "\n".join(str(path) for path in paths),
            )
            if annotation.name not in self._annotations:
                self.add_annotation(annotation)

    @classmethod
    def from_archive(cls, archive, *, replay=False):
        return cls(archive, replay=replay)

    @property
    def cache(self):
        return self._storage.cache

    @property
    def ref_id(self):
        return self._storage.ref_id

    @property
    def view(self):
        return self._storage._view

    def __reduce__(self):
        return (_restore_archiver, (self._storage, self._replay))

    def __copy__(self):
        duplicate = object.__new__(type(self))
        duplicate.__dict__ = self.__dict__.copy()
        return duplicate

    def __deepcopy__(self, memo):
        # Copying a handle does not create a distinct editable archive.
        return self

    def commit_whiteouts(self, records):
        from .archive import commit_whiteouts
        commit_whiteouts([self._storage], records)

    def _verify_signature(self, signature, root):
        signature_name = signature.name
        annotation_dir = root / "annotations" / str(signature.id)
        root_checksums_fp = root / "checksums.sha512"
        sig_checksums_fp = annotation_dir / "checksums.sha512"
        signature_fp = annotation_dir / "signature.gpg"
        try:
            fingerprint = getattr(signature, 'fingerprint')

            if not fingerprint:
                raise ValueError('Signature is missing fingerprint.')

            found_fingerprint = gpg_find_key(fingerprint)
            if not (normalize_fingerprint(found_fingerprint['fingerprint']) ==
                    normalize_fingerprint(signature.fingerprint)):
                raise ValueError('Found fingerprint does not match '
                                 'fingerprint associated with Signature.')

        except Exception as e:
            raise ValueError(f'Signer key not found in local GPG keyring: {e}')

        root_checksum_digest = sha512_file_hex(root_checksums_fp)
        if not root_checksum_digest == getattr(signature, 'checksum_digest'):
            raise ValueError(
                'Root checksums.sha512 does not match digest in metadata.yaml')

        try:
            env = os.environ.copy()
            unix_gpg_terminal_helper(env)

            subprocess.run(
                ['gpg', '--verify',
                 str(signature_fp),
                 str(root_checksums_fp)],
                check=True, env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )

        except FileNotFoundError:
            raise FileNotFoundError('`gpg` not found on PATH.')

        except subprocess.CalledProcessError as e:
            msg = (e.stderr or '').strip()
            if len(msg) > 500:
                msg = msg[:500] + '...'
            raise subprocess.CalledProcessError(
                f'`gpg --verify` failed (rc={e.returncode}): {msg}')

        missing, mismatched = [], []
        for exp_digest, relpath in self._iter_checksums(sig_checksums_fp):
            fp = annotation_dir / relpath
            if not fp.exists():
                missing.append(relpath)
                continue
            obs_digest = sha512_file_hex(fp)
            if obs_digest != exp_digest:
                mismatched.append({
                    'path': relpath,
                    'expected': exp_digest,
                    'actual': obs_digest
                })
        if missing:
            raise ValueError('The following expected files were not found: '
                             f'{missing}')
        if mismatched:
            raise ValueError('The following unexpected files were found: '
                             f'{mismatched}')

        return f'Signature `{signature_name}` verified successfully.'


def _restore_archiver(archive, replay):
    return Archiver.from_archive(archive, replay=replay)
