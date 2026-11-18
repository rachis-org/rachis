# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import contextlib
import warnings
import hashlib
import stat
import os
import io
import re
import sys
import collections
import uuid as _uuid
import zipfile
import pathlib
import shutil
import subprocess
import typing

READ_ONLY_FILE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
READ_ONLY_DIR = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IRUSR \
    | stat.S_IRGRP | stat.S_IROTH
USER_GROUP_RWX = stat.S_IRWXU | stat.S_IRWXG
OTHER_NO_WRITE = stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH
# public key algorithm identifiers
# https://datatracker.ietf.org/doc/html/rfc4880#section-9.1
_PUBKEY_ALG = {
    '1': 'RSA',
    '2': 'RSA',
    '3': 'RSA',
    '16': 'ElGamal',
    '17': 'DSA',
    '18': 'ECDH',
    '19': 'ECDSA',
    '22': 'EdDSA'
}


def in_test_mode():
    """Whether the framework is running in test mode.

    Test mode is enabled by setting either RACHISTEST or QIIMETEST to any
    value, including an empty one. QIIMETEST is the historical name for this
    variable and remains supported; the two are interchangeable.

    """
    return 'RACHISTEST' in os.environ or 'QIIMETEST' in os.environ


def get_view_name(view):
    from .format import FormatBase
    if not isinstance(view, type):
        view = view.__class__

    if issubclass(view, FormatBase):
        # Not qualname because we don't have a notion of "nested" formats
        return view.__name__

    return ':'.join([view.__module__, view.__qualname__])


def tuplize(x):
    # get_origin returns None if not a GenericAlias
    if typing.get_origin(x) is tuple:
        # tuple[X, Y] -> (X, Y)
        return typing.get_args(x)
    if type(x) is not tuple:
        return (x,)
    return x


def overrides(cls):
    def decorator(func):
        if not hasattr(cls, func.__name__):
            raise AssertionError("%r does not override %r"
                                 % (func, cls.__name__))
        return func
    return decorator


def superscript(number):
    table = {
        '0': chr(8304), '1': chr(185), '2': chr(178), '3': chr(179),
        **{str(i): chr(x) for i, x in enumerate(range(8308, 8314), 4)},
        'a': chr(7491), 'e': chr(7497), 'f': chr(7584), 'i': chr(8305),
        'n': chr(8319), '-': chr(8315), '.': chr(39), ',': chr(39)
    }
    return ''.join([table[d] for d in str(number)])


def find_duplicates(iterable):
    """Find duplicate values in an iterable.

    Parameters
    ----------
    iterable : iterable
        Iterable to search for duplicates.

    Returns
    -------
    set
        Values that are duplicated in `iterable`.

    Notes
    -----
    Values in `iterable` must be hashable.

    """
    # Modified from https://stackoverflow.com/a/9835819/3776794 to return
    # duplicates instead of remove duplicates from an iterable.
    seen = set()
    duplicates = set()
    for value in iterable:
        if value in seen:
            duplicates.add(value)
        else:
            seen.add(value)
    return duplicates


# Concept from: http://stackoverflow.com/a/11157649/579416
def duration_time(relative_delta):
    attrs = ['years', 'months', 'days', 'hours', 'minutes', 'seconds',
             'microseconds']
    results = []
    for attr in attrs:
        value = getattr(relative_delta, attr)
        if value != 0:
            if value == 1:
                # Remove plural 's'
                attr = attr[:-1]
            results.append("%d %s" % (value, attr))
    if results:
        text = results[-1]
        if results[:-1]:
            text = ', and '.join([', '.join(results[:-1]), text])
        return text
    else:
        # Great Scott! No time has passed!
        return '0 %s' % attrs[-1]


def has_checksum_native(checksum_type):
    if checksum_type == 'md5':
        checksum_util = 'md5sum'
    elif checksum_type == 'sha512':
        checksum_util = 'sha512'

    return shutil.which(f'{checksum_util}') is not None


def checksum(filepath, checksum_type):
    if not in_test_mode() and has_checksum_native(checksum_type):
        return checksum_native(filepath, checksum_type)
    else:
        return checksum_python(filepath, checksum_type)


