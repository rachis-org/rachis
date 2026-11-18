# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections
import uuid as _uuid
import pathlib
import weakref
import zipfile
import importlib
import os
import re
import io
import yaml
import shutil
import subprocess
from pathlib import Path

import rachis
import rachis.core.cite as cite
import rachis.core.util as util

from rachis.core.util import (
    sha512_file_hex, gpg_find_key, normalize_fingerprint,
    unix_gpg_terminal_helper, checksum, checksum_directory,
    from_checksum_format, is_uuid4
)
from rachis.core.archive.format.v0 import ArchiveFormat
from rachis.core.archive.provenance import (
    metadata_path_constructor, MetadataInfo, ZipActionYamlLoader
)
from rachis.core.annotate import Note, Annotation, ANNOTATION_TYPE_DICT

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
        with zipfile.ZipFile(str(self.path), mode='r') as zf:
            for name in zf.namelist():
                if name.startswith(relpath):
                    parts = pathlib.PurePosixPath(name).parts
                    if len(parts) > 0:
                        result = parts[0]
                        if result not in seen:
                            seen.add(result)
                            yield result

    def open(self, relpath):
        relpath = pathlib.Path(str(self.uuid)) / relpath
        with zipfile.ZipFile(str(self.path), mode='r') as zf:
            # The filehandle will still work even when `zf` is "closed"
            return io.TextIOWrapper(zf.open(self._as_zip_path(relpath)))

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

    @classmethod
    def _as_zip_path(cls, path):
        path = str(pathlib.PurePosixPath(path))
        # zip files don't work well with '.' which is the identity of a Path
        # obj, so just convert to empty string which is basically the identity
        # of a zip's entry
        if path == '.':
            path = ''
        return path

    def load_action_yaml(self):
        action_path = Path('provenance') / 'action' / 'action.yaml'
        with self.open(action_path) as fh:
            return yaml.load(fh, Loader=ZipActionYamlLoader)


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

    def load_action_yaml(self):
        action_path = self.path / 'provenance' / 'action' / 'action.yaml'
        with open(action_path) as fh:
            prov = yaml.safe_load(fh)

        return prov


class ArchiveCheck(_Archive):
    """Used by the Jupyter handlers"""

    # TODO: make this part of the archiver API at some point
    def open(self, relpath):
        abspath = os.path.join(str(self.path), relpath)
        return open(abspath, 'r')

    def relative_iterdir(self, relpath='.'):
        for p in pathlib.Path(self.path).iterdir():
            yield str(p.relative_to(self.path))

    def _get_uuid(self):
        return os.path.basename(self.path)


