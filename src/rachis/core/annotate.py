# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import pathlib
import subprocess
import uuid as _uuid
import yaml
import shutil

from collections import OrderedDict
from datetime import datetime

from rachis.core.util import (find_root_fp, sha512_file_hex,
                              gpg_find_key, format_algorithm,
                              unix_gpg_terminal_helper)
from rachis.util import is_valid_python_identifier


class Annotation():
    """General base class for all Annotation sub-classes.

    Parameters
    ----------
    name : str
        Name of the annotation.
        For each Result object, all Annotations must have a unique name.
        e.g. The same named Annotation can be attached to multiple Results,
        but each Result cannot contain multiple Annotations with the same name.

    Properties
    ----------
    uuid
        The minted uuid4 for each new Annotation that's added.
        This will be the name of each new Annotation's sub-directory within the
        `annotations` directory and is separate from any Result's uuid.

    created_at
        The minted date/time when an Annotation is created.
        Note that this is separate from when an Annotation is attached to a
        Results object, as this can occur at multiple times.

    Returns
    -------
    obj
        An instantiated Annotation of the specified sub-class.
        Note that instantiation of the Annotation base class is not supported.

    See Also
    --------
    Note

    """

    @classmethod
    def load(cls, filepath):
        """Load an Annotation.

        Parameters
        ----------
        filepath : str
            Path to load the Annotation from.

        Returns
        -------
        obj
            The instantiated Annotation sub-class.

        Raises
        ------
        ValueError
            If no `note.txt` file is found under the
            corresponding annotation directory.

        """
        if not hasattr(filepath, 'open'):
            filepath = pathlib.Path(filepath)
        with (filepath / 'metadata.yaml').open() as fh:
            meta_yaml = yaml.safe_load(fh)
            annotation_type = meta_yaml['type']

            annotation = _add_shared_attrs(annotation_type, meta_yaml)

            # NOTE
            if annotation_type == 'Note':
                # Validate that `note.txt` exists
                note_fp = filepath / 'note.txt'
                if not note_fp.exists():
                    raise ValueError(
                        'Unable to load malformed Note with name: '
                        f'"{annotation.name}" due to missing `note.txt` file.'
                    )
                # Attach contents to Note
                else:
                    with note_fp.open() as fh:
                        annotation.contents = fh.read()

            # SIGNATURE
            elif annotation_type == 'Signature':
                # signature-specific attrs
                annotation.algorithm = meta_yaml['algorithm']
                annotation.checksum_digest = meta_yaml['checksum_digest']
                annotation.signer_name = meta_yaml['signer_name']
                annotation.signer_email = meta_yaml['signer_email']
                annotation.fingerprint = meta_yaml.get('fingerprint')

                # Validate `signature.gpg` and Signature-level
                # `checksums.sha512` files exist
                sig_fp = filepath / 'signature.gpg'
                sig_checksum_fp = filepath / 'checksums.sha512'
                fp_dict = {'signature.gpg': sig_fp,
                           'checksums.sha512': sig_checksum_fp}

                for name, fp in fp_dict.items():
                    if not fp.exists():
                        raise ValueError(
                            'Unable to load malformed Signature with name: '
                            f'"{annotation.name}" '
                            f'due to missing `{name}` file.'
                        )

        annotation._filepath = filepath
        return annotation

    # We never expect this to be hit as the base class for Annotations
    # shouldn't ever be instantiated - only the supported sub-classes.
    @property
    def annotation_type(cls):
        raise NotImplementedError

    def __init__(self, name):
        """
        Construction for an initialized Annotation.

        Attributes
        ----------
        name
            The user-provided name of the Annotation.

        id
            The uuid4 ID associated with the Annotation.

        created_at
            The datetime when the Annotation was created.

        Raises
        ------
        TypeError
            If the Annotation base class is instantiated.

        """
        self.validate_name(name)

        if type(self) is Annotation:
            raise TypeError('Annotation is an abstract class'
                            ' and cannot be instantiated directly.')
        self.name = name
        self.id = _uuid.uuid4()
        self.created_at = datetime.now()

    def validate_name(self, name):
        """Validates that the given name is a valid Python idenitifier with the
        exception that `-` is allowed.

        Parameters
        ----------
        name : str
            The name to validate.

        Raises
        ------
        ValueError
            If the name passed in is not a valid Python identifier.
        """
        is_valid_python_identifier(name, 'name')

    def _write_meta_yaml(self, annotations_dir,
                         root_result_uuid, referenced_result_uuid,
                         algorithm=None, checksum_digest=None,
                         signer_name=None, signer_email=None,
                         fingerprint=None):
        """Write the contents of `metadata.yaml` for a given Annotation.

        Parameters
        ----------
        annotations_dir
            The path to the `annotations` directory within a Result object.
            Located under `provenance`.

        root_result_uuid
            The uuid of the Result object where an Annotation is being added.

        referenced_result_uuid
            The uuid of the Result object that an Annotation is referring to.
            Note that in 7.0, `root_result_uuid` and `referenced_result_uuid`
            are the same (i.e. Annotations can only refer to the same Result
            they are being attached to) but separate root and referenced uuids
            will be supported in future versions.

        Returns
        -------
        str
            The filepath where the Annotation's uuid-specific subdirectory
            containing the `metadata.yaml` file was written to.

        """
        # create the annotation directory if it's not already present
        # we don't care if it contains anything, just whether or not it exists
        if not os.path.exists(annotations_dir):
            os.mkdir(annotations_dir)

        # create the unique dir for a particular annotation
        annotation_uuid_dirname = \
            os.path.join(annotations_dir, str(self.id))
        os.mkdir(annotation_uuid_dirname)

        metadata = OrderedDict()
        metadata['id'] = str(self.id)
        metadata['name'] = self.name
        metadata['type'] = self.annotation_type
        metadata['created_at'] = self.created_at
        metadata['root_result_uuid'] = root_result_uuid
        metadata['referenced_result_uuid'] = referenced_result_uuid

        if self.annotation_type == 'Signature':
            metadata['algorithm'] = algorithm
            metadata['checksum_digest'] = checksum_digest
            metadata['signer_name'] = signer_name
            metadata['signer_email'] = signer_email
            metadata['fingerprint'] = fingerprint

        meta_yaml = os.path.join(annotation_uuid_dirname, 'metadata.yaml')
        with open(meta_yaml, 'w') as fh:
            fh.write(yaml.dump(metadata))

        return annotation_uuid_dirname


