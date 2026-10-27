# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""ZIP splicing without decompression or recompression.

Classes
-------
ZipFile
    Standard-library ZIP reader/writer extended with raw member operations.

Warnings
--------
Callers must supply correct ZipInfo metadata and distinct source and target
files. Archive ownership and local/central metadata agreement are not checked.
Any failure may leave partial output; callers must discard that output.

Notes
-----
``ZipFile`` extends the standard-library class; ordinary reads and writes keep
their usual behavior. ``open_raw`` and ``write_raw`` exchange compressed member
payloads, while ``copy_member`` and ``ZipFile.splice`` compose archives.
Headers and directory offsets are rebuilt, so whole ZIP files need not be
byte-identical, but copied compressed payloads are byte-identical.

Raw sources must be blocking, seekable binary streams supporting ``readinto``.
Destinations may be non-seekable. Raw operations reject encrypted and patched
entries and detect truncated headers/payloads. They trust central ZipInfo
sizes, CRCs and methods; use the inherited ``testzip`` for decompression/CRC
validation. ZIP64 sizes and offsets are decoded by the stdlib reader and
regenerated for the destination, never copied as stale extra fields.

Payload reads position and fill the buffer under one source lock, without
probing the underlying cursor or allocating an intermediate bytes buffer.
Splicing streams selected entries in order and rejects collisions as they
occur. Selection planning and choosing winning entries belong to the caller.