class Archiver:
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
    def _make_temp_path(cls, uuid):
        """Allocates a place in the cache for the file to be temporarily
        written. Returns this location and the cache in use.
        """
        from rachis.core.cache import get_cache

        cache = get_cache()
        path = cache.process_pool._allocate(uuid)
        return path, cache

    @classmethod
    def _destroy_temp_path(cls, process_alias):
        from rachis.core.cache import get_cache

        cache = get_cache()
        cache.process_pool.remove(str(process_alias))

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
    def get_archive(cls, filepath) -> _ZipArchive | _NoOpArchive:
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
    def peek(cls, filepath: Path):
        archive = cls.get_archive(filepath)
        Format = cls.get_format_class(archive.version)
        if Format is None:
            cls._futuristic_archive_error(filepath, archive)
        # NOTE: in the future, we may want to manipulate the results so that
        # older formats provide the "new" API even if they don't support it.
        # e.g. a new format has a new property that peek should describe. We
        # add some compatability code here to return a default for that
        # property on older formats.

        version, _ = archive._get_versions()
        if float(version) < 1:
            return [*Format.load_metadata(archive), None]

        yaml_dict = archive.load_action_yaml()

        plugin = yaml_dict['action'].get('plugin')
        if plugin is not None:
            plugin = plugin.split(':')[-1]
        action = yaml_dict['action'].get('action')
        if plugin:
            action = plugin + '.' + action

        return [*Format.load_metadata(archive), action]

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
    def load(cls, filepath, *args, replay=False):
        archive = cls.get_archive(filepath)
        path, cache = cls._make_temp_path(archive.uuid)

        try:
            Format = cls.get_format_class(archive.version)
            if Format is None:
                cls._futuristic_archive_error(filepath, archive)

            archive.mount(path)
            process_alias, data_path = \
                cache._rename_to_data(archive.uuid, path, replay=True)
            rec = ArchiveRecord(
                data_path, data_path / archive.VERSION_FILE, archive.uuid,
                archive.version, archive.framework_version)
            ref = cls(
                data_path, process_alias, Format(rec, replay=replay), cache)
            return ref
        # We really just want to kill these paths if anything at all goes wrong
        # Exceptions including keyboard interrupts are re-raised
        except:  # noqa: E722
            cls._destroy_temp_path(archive.uuid)
            if 'process_alias' in vars():
                cls._destroy_temp_path(process_alias)
            raise

    @classmethod
    def load_raw(cls, filepath, cache, *args, replay=False):
        archive = cls.get_archive(filepath)
        process_alias = cache._alias(str(archive.uuid))

        Format = cls.get_format_class(archive.version)
        if Format is None:
            cls._futuristic_archive_error(filepath, archive)

        path = pathlib.Path(filepath)

        rec = archive.mount(path)
        ref = cls(path, process_alias, Format(rec, replay=replay), cache)

        return ref

    @classmethod
    def from_data(cls, type, format, data_initializer, provenance_capture):
        uuid = _uuid.uuid4()
        path, cache = cls._make_temp_path(uuid)

        try:
            rec = _Archive.setup(uuid, path, cls.CURRENT_FORMAT_VERSION,
                                 rachis.__version__)

            Format = cls.get_format_class(cls.CURRENT_FORMAT_VERSION)
            Format.write(rec, type, format, data_initializer,
                         provenance_capture)

            process_alias, data_path = cache._rename_to_data(uuid, path)
            rec = ArchiveRecord(data_path, data_path / _Archive.VERSION_FILE,
                                uuid, cls.CURRENT_FORMAT_VERSION,
                                rachis.__version__)
            ref = cls(data_path, process_alias, Format(rec), cache)
            return ref
        # We really just want to kill these paths if anything at all goes wrong
        # Exceptions including keyboard interrupts are re-raised
        except:  # noqa: E722
            cls._destroy_temp_path(uuid)
            if 'process_alias' in vars():
                cls._destroy_temp_path(process_alias)
            raise

    def __init__(self, path, process_alias, fmt, cache):
        self.path = path
        self.process_alias = process_alias
        self._fmt = fmt
        self._destructor = weakref.finalize(self, cache._deallocate,
                                            str(self.process_alias))
        self._memoize_annotations = []

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
        return self._fmt.data_dir

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
        return getattr(self._fmt, 'citations', cite.Citations())

    def save(self, filepath):
        _ZipArchive.save(self.path, filepath)

    def get_checksums(self):
        with open(self.root_dir / self._fmt.CHECKSUM_FILE) as fh:
            return dict(from_checksum_format(line) for line in fh.readlines())

    def extract_uuid_in_provenance(self, file):
        return re.findall(r'provenance/artifacts/([0-9a-f-]{36})/', file)

    @property
    def _annotations(self):
        """
        Append any existing Annotations to `self._annotations`.
        Helper method for `add_annotation`, after a given Annotation
        has been written to disk.
        """
        if self._memoize_annotations:
            return self._memoize_annotations[0]

        annotations = {}
        annotations_dir = self.annotations_dir
        # annotations_dir will be None for all previous archive versions < 7.0
        if annotations_dir and os.path.exists(annotations_dir):
            for annotation_id in os.listdir(annotations_dir):
                annotation_path = os.path.join(annotations_dir, annotation_id)
                annotation = Annotation.load(annotation_path)
                annotations[annotation.name] = annotation

        self._memoize_annotations.append(annotations)

        return annotations


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

    def add_annotation(self, annotation, reference_uuid = None):
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
        if annotation.name in self._annotations:
            raise ValueError(
                'Duplicate name detected when attempting to add '
                f'Annotation with name: "{annotation.name}"\n'
                'Annotation names must be unique within each Result '
                'they are attached to.'
            )

        if not reference_uuid:
            annotation._write(annotations_dir=self.annotations_dir,
                            root_result_uuid=str(self.uuid),
                            referenced_result_uuid=str(self.uuid))
        else:
            annotation._write(annotations_dir=self.annotations_dir,
                            root_result_uuid=str(self.uuid),
                            referenced_result_uuid=str(reference_uuid))

        self._annotations[annotation.name] = annotation

        # now calculate checksums for all files within the newly minted
        # annotation subdir
        # TODO: think about moving these into annotation._write after 7.1
        annotation_dir = Path(self.annotations_dir) / str(annotation.id)
        checksum_ext = self._fmt.CHECKSUM_TYPE
        manifest = self._fmt.CHECKSUM_FILE

        checksums = util.checksum_directory(annotation_dir,
                                            checksum_type=checksum_ext)

        with (annotation_dir / manifest).open('w') as fh:
            for item in checksums.items():
                fh.write(util.to_checksum_format(*item))
                fh.write('\n')

        # this ensures additional attrs on Signature (signer name/email)
        # are present when running Archiver.verify
        loaded_annotation = Annotation.load(str(annotation_dir))
        self._annotations[loaded_annotation.name] = loaded_annotation

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
        annotations = self._annotations

        if name not in annotations:
            raise KeyError(f'No Annotation found with name: "{name}"')

        annotations_dir = self.annotations_dir
        annotation_disk_dir = os.path.join(annotations_dir,
                                           str(annotations[name].id))

        if not os.path.exists(annotation_disk_dir):
            raise ValueError('Unable to locate on-disk directory '
                             f'for Annotation with name: "{name}"')

        shutil.rmtree(annotation_disk_dir)
        del annotations[name]

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
        """
        Verify a Signature annotation by name on the provided Result.

        Parameters
        ----------
        signature_name
            Name of the Signature Annotation to verify.

        Notes
        -----
        The following checks are performed:
            - fingerprint match in local GPG keyring
            - sha512sum for root level checksums file matches checksum_digest
            - gpg detached signature verification
            - sha512sum checks for each file in signature-level checksums file
        """
        signature = self.get_annotation(signature_name)

        annotation_dir = \
            pathlib.Path(self.annotations_dir) / str(signature.id)

        root_fp = self.root_dir
        root_checksums_fp = root_fp / 'checksums.sha512'
        sig_checksums_fp = annotation_dir / 'checksums.sha512'
        signature_fp = annotation_dir / 'signature.gpg'

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
        with open(self.root_dir / self._fmt.CHECKSUM_FILE, 'w') as fh:
            for k, v in checksums.items():
                fh.write(f'{v}  {k}\n')

    def has_checksums(self):
        return hasattr(self._fmt, 'CHECKSUM_FILE')

    def validate_checksums(self):
        if not self.has_checksums():
            return ChecksumDiff({}, {}, {})

        obs = \
            dict(x for x in
                 checksum_directory(str(self.root_dir),
                                    checksum_type=self._fmt.CHECKSUM_TYPE)
                 .items()
                 if (x[0] != self._fmt.CHECKSUM_FILE and
                     pathlib.Path(x[0]).parts[0] != 'annotations')
                 )

        exp = self.get_checksums()

        obs_keys = set(obs)
        exp_keys = set(exp)

        added = {x: obs[x] for x in obs_keys - exp_keys}
        removed = {x: exp[x] for x in exp_keys - obs_keys}
        changed = {x: (exp[x], obs[x]) for x in exp_keys & obs_keys
                   if exp[x] != obs[x]}

        return ChecksumDiff(added=added, removed=removed, changed=changed)

    def metadata_paths(self):
        '''
        Finds and returns all absolute and relative metadata paths.
        '''
        def get_metadata_objects(yaml_dict):
            '''
            Recursively searches through dictionary returned by
            `yaml.safe_load` returning any `MetadataInfo` objects.
            '''
            metadata = []
            if isinstance(yaml_dict, MetadataInfo):
                metadata.append(yaml_dict)
            elif isinstance(yaml_dict, dict):
                for value in yaml_dict.values():
                    metadata.extend(get_metadata_objects(value))
            elif isinstance(yaml_dict, (list, tuple)):
                for value in yaml_dict:
                    metadata.extend(get_metadata_objects(value))
            return metadata

        yaml.SafeLoader.add_constructor('!metadata', metadata_path_constructor)

        metadata_paths = []
        relative_metadata_paths = []
        for action_yaml in Path(self.provenance_dir).rglob('action.yaml'):
            with open(action_yaml) as fh:
                metadata = yaml.safe_load(fh)
                metadatas = get_metadata_objects(metadata)
                for metadata in metadatas:
                    path = action_yaml.parent / metadata.relative_fp
                    metadata_paths.append(path)
                    relative_metadata_paths.append(metadata.relative_fp)

        return [metadata_paths, relative_metadata_paths]

    def redact_metadata(self):
        '''
        Empties metadata files. It is also neccessary to remove the
        corresponding lines in the `checksums` file.
        '''
        metadata_paths, relative_metadata_paths = self.metadata_paths()

        if len(metadata_paths) == 0:
            raise ValueError(
                'Cannot redact metadata from a Result without metadata.'
            )

        if all(os.path.getsize(path) == 0 for path in metadata_paths):
            raise ValueError(
                'Cannot redact metadata from a Result with only redacted '
                'metadata files.'
            )

        # Empty metadata files
        for metadata_path in metadata_paths:
            open(metadata_path, 'w').close()

        # Re-write checksums for empty metadata files
        checksums = self.get_checksums()

        for k, v in checksums.items():
            if any(path in k for path in relative_metadata_paths):
                checksums[k] = checksum(
                    self.root_dir / k, self._fmt.CHECKSUM_FILE.split('.')[1]
                )

        self.write_checksums(checksums)

        joined = '\n'.join(str(p) for p in metadata_paths)
        annotation = Note(
            name='Metadata-redaction',
            text=f'Redacted metadata from all Results in provenance for '
                 f'performance and/or privacy reasons.\n'
                 f"Redacted the following files:\n{joined}"
        )
        if self.annotations_dir is not None:
            self.add_annotation(annotation)

            for path in os.listdir(self.provenance_dir):
                if any(r_path in path for r_path in relative_metadata_paths):
                    ref_uuid = self.extract_uuid_in_provenance(path)
                    self.add_annotation(annotation, ref_uuid)