def checksum_python(filepath, checksum_type):
    if checksum_type == 'md5':
        hash_obj = hashlib.md5()
    elif checksum_type == 'sha512':
        hash_obj = hashlib.sha512()
    # we shouldn't ever hit this branch, but just in case
    else:
        raise TypeError(f'Unsupported checksum type: {checksum_type!r}')

    with open(str(filepath), mode='rb') as fh:
        for chunk in iter(lambda: fh.read(io.DEFAULT_BUFFER_SIZE), b""):
            hash_obj.update(chunk)
    return hash_obj.hexdigest()


def checksum_native(filepath, checksum_type):
    if checksum_type == 'md5':
        cmd = ['md5sum', str(filepath)]
    elif checksum_type == 'sha512':
        cmd = ['sha512sum', str(filepath)]
    # we shouldn't ever hit this branch, but just in case
    else:
        raise TypeError(f'Unsupported checksum type: {checksum_type!r}')

    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    _, digest = from_checksum_format(result.stdout)
    return digest


def checksum_zip(zf: zipfile.ZipFile, filepath: str,
                 checksum_type: str) -> str:
    """
    Given a ZipFile object and relative filepath within the zip archive,
    returns the checksum of the file
    """
    if checksum_type == 'md5':
        hash_obj = hashlib.md5()
    elif checksum_type == 'sha512':
        hash_obj = hashlib.sha512()
    # we shouldn't ever hit this branch, but just in case
    else:
        raise TypeError(f'Unsupported checksum type: {checksum_type!r}')

    with zf.open(filepath) as fh:
        for chunk in iter(lambda: fh.read(io.DEFAULT_BUFFER_SIZE), b""):
            hash_obj.update(chunk)
    return hash_obj.hexdigest()


def checksum_directory(directory, checksum_type):
    if not in_test_mode() and has_checksum_native(checksum_type):
        checksum = checksum_native
    else:
        checksum = checksum_python

    directory = str(directory)
    sums = collections.OrderedDict()
    for root, dirs, files in os.walk(directory, topdown=True):
        dirs[:] = sorted([d for d in dirs if not d[0] == '.'])
        for file in sorted(files):
            if file[0] == '.':
                continue

            path = os.path.join(root, file)
            sums[os.path.relpath(path, start=directory)] = \
                checksum(path, checksum_type)
    return sums


def checksum_directory_zip(zf: zipfile.ZipFile, checksum_type: str) -> dict:
    """
    Returns a mapping of fp/checksum pairs for all files in zf.

    The root dir has been removed from these filepaths. This mimics the output
    in checksums.md5 (without sorted descent), but is not generalizable beyond
    Rachis archives.
    """
    sums = dict()
    for file in zf.namelist():
        fp = pathlib.Path(file)
        if fp.name == f'checksums.{checksum_type}':
            continue

        file_parts = list(fp.parts)
        internal_path_parts = file_parts[1:]

        if internal_path_parts and internal_path_parts[0] == 'annotations':
            continue

        fp_w_o_root_uuid = pathlib.Path(*(file_parts[1:]))
        sums[str(fp_w_o_root_uuid)] = checksum_zip(zf, file, checksum_type)

    return sums


def to_checksum_format(filepath, checksum):
    # see https://www.gnu.org
    # /software/coreutils/manual/html_node/md5sum-invocation.html
    if '\\' in filepath or '\n' in filepath:
        filepath = filepath.replace('\\', '\\\\').replace('\n', '\\n')
        checksum = '\\' + checksum

    return '%s  %s' % (checksum, filepath)


def from_checksum_format(line):
    line = line.rstrip('\n')
    parts = line.split('  ', 1)
    if len(parts) < 2:
        parts = line.split(' *', 1)

    checksum, filepath = parts

    if checksum[0] == '\\':
        chars = ''
        escape = False
        # Gross, but regular `.replace` will overlap with itself and
        # negative lookbehind in regex is *probably* harder than scanning
        for char in filepath:
            # 1) Escape next character
            if not escape and char == '\\':
                escape = True
                continue

            # 2) Handle escape sequence
            if escape:
                try:
                    chars += {'\\': '\\', 'n': '\n'}[char]
                except KeyError:
                    chars += '\\' + char  # Wasn't an escape after all
                escape = False
                continue

            # 3) Nothing interesting
            chars += char

        checksum = checksum[1:]
        filepath = chars

    return filepath, checksum