This module intentionally contains no cache or archive-format semantics.
"""

import copy
import io
import struct
import zipfile


__all__ = ["ZipFile"]

# ZIP layouts follow APPNOTE sections 4.3.7, 4.4.4 and 4.5:
# https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT
# '<' means little-endian, with no padding; H = uint16, I = uint32.
#
# Local file header: 30 fixed bytes, then filename and extra-field bytes.
# +--------+------+-------+----------------------------------+
# | Offset | Code | Bytes | Field                            |
# +--------+------+-------+----------------------------------+
# |      0 |  4s  |     4 | Signature: PK\x03\x04            |
# |      4 |   H  |     2 | Version needed to extract        |
# |      6 |   H  |     2 | General-purpose flags            |
# |      8 |   H  |     2 | Compression method               |
# |     10 |   H  |     2 | Modification time (DOS)          |
# |     12 |   H  |     2 | Modification date (DOS)          |
# |     14 |   I  |     4 | CRC-32                           |
# |     18 |   I  |     4 | Compressed size                  |
# |     22 |   I  |     4 | Uncompressed size                |
# |     26 |   H  |     2 | Filename length                  |
# |     28 |   H  |     2 | Extra-field length               |
# +--------+------+-------+----------------------------------+
# [fixed header][filename: name_len][extras: extra_len][compressed payload]
# payload_offset = header_offset + 30 + name_len + extra_len
_LOCAL = struct.Struct("<4s5H3I2H")

# Each extra field is a length-delimited record; many can be concatenated.
# Byte offset:  0              2              4
#               +--------------+--------------+-------------------------+
#               | Tag (H)      | Length (H)   | Payload (Length bytes)  |
#               +--------------+--------------+-------------------------+
# _EXTRA reads/writes only the first four bytes, not the payload.
_EXTRA = struct.Struct("<HH")

# Extra-field tags are identifiers, not bit masks.
_ZIP64 = 0x0001         # Extended 64-bit sizes/offsets, rebuilt on relocation.
_UNICODE_PATH = 0x7075  # Unicode filename with CRC of the original name bytes.

# General-purpose flags, drawn most-significant bit first:
#                       15..12  11..8  7..4  3..0
#                       +-----+------+-----+-----+
# _DESCRIPTOR           |0000 | 0000 |0000 |1000 | = 1 << 3
# _UNSUPPORTED_FLAGS    |0010 | 0000 |0110 |0001 | = bits 13, 6, 5, 0
#                       +-----+------+-----+-----+
# Bit  3: CRC/sizes follow the payload in a data descriptor. Raw writes clear
#         this bit because those values are already known for the new header.
# Bit  0: Encrypted payload.
# Bit  5: Compressed patch data.
# Bit  6: Strong encryption.
# Bit 13: Local header values masked for central-directory encryption.
# Bit 11: UTF-8 names/comments; accepted and recomputed when names are encoded.
_DESCRIPTOR = 1 << 3
_UNSUPPORTED_FLAGS = (1 << 0) | (1 << 5) | (1 << 6) | (1 << 13)
_CHUNK_SIZE = 1024 * 1024  # Copy at most 1 MiB of payload per iteration.


def _destination_extra(data, *, renamed):
    """Remove ZIP64 fields and Unicode-path fields invalidated by a rename.

    Parameters
    ----------
    data : bytes
        Source extra-field records.
    renamed : bool
        Whether the destination filename differs from the source filename.

    Returns
    -------
    bytes
        Retained records, preserving their original bytes and order.
    """
    kept = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < _EXTRA.size:
            raise zipfile.BadZipFile("Truncated ZIP extra field header")
        tag, size = _EXTRA.unpack_from(data, offset)
        end = offset + _EXTRA.size + size
        if end > len(data):
            raise zipfile.BadZipFile("Truncated ZIP extra field data")
        if tag != _ZIP64 and not (renamed and tag == _UNICODE_PATH):
            kept.append(data[offset:end])
        offset = end
    return b"".join(kept)


def _destination_info(info, arcname=None):
    """Copy member metadata and prepare it for destination headers.

    Parameters
    ----------
    info : zipfile.ZipInfo
        Source metadata, left unchanged.
    arcname : str or None, optional
        Destination filename. Default is None, preserving the source name.

    Returns
    -------
    zipfile.ZipInfo
        New metadata with normalized filename flags, no source read boundary,
        and stale ZIP64 or renamed Unicode-path extra fields removed. The
        writer assigns the destination header offset and generates ZIP64
        fields.
    """
    entry = copy.copy(info)
    if arcname is not None:
        # Use ZipInfo's filename normalization, including directory suffix.
        entry.filename = zipfile.ZipInfo(arcname).filename
        entry.orig_filename = entry.filename
    # Re-encode names consistently for both rebuilt headers, including
    # sources read with metadata_encoding instead of CP437.
    entry.flag_bits &= ~(_DESCRIPTOR | 0x800)
    entry.flag_bits = entry._encodeFilenameFlags()[1]
    # A copied source boundary must never constrain reads at the new
    # destination offset, including reads before the writer is closed.
    entry._end_offset = None
    entry.extra = _destination_extra(
        entry.extra, renamed=entry.filename != info.filename
    )
    return entry


def _info(source, member):
    if not source.fp:
        raise ValueError("Source ZIP is closed")
    if source._writing:
        raise ValueError("Source ZIP has an open writing handle")
    if isinstance(member, zipfile.ZipInfo):
        # Follow stdlib ZipFile.open: caller-supplied metadata is trusted.
        # Do not enumerate or index the source to prove ownership.
        return member
    return source.getinfo(member)


def _payload_offset(file, info):
    header = file.read_at(info.header_offset, _LOCAL.size)
    if len(header) != _LOCAL.size:
        raise zipfile.BadZipFile("Truncated local file header")
    fields = _LOCAL.unpack(header)
    if fields[0] != b"PK\x03\x04":
        raise zipfile.BadZipFile("Invalid local file header signature")
    if (fields[2] | info.flag_bits) & _UNSUPPORTED_FLAGS:
        raise NotImplementedError("Raw copying excludes encryption/patches")
    # Central ZipInfo already resolves ZIP64 sizes and offsets. Only the local
    # filename/extra lengths are needed; skip those bytes without parsing them.
    return info.header_offset + _LOCAL.size + fields[-2] + fields[-1]


# stdlib _SharedFile gives each member reader its own logical cursor (_pos),
# while sharing the ZipFile's underlying file object and RLock. Its read/seek
# methods hold that lock while positioning and accessing the shared file;
# they update _pos but do not restore the shared cursor afterward. The writing
# callback prevents reads while a member write handle is active.
#
# _SourceFile reuses that lock, writing callback and close protocol. Its *_at
# methods bypass _pos: _RawReader tracks the member-relative position and
# supplies an absolute offset for each read. This avoids underlying tell()
# calls and allows readinto() directly into the supplied buffer.
#
# Lifetime (no separate file descriptor is opened):
#
#   ZipFile                         starts with one file reference
#      |
#      +-- _open_raw()              adds one reference per raw stream
#              |
#              +-- BufferedReader -> _RawReader -> _SourceFile
#
# Closing BufferedReader closes _RawReader, which calls _SourceFile.close().
# Inherited _SharedFile.close() detaches its file object and invokes the
# source ZipFile's _fpclose callback once, releasing that stream's reference.
# A header/setup failure in _open_raw() takes the same release path.
#
# ZipFile.close() clears source.fp and releases the ZipFile's own reference;
# existing readers still hold the file object and can continue reading.
# _fpclose closes the underlying file only when the reference count reaches
# zero and ZipFile opened the file itself. Caller-provided streams stay open
# and remain the caller's responsibility.
class _SourceFile(zipfile._SharedFile):
    """Retain stdlib handle ownership with reads at explicit source offsets."""

    def read_at(self, offset, size):
        with self._lock:
            self._check_readable()
            self._file.seek(offset)
            return self._file.read(size)

    def readinto_at(self, offset, buffer):
        with self._lock:
            self._check_readable()
            self._file.seek(offset)
            return self._file.readinto(buffer)

    def _check_readable(self):
        if self._writing():
            raise ValueError("Source ZIP has an open writing handle")


class _RawReader(io.RawIOBase):
    def __init__(self, file, offset, size):
        super().__init__()
        self._file = file
        self._offset = offset
        self._size = size
        self._position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        self._checkClosed()
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        self._checkClosed()
        bases = {io.SEEK_SET: 0, io.SEEK_CUR: self._position,
                 io.SEEK_END: self._size}
        if whence not in bases:
            raise ValueError("Invalid seek origin")
        position = bases[whence] + offset
        if position < 0:
            raise ValueError("Negative seek position")
        self._position = position
        return position

    def readinto(self, buffer):
        self._checkClosed()
        count = min(len(buffer), max(0, self._size - self._position))
        if not count:
            return 0
        read = self._file.readinto_at(
            self._offset + self._position, memoryview(buffer)[:count]
        )
        if not read:
            raise zipfile.BadZipFile("Truncated compressed payload")
        self._position += read
        return read

    def close(self):
        if not self.closed:
            self._file.close()
        super().close()


def _open_raw(source, info):
    # Follow ZipFile.open's shared-handle lifetime and locking protocol. These
    # private stdlib interactions are confined to this module and tested here.
    source._fileRefCnt += 1
    file = _SourceFile(
        source.fp, info.header_offset, source._fpclose, source._lock,
        lambda: source._writing,
    )
    try:
        offset = _payload_offset(file, info)
        return io.BufferedReader(_RawReader(file, offset, info.compress_size))
    except BaseException:
        file.close()
        raise


class ZipFile(zipfile.ZipFile):
    """Standard ZipFile with streaming compressed-member composition.

    Parameters
    ----------
    file : str, os.PathLike, or binary file-like object
        Archive path or stream. Raw reading requires a blocking, seekable
        binary stream supporting ``readinto``; writing also supports
        non-seekable streams.
    mode : {'r', 'w', 'x', 'a'}, optional
        Open for reading, overwrite, exclusive creation, or append,
        respectively. Default is 'r'.
    compression : int, optional
        Default compression method for ordinary writes. Default is
        ``zipfile.ZIP_STORED``. Raw writes preserve the supplied method.
    compresslevel : int or None, optional
        Compression level for ordinary writes. Default is None, using the
        codec's default. Raw writes do not use a compression level.
    strict_timestamps : bool, optional
        Enforce the ZIP timestamp range for ordinary filesystem writes. Default
        is True. This is a keyword-only argument.
    metadata_encoding : str or None, optional
        Encoding for member names and comments when reading legacy archives.
        Default is None. This keyword-only argument requires Python 3.11 or
        later, following the standard-library constructor.

    Warnings
    --------
    Raw operations trust supplied ZipInfo objects without checking their source
    archive. Obtain them from the supplied source and preserve their metadata.
    Source and destination files must be distinct. Local metadata agreement is
    not checked. Discard partial output after any failure.

    See Also
    --------
    zipfile.ZipFile : Standard constructor options and ordinary ZIP operations.
    open_raw : Read a member's compressed payload.
    write_raw : Write a compressed payload with supplied metadata.
    copy_member : Copy one member without invoking a codec.
    splice : Relocate subtrees from multiple archives into an open target.

    Notes
    -----
    Regular ``open``, ``write``, ``writestr`` and extraction are inherited. Raw
    methods preserve compression, CRC, timestamps, attributes, comments, and
    unrelated extra fields. ZIP64 fields are rebuilt, and Unicode path extra
    fields are removed on rename to avoid stale filename checksums.
    ZIP64 is always allowed and emitted when sizes or offsets require it.

    Raw operations do not verify payload CRCs. Use the inherited ``testzip``
    method for decompression and CRC validation. Encrypted and patched entries
    are excluded from raw operations.

    Examples
    --------
    Relocate a subtree into an empty ZIP without changing compressed bytes.

    >>> from io import BytesIO
    >>> source = BytesIO()
    >>> with ZipFile(source, 'w') as archive:
    ...     archive.writestr('old/one', b'first')
    ...     archive.writestr('other/two', b'second')
    >>> selected = BytesIO()
    >>> with ZipFile(source) as src, ZipFile(selected, 'w') as dst:
    ...     dst.splice(src, 'new/', select_prefix='old/')
    >>> with ZipFile(selected) as archive:
    ...     archive.namelist()
    ['new/one']

    Supply a pre-partitioned selection to avoid scanning the source directory.

    >>> partitioned = BytesIO()
    >>> with ZipFile(source) as src, ZipFile(partitioned, 'w') as dst:
    ...     infos = [src.getinfo('old/one')]
    ...     dst.splice(src, 'node/', select=infos, select_prefix='old/')
    >>> with ZipFile(partitioned) as archive:
    ...     archive.namelist()
    ['node/one']

    Combine entire archives by using empty source and destination prefixes.

    >>> combined = BytesIO()
    >>> with ZipFile(source) as one, ZipFile(selected) as two:
    ...     with ZipFile(combined, 'w') as dst:
    ...         dst.splice(one)
    ...         dst.splice(two)
    >>> with ZipFile(combined) as archive:
    ...     archive.namelist()
    ['old/one', 'other/two', 'new/one']
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, allowZip64=True, **kwargs)

    def open_raw(self, member):
        """Open a bounded stream of a member's compressed payload.

        Parameters
        ----------
        member : str or zipfile.ZipInfo
            Member name or trusted source metadata. A name selects the last
            occurrence when the source has duplicate names.

        Returns
        -------
        io.BufferedReader
            Seekable binary stream retaining the source file until the stream
            closes.

        Raises
        ------
        KeyError
            The requested member name does not exist.
        ValueError
            The source is closed or has an open writing handle.
        zipfile.BadZipFile
            The local header is truncated or has an invalid signature, or a
            subsequent payload read reaches EOF before the declared compressed
            size.
        NotImplementedError
            The member declares encryption or compressed patches.

        Warnings
        --------
        ZipInfo metadata is trusted without membership or local/central
        agreement checks. Raw input must be a blocking, seekable stream
        supporting ``readinto``.

        See Also
        --------
        write_raw : Write compressed bytes with supplied metadata.
        copy_member : Copy a member while managing the raw stream's lifetime.

        Notes
        -----
        Closing this archive does not invalidate the returned stream. No
        decompression or CRC verification occurs. Local filename and extra
        lengths locate the payload; sizes, CRC and compression method come from
        ZipInfo.
        """
        return _open_raw(self, _info(self, member))

    def copy_member(self, source, member, *, arcname=None):
        """Copy a member from an open ZipFile without using a codec.

        Parameters
        ----------
        source : zipfile.ZipFile
            Open source with blocking, seekable binary storage supporting
            ``readinto``.
        member : str or zipfile.ZipInfo
            Source member name or trusted source metadata.
        arcname : str or None, optional
            Destination name. Default is None, preserving the source name.

        Returns
        -------
        zipfile.ZipInfo
            Destination metadata with the new name and header offset.

        Raises
        ------
        KeyError
            The requested source name does not exist.
        ValueError
            An archive has an incompatible state, sizes are negative, or the
            destination already contains the mapped name.
        zipfile.BadZipFile
            A local header or compressed payload is truncated, or extra fields
            needed to rebuild output metadata are malformed.
        NotImplementedError
            The member declares encryption or compressed patches.

        Warnings
        --------
        The caller must supply correct ZipInfo metadata and distinct
        source/target files. No file-identity checks are made. Discard partial
        output after failure.

        See Also
        --------
        splice : Copy a selection under a destination prefix.
        open_raw : Obtain compressed bytes with retained source ownership.

        Notes
        -----
        Source metadata is not mutated. The temporary raw stream is closed
        here; ``source`` remains open.
        """
        info = _info(source, member)
        with _open_raw(source, info) as stream:
            return self.write_raw(info, stream, arcname=arcname)

    def write_raw(self, info, stream, *, arcname=None):
        """Write declared compressed bytes from a blocking binary stream.

        Parameters
        ----------
        info : zipfile.ZipInfo
            Trusted compression method, compressed size, uncompressed size and
            CRC.
        stream : binary file-like object
            Blocking stream positioned at the compressed payload. Must
            implement ``read(size)`` and honor the requested size limit. It
            remains open and advances by
            ``info.compress_size`` bytes on success; seeking is not required.
        arcname : str or None, optional
            Destination name. Default is None, using ``info.filename``.

        Returns
        -------
        zipfile.ZipInfo
            Destination metadata with rebuilt headers and the new offset.

        Raises
        ------
        ValueError
            The destination is closed, not writable, or has an open writing
            handle; sizes are negative; or a name collides.
        zipfile.BadZipFile
            Extra fields are malformed or the payload stream ends prematurely.
        NotImplementedError
            The metadata declares encryption or compressed patches.

        Warnings
        --------
        The caller supplies correct metadata and a distinct payload source. CRC
        and uncompressed size are not verified. Discard partial output after
        any failure.

        See Also
        --------
        open_raw : Read compressed bytes from an existing member.
        copy_member : Copy a member and manage its raw stream automatically.

        Notes
        -----
        Input metadata is not mutated. Compressed bytes are copied in bounded
        chunks without invoking a codec. ZIP64 fields are rebuilt for the
        destination.
        """
        if not self.fp or self.mode not in ("w", "x", "a"):
            raise ValueError("Destination ZIP is closed or not writable")
        if info.flag_bits & _UNSUPPORTED_FLAGS:
            raise NotImplementedError(
                "Raw copying excludes encryption/patches"
            )
        if info.file_size < 0 or info.compress_size < 0:
            raise ValueError("ZIP member sizes must be nonnegative")
        entry = _destination_info(info, arcname)
        zip64 = max(entry.file_size, entry.compress_size) > zipfile.ZIP64_LIMIT
        with self._lock:
            self._check_raw_write(entry)
            header = entry.FileHeader(zip64)
            self._didModify = True
            self._writing = True
            try:
                self.fp.write(header)
                remaining = entry.compress_size
                while data := stream.read(min(remaining, _CHUNK_SIZE)):
                    self.fp.write(data)
                    remaining -= len(data)
                if remaining:
                    raise zipfile.BadZipFile("Truncated compressed payload")
                self.start_dir = self.fp.tell()
                self.filelist.append(entry)
                self.NameToInfo[entry.filename] = entry
            finally:
                self._writing = False
        return entry

    def _check_raw_write(self, entry):
        if self._writing:
            raise ValueError("Destination ZIP has an open writing handle")
        if self._seekable:
            self.fp.seek(self.start_dir)
        entry.header_offset = self.fp.tell()
        if entry.filename in self.NameToInfo:
            raise ValueError(f"Duplicate member: {entry.filename!r}")

    def splice(self, source, dst="", *, select=None, select_prefix=""):
        """Stream selected source entries under a destination prefix.

        Parameters
        ----------
        source : zipfile.ZipFile
            Open source with blocking, seekable binary storage supporting
            ``readinto``.
        dst : str, optional
            Destination directory prefix. Default is '', the ZIP root.
        select : iterable of zipfile.ZipInfo or None, optional
            Trusted source entries, consumed in supplied order. Default is
            None, iterating the source directory. Lists and generators are
            supported; an empty selection copies nothing.
        select_prefix : str, optional
            Source prefix to filter and remove before prepending ``dst``.
            Default is '', preserving full source paths. A trailing slash is
            optional for both prefixes. With an explicit selection, only those
            entries are examined. The prefix must be strictly shorter than the
            matching filename; the selected directory's own entry is excluded.

        Raises
        ------
        TypeError
            An explicit selection contains something other than ZipInfo
            objects.
        ValueError
            A prefix is invalid, an archive has an incompatible state, or a
            mapped name already exists in the destination.
        zipfile.BadZipFile
            A header or payload is truncated, or output extra fields are
            malformed.
        NotImplementedError
            A copied member declares encryption or compressed patches.

        Warnings
        --------
        Callers must supply correct source metadata and distinct source/target
        files. Ownership and file identity are not checked. Entries are copied
        immediately; errors can leave a partially written target, which the
        caller must discard. Resolve duplicate winners and whiteouts before
        calling this method.

        See Also
        --------
        copy_member : Copy a single entry with an explicit destination name.
        open_raw : Read a bounded compressed member stream.

        Notes
        -----
        Prefixes respect directory boundaries. Only entries beneath the source
        prefix are copied, excluding the selected directory's own entry for any
        destination. Directory entries beneath it are preserved, including
        empty directories. Unmatched prefixes copy nothing. Selection and
        prefix filtering combine by intersection.

        There is no preflight plan, replacement policy, source index
        construction or scan of the target entry list. Collision detection uses
        the target name index as each entry is written. Repeated calls combine
        distinct selections into one open target; its archive comment and
        existing entries are preserved.
        """
        selected = _selection(source, select)
        select_prefix, dst = _prefix(select_prefix), _prefix(dst)
        for info in selected:
            if not isinstance(info, zipfile.ZipInfo):
                raise TypeError("Explicit selections require ZipInfo objects")
            if (
                info.filename.startswith(select_prefix)
                and info.filename != select_prefix
            ):
                name = dst + info.filename[len(select_prefix):]
                self.copy_member(source, info, arcname=name)


def _prefix(value):
    if not isinstance(value, str):
        raise TypeError("ZIP prefixes must be strings")
    if value == "":
        return value
    parts = (value[:-1] if value.endswith("/") else value).split("/")
    if "\\" in value or "\x00" in value or any(
        part in ("", ".", "..") for part in parts
    ):
        raise ValueError("ZIP prefixes must be relative directory paths")
    return "/".join(parts) + "/"


def _selection(source, select):
    if select is None:
        return source.infolist()
    if not isinstance(select, (str, bytes)):
        try:
            return iter(select)
        except TypeError:
            pass
    raise TypeError("select requires an iterable of ZipInfo objects or None")