class UnknownAnnotation(Annotation):
    """Utility sub-class that handles loading newer Annotation types on an
    older version of Rachis/QIIME 2 that supports Annotations.
    """
    annotation_type = 'Unknown'

    def __init__(*args):
        raise NotImplementedError('`UnknownAnnotation` is an abstract class'
                                  ' used for handling Annotations associated'
                                  ' with future versions of Rachis.'
                                  ' It should not be instantiated directly.')

    _write_meta_yaml = __init__
    _write = __init__


class Note(Annotation):
    """Note sub-class, inherits from Annotations.

    Parameters
    ----------
    text : str
        Inline text that will be written inside the Note's `note.txt` file.
        This parameter is optional, but either `text` OR `filepath` must be
        provided.

    filepath : str
        Path to a file whose contents should be written inside the Note's
        `note.txt` file.
        This parameter is optional, but either `text` OR `filepath` must be
        provided.

    Properties
    ----------
    type : Note
        The type of Annotation being instantiated.

    Returns
    -------
    Note : obj
        The instantiated Note.

    See Also
    --------
    Annotation

    """
    annotation_type = 'Note'

    # NOTE: in future versions, name will become optional & the default value
    # will be the annotation's UUID (if name isn't provided by the user)
    def __init__(self, name, *, text=None, filepath=None):
        # Construct Annotation class
        super().__init__(name)
        # Ensure exactly one of text or filepath is provided
        if text and filepath:
            raise ValueError(
                'Cannot set both `text` and `filepath` params. '
                'Please provide either inline text or a filepath only.'
            )
        if not text and not filepath:
            raise ValueError(
                'No inputs provided to either `text` or `filepath`. '
                'Please provide either inline text or a filepath.'
            )

        if text is not None:
            self.contents = text
            self._filepath = None
        else:
            if not isinstance(filepath, (str, os.PathLike)):
                raise TypeError(
                    f'Unexpected input for `filepath`: {filepath} '
                    '`filepath` should either be a '
                    '`string` or an `os.PathLike` object.'
                )
            if not os.path.exists(filepath):
                raise ValueError(
                    f'File not found from provided `filepath`: {filepath} '
                    'Double check that the provided file exists '
                    ' and is in the expected location.'
                )

            self._filepath = str(filepath)
            self.contents = None

    def _write(self, annotations_dir, root_result_uuid,
               referenced_result_uuid):
        """Write the contents of an instantiated Note.

        Parameters
        ----------
        annotations_dir
            The path to the `annotations` directory within a Result object.
            Located under `provenance`.

        root_result_uuid
            The uuid of the Result object where an Annotation is being added.

        referenced_result_uuid
            The uuid of the Result object that an Annotation is referring to.
            Note that in 7.0, `root_result_uuid` and `referenced_result_uuid`
            are the same (i.e. Annotations can only refer to the same Result
            they are being attached to) but separate root and referenced uuids
            will be supported in future versions.

        See Also
        --------
        _write_meta_yaml

        """
        # call _write_meta_yaml to write the stuff that's the same
        # across both input types
        annotation_uuid_dirname = \
            self._write_meta_yaml(annotations_dir,
                                  root_result_uuid,
                                  referenced_result_uuid)

        note_path = os.path.join(annotation_uuid_dirname, 'note.txt')

        if self.contents:
            contents = self.contents.encode('utf-8')
        else:
            with open(self._filepath, 'rb') as fh:
                contents = fh.read()

        # validation for max size and parsability
        max_size = 10 * 1024 * 1024

        if len(contents) > max_size:
            raise ValueError('Note contents exceed maximum size of 10 MiB')

        try:
            contents.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('Note contents are not valid UTF-8')

        with open(note_path, 'wb') as fh:
            fh.write(contents)