@contextlib.contextmanager
def warning():
    def _warnformat(msg, category, filename, lineno, file=None, line=None):
        return '%s:%s: %s: %s\n' % (filename, lineno, category.__name__, msg)

    default_warn_format = warnings.formatwarning
    try:
        warnings.formatwarning = _warnformat
        warnings.filterwarnings('always')
        yield warnings.warn
    finally:
        warnings.formatwarning = default_warn_format


# Descriptor protocol for creating an attribute that is bound to an
# (arbitrarily nested) attribute accessible to the instance at runtime.
class LateBindingAttribute:
    def __init__(self, attribute):
        self._attribute = attribute

    def __get__(self, obj, cls=None):
        attrs = self._attribute.split('.')
        curr_attr = obj
        for attr in attrs:
            curr_attr = getattr(curr_attr, attr)
        return staticmethod(curr_attr).__get__(obj, cls)

def _immutable_error(obj, *args):
    raise TypeError('%s is immutable.' % obj.__class__.__name__)


class ImmutableBase:
    def _freeze_(self):
        """Disables __setattr__ when called. It is idempotent."""
        self._frozen = True  # The particular value doesn't matter

    __delattr__ = __setitem__ = __delitem__ = _immutable_error

    def __setattr__(self, *args):
        # This doesn't stop silly things like
        # object.__setattr__(obj, ...), but that's a pretty rude thing
        # to do anyways. We are just trying to avoid accidental mutation.
        if hasattr(self, '_frozen'):
            _immutable_error(self)
        super().__setattr__(*args)


def sorted_poset(iterable, *, key=None, reverse=False):
    values = list(iterable)
    elements = values
    if key is not None:
        elements = [key(x) for x in values]

    result = []
    sorted_elements = []
    for value, element in zip(values, elements):
        idx = 0
        for idx, placed in enumerate(sorted_elements, 1):
            if element <= placed:
                idx -= 1
                break

        result.insert(idx, value)
        sorted_elements.insert(idx, element)
    if reverse:
        result = list(reversed(result))
    return result


def is_uuid4(uuid_str):
    # Adapted from https://gist.github.com/ShawnMilo/7777304
    try:
        uuid = _uuid.UUID(hex=uuid_str, version=4)
    except ValueError:
        # The string is not a valid hex code for a UUID.
        return False

    # If uuid_str is a valid hex code, but an invalid uuid4, UUID.__init__
    # will convert it to a valid uuid4.
    return str(uuid) == uuid_str


def set_permissions(path, file_permissions=None, dir_permissions=None,
                    skip_root=False):
    """Set permissions on all directories and files under and including path
    """
    # Panfs is currently causing issues for us setting permissions. We still
    # want to set rwx for user and group before we remove things to ensure we
    # can remove them, but we want to temporarily no-op other permission
    # changes
    if file_permissions != USER_GROUP_RWX:
        file_permissions = None

    if dir_permissions != USER_GROUP_RWX:
        dir_permissions = None

    # Just get out if we aren't doing anything
    if file_permissions is None and dir_permissions is None:
        return

    for directory, _, files in os.walk(path):
        # We may want to set permissions under a directory but not on the
        # directory itself.
        if dir_permissions and not (skip_root and directory == str(path)):
            try:
                os.chmod(directory, dir_permissions)
            except FileNotFoundError:
                pass

        for file in files:
            if file_permissions:
                try:
                    os.chmod(os.path.join(directory, file), file_permissions)
                except FileNotFoundError:
                    pass


def touch_under_path(path):
    """Touches everything under a given path to ensure they don't get culled by
    Mac
    """
    for directory, _, files in os.walk(path):
        try:
            os.utime(directory, None, follow_symlinks=False)
        except FileNotFoundError:
            pass

        for file in files:
            try:
                os.utime(
                    os.path.join(directory, file), None, follow_symlinks=False)
            except FileNotFoundError:
                pass


def create_collection_name(*, name, key, idx, size):
    """ Only accepts kwargs. Creates a name for a collection item in a
        standardized way. Assumes 0 based indexing.
    """
    return [name, key, f'{idx + 1}/{size}']


# annotation helpers
# helper for parsing user name/email for keypair identification
def _parse_uid(uid_str):
    email_regex = re.compile(r'.*<([^>]+)>')
    uid_match = email_regex.match(uid_str or "")
    if uid_match:
        email = uid_match.group(1)
        name = uid_str[: uid_str.index('<')].strip()
        return name or None, email or None
    return (uid_str.strip() or None, None)


# Apparently this is helpful on Unix to GPG to find the correct terminal
def unix_gpg_terminal_helper(env):
    try:
        if sys.stdin and sys.stdin.isatty():
            env.setdefault('GPG_TTY', os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass
    return env


# helper for normalizing fingerprint formatting
def normalize_fingerprint(s):
    return re.sub(r'\s+', '', (s or '')).upper()


# helper for locating root_fp for a given Result
def find_root_fp(annotations_dir, root_result_uuid):
    p = pathlib.Path(annotations_dir)
    parts = p.parts
    try:
        idx = parts.index(root_result_uuid)
    except ValueError:
        raise ValueError('Could not locate result UUID '
                         f'"{root_result_uuid}" in path: {p}')

    return pathlib.Path(*parts[:idx + 1])


# helper for calculating the root level checksum digest
def sha512_file_hex(path):
    hex = hashlib.sha512()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024*1024), b""):
            hex.update(chunk)
    return hex.hexdigest()


# helper for pulling keypair info from a given fingerprint
def gpg_find_key(fingerprint_raw):
    fingerprint = normalize_fingerprint(fingerprint_raw)

    if not re.fullmatch(r'[0-9A-F]{40}|[0-9A-F]{64}', fingerprint):
        raise ValueError('Expected a full GPG fingerprint (40 or 64 chars).')

    cmd = [
        'gpg',
        '--list-keys',
        '--with-colons',
        '--fingerprint',
        '--keyid-format=long',
        fingerprint_raw
    ]

    try:
        output = subprocess.check_output(cmd, text=True)
    except FileNotFoundError as e:
        raise RuntimeError('`gpg` not found on `PATH`.') from e
    except subprocess.CalledProcessError:
        raise RuntimeError(
            'No matching key found for the provided fingerprint')

    key_info = {
        'fingerprint': None,
        'algorithm': None,
        'length': None,
        'curve': None,
        'uids': [],
        'chosen_uid': None
    }

    # format for the output of `gpg --list-keys`
    # pub:...:<len>:<algo>:<keyid>:...
    # fpr:::::::::<PRIMARY-FINGERPRINT>::    -> fingerprint for the primary key
    # uid:::::::<Name <email>>:              -> UID(s) for the primary key
    # uid:::::::<Other Name <other@example>>:
    # sub:...:                               -> subkey (not the primary)
    # fpr:::::::::<SUBKEY-FINGERPRINT>::     -> fingerprint for the subkey
    # ...

    # this state flag tells us whether or not we're in the primary key block
    in_primary = False

    for line in output.splitlines():
        parts = line.split(':')
        tag = parts[0]

        # public key tag; the primary key info that matches
        # the fingerprint will be here
        if tag == 'pub':
            in_primary = True
            # length and algorithm are always present on pub lines
            # so don't need to check truthiness on these
            length = parts[2] if len(parts) > 2 else '0'
            algorithm_num = parts[3] if len(parts) > 3 else ''
            # curve is optional and only set for ECC keys (ECDSA/ECDH/EdDSA)
            # when not applicable, gpg leaves this empty - hence the need
            # to check truthiness on this field
            curve = parts[15] if len(parts) > 15 and parts[15] else None
            key_info['length'] = int(length) if str(length).isdigit() else 0
            key_info['algorithm'] = \
                _PUBKEY_ALG.get(algorithm_num, f'ALG-{algorithm_num}')
            key_info['curve'] = curve
        # subkey fingerprint (if applicable)

        elif in_primary and tag == 'fpr':
            # confirm primary fingerprint matches the input fingerprint
            normalized_fingerprint = \
                normalize_fingerprint(parts[9] if len(parts) > 9 else '')
            if fingerprint and normalized_fingerprint != fingerprint:
                # If gpg listed a different key somehow, skip it
                continue
            # this ensures we don't overwrite the primary with a subkey
            if key_info['fingerprint'] is None:
                key_info['fingerprint'] = normalized_fingerprint

        # fill in name/email from given uid
        elif in_primary and tag == 'uid':
            raw = parts[9] if len(parts) > 9 else ''
            name, email = _parse_uid(raw)
            key_info['uids'].append({'raw': raw, 'name': name, 'email': email})

        # If we ever saw a new 'pub' after the first, we could break once
        # fingerprint is confirmed. But gpg with a full fingerprint should
        # return a single primary key.

    # confirmation that the listed key's fingerprint matches input
    if key_info['fingerprint'] is None:
        raise RuntimeError('Could not confirm primary key fingerprint '
                           'from `gpg` output.')

    # choose a default UID (first one if present)
    key_info['chosen_uid'] = (
        key_info['uids'][0] if key_info['uids']
        else {'raw': None, 'name': None, 'email': None}
    )

    return key_info