class Signature(Annotation):
    """Signature sub-class, inherits from Annotations.

    Creates a cryptographic signature over the Result's root checksums.sha512
    file using credentials for an existing key pair via GnuPG.

    Parameters
    ----------
    name : str
        Annotation name (validated like other Annotations).

    fingerprint : str
        Fingerprint associated with the key pair in GnuPG
        that will be used for signing.

    Returns
    -------
    Signature : obj
        The instantiated Signature.

    See Also
    --------
    Annotation
    """
    annotation_type = 'Signature'

    # NOTE: in future versions, name will become optional & the default value
    # will be the annotation's UUID (if name isn't provided by the user)
    def __init__(self, name, *, fingerprint):
        # Construct Annotation class
        super().__init__(name)

        if not fingerprint:
            raise ValueError(
                'No input provided for `fingerprint`. '
                'Please provide `fingerprint` for key pair identification.'
            )

        # Signature-specific construction
        key_info = gpg_find_key(fingerprint)

        self.algorithm = format_algorithm(key_info)
        self.fingerprint = key_info['fingerprint']
        self.signer_name = key_info['chosen_uid']['name']
        self.signer_email = key_info['chosen_uid']['email']

    def _write(self, annotations_dir, root_result_uuid,
               referenced_result_uuid):
        """Write the contents of an instantiated Signature.

        Parameters
        ----------
        annotations_dir
            The path to the `annotations` directory within a Result object.
            Located under `provenance`.

        root_result_uuid
            The uuid of the Result object where an Annotation is being added.

        referenced_result_uuid
            The uuid of the Result object that an Annotation is referring to.
            Note that in 7.0 & 7.1, `root_result_uuid` and
            `referenced_result_uuid` are the same (i.e. Annotations can only
            refer to the same Result they are being attached to) but separate
            root and referenced uuids will be supported in future versions.
        """
        root_fp = pathlib.Path(find_root_fp(annotations_dir, root_result_uuid))
        annotations_dir = pathlib.Path(annotations_dir)
        checksums_fp = root_fp / 'checksums.sha512'

        if not checksums_fp.exists():
            raise ValueError(
                'Unable to create Signature due to malformed Result: '
                f'missing root checksums file at "{checksums_fp}".'
            )
        checksum_digest = sha512_file_hex(checksums_fp)

        env = os.environ.copy()
        unix_gpg_terminal_helper(env)

        annotation_uuid_dirname = None
        signature_dir = None
        sig_fp = None

        try:
            annotation_uuid_dirname = self._write_meta_yaml(
                str(annotations_dir), root_result_uuid,
                referenced_result_uuid, self.algorithm, checksum_digest,
                self.signer_name, self.signer_email,
                self.fingerprint
            )

            signature_dir = pathlib.Path(annotation_uuid_dirname)
            sig_fp = signature_dir / 'signature.gpg'

            cmd = [
                'gpg',
                '--local-user', str(self.fingerprint),
                '--output', str(sig_fp),
                '--detach-sign', str(checksums_fp)
            ]

            try:
                subprocess.run(cmd, check=True, env=env,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
            except FileNotFoundError as e:
                raise RuntimeError(
                    'GnuPG (`gpg`) is not installed or not on `PATH`. '
                    'Install GnuPG and ensure your key pair is available.'
                ) from e
            except subprocess.CalledProcessError as e:
                msg = (
                    '`gpg` signing failed. Ensure that the selected key '
                    'exists and is unlocked (or that pinentry can prompt for '
                    'the private key password).'
                )
                if e.stderr:
                    msg += f' Details: {e.stderr.strip()}'
                raise RuntimeError(msg) from e

            if not sig_fp.exists() or sig_fp.stat().st_size == 0:
                raise RuntimeError(
                    '`gpg` reported success but no signature was written.'
                )

            # smoke check to ensure signature writing was successful
            try:
                subprocess.run(
                    ['gpg', '--verify', str(sig_fp), str(checksums_fp)],
                    check=True, env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except subprocess.CalledProcessError:
                raise RuntimeError(
                    'Wrote signature.gpg but `gpg --verify` failed; '
                    'signature may be invalid.'
                )

            return

        # just to make extra sure there isn't a dangling
        # annotation dir or half baked contents
        except Exception as e:
            try:
                if annotation_uuid_dirname:
                    shutil.rmtree(annotation_uuid_dirname, ignore_errors=True)
                if signature_dir and signature_dir.exists():
                    shutil.rmtree(signature_dir, ignore_errors=True)
                elif (annotation_uuid_dirname and
                      os.path.exists(annotation_uuid_dirname)):
                    shutil.rmtree(annotation_uuid_dirname, ignore_errors=True)
            finally:
                raise RuntimeError(
                    f'Failed to create Signature annotation "{self.name}": {e}'
                )


# NOTE: UPDATE ME WITH EACH NEW ANNOTATION SUB-TYPE
ANNOTATION_TYPE_DICT = {'Note': Note, 'Signature': Signature}


# cls helper
def _add_shared_attrs(annotation_type, meta_yaml):
    cls = ANNOTATION_TYPE_DICT.get(annotation_type, UnknownAnnotation)
    annotation = cls.__new__(cls)

    annotation.id = meta_yaml['id']
    annotation.name = meta_yaml['name']
    annotation.annotation_type = meta_yaml['type']
    annotation.created_at = meta_yaml['created_at']

    return annotation