# helper for formatting keypair algorithm in metadata.yaml
def format_algorithm(key_info):
    algorithm = key_info.get('algorithm')
    curve = (key_info.get('curve') or '').lower()
    length = key_info.get('length') or 0
    if algorithm == 'EdDSA' and curve == 'ed25519':
        return 'Ed25519'
    elif algorithm in {'ECDSA', 'ECDH'} and key_info.get('curve'):
        return f'{algorithm}/{key_info["curve"]}'
    elif algorithm in {'RSA', 'DSA'} and length:
        return f'{algorithm}-{length}'
    else:
        return algorithm or 'unknown'


def replace_bytes_in_directory(directory, old_bytes, new_bytes, extensions,
                               buffer_size=None):
    """
    Recursively replaces all occurrences of old_bytes with new_bytes in files
    under the given directory. Only processes files with specified extensions.

    Parameters
    ----------
    directory : str
        The root directory to recursively search.
    old_bytes : bytes
        The byte sequence to find and replace. Must be at least 2 bytes long.
    new_bytes : bytes
        The byte sequence to replace old_bytes with.
    extensions : set[str] or list[str]
        File extensions to process (e.g., {'.txt', '.md'}).
        Extensions should include the leading dot.
    buffer_size : int, optional
        Size of the read buffer in bytes. Default is 4096.
    """
    extensions = set(extensions)

    for root, _, files in os.walk(directory):
        for file in files:
            if os.path.splitext(file)[1] in extensions:
                filepath = os.path.join(root, file)
                replace_bytes_in_file(filepath, old_bytes, new_bytes,
                                      buffer_size)


def replace_bytes_in_file(filepath, old_bytes, new_bytes, buffer_size=None):
    """
    Replaces all occurrences of old_bytes with new_bytes in the specified file.

    Parameters
    ----------
    filepath : str
        Path to the file to process.
    old_bytes : bytes
        The byte sequence to find and replace. Should be at least 2 bytes long.
        If only replacing 1 byte, use a simpler implementation that does not
        have to consider chunk boundaries.
    new_bytes : bytes
        The byte sequence to replace old_bytes with.
    buffer_size : int, optional
        Size of the read buffer in bytes. Default is io.DEFAULT_BUFFER_SIZE.
    """
    if buffer_size is None:
        buffer_size = io.DEFAULT_BUFFER_SIZE

    # Compile the regex pattern for old_bytes
    pattern = re.compile(re.escape(old_bytes))

    tempfile = filepath + '._tmp_'
    with open(filepath, 'rb') as src, open(tempfile, 'wb') as dst:
        # Initialize a buffer to handle overlapping matches
        overlap_buffer = b''

        while True:
            # Read the next chunk of data
            chunk = src.read(buffer_size)
            if not chunk:
                break

            # Prepend any overlap from the previous chunk
            chunk = overlap_buffer + chunk

            # Find all matches in the current chunk
            matches = list(pattern.finditer(chunk))

            # If matches are found, process them
            if matches:
                last_match = matches[-1]
                last_match_end = last_match.end()

                # Write up to the last match's end
                dst.write(pattern.sub(new_bytes, chunk[:last_match_end]))

                # Save the overlap for the next chunk
                overlap_buffer = chunk[last_match_end:]
            else:
                # If no matches, write the chunk minus the possible overlap
                write_end = max(0, len(chunk) - len(old_bytes) + 1)
                dst.write(chunk[:write_end])

                # Save the overlap for the next chunk
                overlap_buffer = chunk[write_end:]

        # Write any remaining overlap buffer
        if overlap_buffer:
            dst.write(pattern.sub(new_bytes, overlap_buffer))

    # Replace the original file with the modified file
    os.replace(tempfile, filepath)


def flatten_children(dictionary, child_key='children'):
    results = []
    for value in dictionary.values():
        value = value.copy()
        results.append(value)
        children = value.pop(child_key, {})
        results.extend(flatten_children(children, child_key))
    return results
