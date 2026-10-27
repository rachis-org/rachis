# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
import copy
import io
import struct
import warnings
import zipfile

import pytest

from rachis.core.zip import ZipFile


METHODS = [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
           zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]
PAYLOAD = bytes(range(256)) * 4096


def raw_payload(archive, info):
    """Independent inspection of compressed bytes using the local header."""
    position = archive.fp.tell()
    try:
        archive.fp.seek(info.header_offset)
        header = archive.fp.read(30)
        name_size, extra_size = struct.unpack_from("<HH", header, 26)
        archive.fp.seek(name_size + extra_size, io.SEEK_CUR)
        return archive.fp.read(info.compress_size)
    finally:
        archive.fp.seek(position)


def fields(extra):
    result = {}
    while extra:
        tag, size = struct.unpack_from("<HH", extra)
        result[tag] = extra[4:4 + size]
        extra = extra[4 + size:]
    return result


def local_header(archive, info):
    position = archive.fp.tell()
    try:
        archive.fp.seek(info.header_offset)
        header = archive.fp.read(30)
        name_size, extra_size = struct.unpack_from("<HH", header, 26)
        archive.fp.read(name_size)
        return header, fields(archive.fp.read(extra_size))
    finally:
        archive.fp.seek(position)


def source_zip(method=zipfile.ZIP_DEFLATED, *, descriptor=False, zip64=False):
    buffer = io.BytesIO()
    target = NonSeekable(buffer) if descriptor else buffer
    with zipfile.ZipFile(target, "w", compression=method) as archive:
        info = zipfile.ZipInfo("original/ünicode.bin", (2020, 2, 3, 4, 5, 6))
        info.compress_type = method
        info.external_attr = 0o100640 << 16
        info.internal_attr = 1
        info.comment = b"member comment"
        info.extra = struct.pack("<HH4s", 0xCAFE, 4, b"data")
        with archive.open(info, "w", force_zip64=zip64) as writer:
            writer.write(PAYLOAD)
        archive.writestr("empty", b"")
        archive.writestr("folder/", b"")
        archive.writestr("other", b"other contents")
        archive.comment = b"archive comment"
    buffer.seek(0)
    return buffer


class NonSeekable:
    def __init__(self, buffer):
        self.buffer = buffer

    def write(self, data):
        return self.buffer.write(data)

    def flush(self):
        self.buffer.flush()


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("descriptor", [False, True])
@pytest.mark.parametrize("zip64", [False, True])
@pytest.mark.parametrize("selection_style", ["prefix", "infos"])
def test_splice_preserves_compressed_bytes_and_metadata(
    method, descriptor, zip64, selection_style
):
    buffer = source_zip(method, descriptor=descriptor, zip64=zip64)
    target = io.BytesIO()
    with ZipFile(buffer) as source:
        original = source.getinfo("original/ünicode.bin")
        before = raw_payload(source, original)
        original_state = (original.header_offset, original.flag_bits,
                          original.extra, original.filename)
        with ZipFile(target, "w") as output:
            output.comment = b"target comment"
            output.splice(
                source, "relocated/",
                select=None if selection_style == "prefix" else [original],
                select_prefix="original/",
            )
        assert original_state == (
            original.header_offset, original.flag_bits,
            original.extra, original.filename,
        )
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        copied = result.getinfo("relocated/ünicode.bin")
        assert result.namelist() == ["relocated/ünicode.bin"]
        assert result.read(copied) == PAYLOAD
        assert raw_payload(result, copied) == before
        assert result.testzip() is None
        assert result.comment == b"target comment"
        for attribute in ("date_time", "compress_type", "compress_size",
                          "file_size", "CRC", "external_attr", "internal_attr",
                          "comment", "create_system"):
            assert getattr(copied, attribute) == getattr(original, attribute)
        assert fields(copied.extra)[0xCAFE] == b"data"
        assert copied.flag_bits & 8 == 0


def test_copy_uses_no_compressor_or_decompressor(monkeypatch):
    source = source_zip()

    def forbidden(*args, **kwargs):
        raise AssertionError("A raw copy must not use any codec")

    with monkeypatch.context() as patch:
        patch.setattr(zipfile, "_get_compressor", forbidden)
        patch.setattr(zipfile, "_get_decompressor", forbidden)
        with ZipFile(source) as archive:
            target = io.BytesIO()
            with ZipFile(target, "w") as destination:
                destination.splice(archive)
            union = io.BytesIO()
            with ZipFile(target) as src, ZipFile(union, "w") as dst:
                dst.splice(src)
    union.seek(0)
    with zipfile.ZipFile(union) as archive:
        assert archive.read("original/ünicode.bin") == PAYLOAD


def test_raw_stream_can_be_copied_after_source_closes():
    source = ZipFile(source_zip())
    info = source.getinfo("original/ünicode.bin")
    expected = raw_payload(source, info)
    stream = source.open_raw(info)
    source.close()
    assert stream.read(5) == expected[:5]
    stream.seek(-3, io.SEEK_END)
    assert stream.read() == expected[-3:]
    stream.seek(0)
    target = io.BytesIO()
    with stream, ZipFile(target, "w") as output:
        output.write_raw(info, stream, arcname="new")
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.read("new") == PAYLOAD
        assert raw_payload(result, result.getinfo("new")) == expected


@pytest.mark.parametrize("sentinels", [
    (True, False, False), (False, True, False), (False, False, True),
    (True, True, True),
])
def test_central_zip64_conditional_sizes_and_offsets(sentinels):
    """Real ZIP64 sentinels with small data instead of a 4 GiB fixture."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as src:
        src.writestr("member", b"contents" * 100)
    data = bytearray(buffer.getvalue())
    central = data.index(b"PK\x01\x02")
    end = data.index(b"PK\x05\x06", central)
    record = bytearray(data[central:end])
    values = []
    # ZIP64 field order is size, compressed size, header offset. A field is
    # present only when its corresponding central-header value is a sentinel.
    for replace, offset in zip(sentinels, [24, 20, 42]):
        if replace:
            values.append(struct.unpack_from("<I", record, offset)[0])
            struct.pack_into("<I", record, offset, 0xFFFFFFFF)
    extra = struct.pack("<HH", 1, 8 * len(values))
    extra += struct.pack("<" + "Q" * len(values), *values)
    struct.pack_into("<H", record, 6, 45)
    struct.pack_into("<H", record, 30, len(extra))
    record += extra
    ending = bytearray(data[end:])
    struct.pack_into("<I", ending, 12, len(record))
    rebuilt = io.BytesIO(data[:central] + record + ending)
    output = io.BytesIO()
    with ZipFile(rebuilt) as source:
        assert source.read("member") == b"contents" * 100
        original = raw_payload(source, source.infolist()[0])
        with ZipFile(output, "w") as destination:
            destination.splice(source)
    output.seek(0)
    with zipfile.ZipFile(output) as result:
        assert result.read("member") == b"contents" * 100
        info = result.infolist()[0]
        assert raw_payload(result, info) == original
        assert 1 not in fields(info.extra)


def test_zip64_destination_sizes_offsets_and_end_records(monkeypatch):
    # Exercise stdlib's real ZIP64 write/read branches without allocating GiB.
    buffer = source_zip()
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 64)
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 2)
    target = io.BytesIO()
    with ZipFile(buffer) as source:
        with ZipFile(target, "w") as destination:
            destination.splice(source)
    data = target.getvalue()
    assert b"PK\x06\x06" in data
    assert b"PK\x06\x07" in data
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        first, second = result.infolist()[:2]
        header, extra = local_header(result, first)
        assert struct.unpack_from("<II", header, 18) == (
            0xFFFFFFFF, 0xFFFFFFFF
        )
        assert struct.unpack("<QQ", extra[1]) == (
            first.file_size, first.compress_size
        )
        assert first.extract_version >= 45
        assert struct.unpack_from("<QQ", fields(first.extra)[1]) == (
            first.file_size, first.compress_size
        )
        assert struct.unpack("<Q", fields(second.extra)[1]) == (
            second.header_offset,
        )
        assert result.testzip() is None


def test_actual_zip64_offset_in_sparse_file(tmp_path):
    path = tmp_path / "sparse.zip"
    with path.open("w+b") as buffer:
        buffer.seek(0x100000000 + 123)
        with zipfile.ZipFile(buffer, "w") as source:
            source.writestr("tail", b"small payload")
    target = io.BytesIO()
    with ZipFile(path) as source:
        assert source.getinfo("tail").header_offset > 0xFFFFFFFF
        with ZipFile(target, "w") as destination:
            destination.splice(source)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.read("tail") == b"small payload"
        assert result.getinfo("tail").header_offset == 0
        assert 1 not in fields(result.getinfo("tail").extra)


def test_raw_writer_preserves_sizes_above_uint32():
    # A raw writer trusts caller-supplied metadata. Test its 64-bit encoding
    # separately from decompression; this is not a valid deflate payload for
    # the declared uncompressed size, and is deliberately not read that way.
    info = zipfile.ZipInfo("large")
    info.compress_type = zipfile.ZIP_DEFLATED
    info.file_size = 0x100000000 + 123
    info.compress_size = 2
    info.CRC = 0
    target = io.BytesIO()
    with ZipFile(target, "w") as output:
        output.write_raw(info, io.BytesIO(b"\x03\x00"))
    target.seek(0)
    with ZipFile(target) as result:
        entry = result.getinfo("large")
        assert entry.file_size == info.file_size
        header, extra = local_header(result, entry)
        assert struct.unpack_from("<II", header, 18) == (
            0xFFFFFFFF, 0xFFFFFFFF
        )
        assert struct.unpack("<QQ", extra[1]) == (info.file_size, 2)
        assert struct.unpack("<QQ", fields(entry.extra)[1]) == (
            info.file_size, 2
        )
        with result.open_raw(entry) as stream:
            assert stream.read() == b"\x03\x00"


def test_raw_writer_at_actual_zip64_offset(tmp_path):
    path = tmp_path / "sparse-output.zip"
    offset = 0x100000000 + 123
    with ZipFile(source_zip()) as source, path.open("w+b") as buffer:
        buffer.seek(offset)
        with ZipFile(buffer, "w") as output:
            output.copy_member(source, "other", arcname="relocated")
    with zipfile.ZipFile(path) as result:
        entry = result.getinfo("relocated")
        assert entry.header_offset == offset
        assert struct.unpack("<Q", fields(entry.extra)[1]) == (offset,)
        assert result.read(entry) == b"other contents"



@pytest.mark.parametrize("winner", ["first", "last"])
def test_callsite_selects_duplicate_winner_before_splicing(winner):
    first = io.BytesIO()
    second = io.BytesIO()
    with zipfile.ZipFile(first, "w", compression=zipfile.ZIP_DEFLATED) as src:
        src.writestr("same", b"first")
        src.writestr("a", b"a")
    with zipfile.ZipFile(second, "w", compression=zipfile.ZIP_BZIP2) as src:
        src.writestr("same", b"last")
        src.writestr("b", b"b")
    output = io.BytesIO()
    with ZipFile(first) as one, ZipFile(second) as two:
        with ZipFile(output, "w") as dst:
            chosen = one if winner == "first" else two
            dst.splice(chosen, select=[chosen.getinfo("same")])
            dst.splice(one, select=[one.getinfo("a")])
            dst.splice(two, select=[two.getinfo("b")])
        compressed = raw_payload(chosen, chosen.getinfo("same"))
    output.seek(0)
    with zipfile.ZipFile(output) as result:
        assert result.namelist() == ["same", "a", "b"]
        assert result.read("same") == winner.encode()
        assert raw_payload(result, result.getinfo("same")) == compressed



def test_collision_on_first_entry_does_not_add_an_entry():
    target = io.BytesIO()
    with ZipFile(source_zip()) as source, ZipFile(target, "w") as output:
        output.writestr("relocated/ünicode.bin", b"untouched")
        with pytest.raises(ValueError, match="Duplicate"):
            output.splice(source, "relocated/", select_prefix="original/")
        assert output.namelist() == ["relocated/ünicode.bin"]
        assert output.read("relocated/ünicode.bin") == b"untouched"




def test_append_and_mix_raw_and_regular_writes(tmp_path):
    target = tmp_path / "output.zip"
    with ZipFile(target, "w") as output:
        output.writestr("before", b"before")
    with ZipFile(target, "a") as output, zipfile.ZipFile(source_zip()) as src:
        output.splice(src, "moved/", select_prefix="original/")
        output.writestr("after", b"after")
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == ["before", "moved/ünicode.bin", "after"]
        assert result.testzip() is None


def test_raw_stream_bounds_and_independent_positions():
    with ZipFile(source_zip()) as source:
        info = source.infolist()[0]
        compressed = raw_payload(source, info)
        with source.open_raw(info) as one, source.open_raw(info) as two:
            assert one.read(4) == compressed[:4]
            assert two.read(7) == compressed[:7]
            assert one.read() == compressed[4:]
            assert one.read() == b""
            one.seek(10, io.SEEK_END)
            assert one.read() == b""
            with pytest.raises(ValueError, match="Negative"):
                one.seek(-1)
            with pytest.raises(ValueError):
                one.seek(0, 123)
            one.seek(0)
            assert one.read() == compressed
            assert source.read("other") == b"other contents"



@pytest.mark.parametrize("mutation", ["signature", "truncated"])
def test_invalid_or_truncated_fixed_local_header_is_rejected(mutation):
    with ZipFile(source_zip()) as source:
        info = source.infolist()[0]
        source.fp.seek(info.header_offset)
        if mutation == "signature":
            source.fp.write(b"bad!")
        else:
            source.fp.truncate(info.header_offset + 20)
        with pytest.raises(zipfile.BadZipFile):
            source.open_raw(info)
        assert source._fileRefCnt == 1


@pytest.mark.parametrize("mutation", [
    "filename", "method", "size", "crc", "zip64", "extra",
])
def test_copy_trusts_central_metadata_and_skips_local_metadata(mutation):
    data = bytearray(source_zip(zip64=mutation == "zip64").getvalue())
    nlen = struct.unpack_from("<H", data, 26)[0]
    if mutation == "filename":
        data[30] = ord("x")
    elif mutation == "method":
        struct.pack_into("<H", data, 8, zipfile.ZIP_STORED)
    elif mutation == "size":
        struct.pack_into("<I", data, 18, 123)
    elif mutation == "crc":
        struct.pack_into("<I", data, 14, 123)
    elif mutation == "zip64":
        struct.pack_into("<H", data, 30 + nlen + 8, 0xCAFF)
    elif mutation == "extra":
        struct.pack_into("<H", data, 30 + nlen + 2, 0xFFFF)
    target = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as source, ZipFile(target, "w") as output:
        info = source.infolist()[0]
        compressed = raw_payload(source, info)
        output.splice(source, select=[info])
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        copied = result.infolist()[0]
        assert raw_payload(result, copied) == compressed
        assert result.read(copied) == PAYLOAD
        assert result.testzip() is None


@pytest.mark.parametrize("flag", [1, 32, 64, 8192])
def test_encryption_and_patch_flags_rejected(flag):
    with ZipFile(source_zip()) as source:
        info = source.infolist()[0]
        info.flag_bits |= flag
        with pytest.raises(NotImplementedError):
            source.open_raw(info)


def test_truncated_raw_input():
    with ZipFile(source_zip()) as source:
        info = source.infolist()[0]
        with ZipFile(io.BytesIO(), "w") as output:
            with pytest.raises(zipfile.BadZipFile, match="Truncated"):
                output.write_raw(info, io.BytesIO(b""))


def test_open_write_handles_and_closed_sources():
    with ZipFile(source_zip()) as source:
        with ZipFile(io.BytesIO(), "w") as output:
            with output.open("busy", "w"):
                with pytest.raises(ValueError, match="writing handle"):
                    output.copy_member(source, "empty")
    with pytest.raises(ValueError, match="closed"):
        source.open_raw("empty")


def test_empty_splice():
    target = io.BytesIO()
    with ZipFile(source_zip()) as source, ZipFile(target, "w") as output:
        output.splice(source, select_prefix="missing/")
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == []
    empty = io.BytesIO()
    with ZipFile(empty, "w"):
        pass
    target = io.BytesIO()
    with ZipFile(empty) as source, ZipFile(target, "w") as output:
        output.splice(source)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == []


@pytest.mark.parametrize("arcname", [None, "old", "new"])
def test_unicode_path_extra_is_removed_only_on_rename(arcname):
    buffer = io.BytesIO()
    info = zipfile.ZipInfo("old")
    info.extra = struct.pack("<HH5s", 0x7075, 5, b"\x01abcd")
    with zipfile.ZipFile(buffer, "w") as src:
        src.writestr(info, b"contents")
    target = io.BytesIO()
    with ZipFile(buffer) as src, ZipFile(target, "w") as output:
        output.copy_member(src, "old", arcname=arcname)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        name = "old" if arcname is None else arcname
        if name == "old":
            assert result.getinfo(name).extra == info.extra
        else:
            assert 0x7075 not in fields(result.getinfo(name).extra)
        assert result.read(name) == b"contents"


def test_file_backed_raw_stream_retains_file_until_last_close(tmp_path):
    path = tmp_path / "source.zip"
    path.write_bytes(source_zip().getvalue())
    source = ZipFile(path)
    file = source.fp
    one = source.open_raw("original/ünicode.bin")
    two = source.open_raw("original/ünicode.bin")
    source.close()
    assert not file.closed
    assert one.read(10) == two.read(10)
    one.close()
    assert not file.closed
    assert two.read()
    two.close()
    assert file.closed


@pytest.mark.parametrize("zip64", [False, True])
def test_descriptor_without_signature(zip64):
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        NonSeekable(buffer), "w", compression=zipfile.ZIP_DEFLATED
    ) as source:
        with source.open("member", "w", force_zip64=zip64) as writer:
            writer.write(b"contents" * 100)
    data = bytearray(buffer.getvalue())
    descriptor = data.index(b"PK\x07\x08")
    del data[descriptor:descriptor + 4]
    end = data.index(b"PK\x05\x06")
    central_offset = struct.unpack_from("<I", data, end + 16)[0]
    struct.pack_into("<I", data, end + 16, central_offset - 4)
    output = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as source:
        assert source.read("member") == b"contents" * 100
        with ZipFile(output, "w") as destination:
            destination.splice(source)
    output.seek(0)
    with zipfile.ZipFile(output) as result:
        assert result.read("member") == b"contents" * 100


def test_streaming_is_bounded_and_handles_short_reads(monkeypatch):
    # Make many chunks observable without a large test fixture.
    import rachis.core.zip as rawzip

    monkeypatch.setattr(rawzip, "_CHUNK_SIZE", 1024)

    class ShortReader(io.BytesIO):
        def read(self, count=-1):
            assert 0 <= count <= 1024
            return super().read(min(count, 100))

    target = io.BytesIO()
    with zipfile.ZipFile(source_zip(zipfile.ZIP_STORED)) as source:
        info = source.infolist()[0]
        stream = ShortReader(raw_payload(source, info) + b"following bytes")
        with ZipFile(target, "w") as output:
            output.write_raw(info, stream)
        assert stream.tell() == info.compress_size
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.read(info.filename) == PAYLOAD



def test_callsite_excludes_original_when_selecting_zero_byte_replacement():
    original = io.BytesIO()
    replacement = io.BytesIO()
    with zipfile.ZipFile(original, "w") as source:
        source.writestr("member", b"contents")
        source.writestr("keep", b"keep")
    with zipfile.ZipFile(replacement, "w") as source:
        source.writestr("member", b"")
    target = io.BytesIO()
    with ZipFile(original) as one, ZipFile(replacement) as two:
        with ZipFile(target, "w") as output:
            output.splice(one, select=[one.getinfo("keep")])
            output.splice(two)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == ["keep", "member"]
        assert result.read("member") == b""
        assert result.read("keep") == b"keep"


def test_duplicate_source_entries_can_be_selected_by_identity():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as source:
        source.writestr("same", b"first")
        with pytest.warns(UserWarning, match="Duplicate"):
            source.writestr("same", b"last")
    with ZipFile(buffer) as source:
        target = io.BytesIO()
        with ZipFile(target, "w") as output:
            output.copy_member(source, source.infolist()[0])
        target.seek(0)
        with zipfile.ZipFile(target) as result:
            assert result.read("same") == b"first"
        target = io.BytesIO()
        with ZipFile(target, "w") as output:
            output.splice(source, select=[source.infolist()[-1]])
        target.seek(0)
        with zipfile.ZipFile(target) as result:
            assert result.read("same") == b"last"


def test_raw_copy_does_not_require_codec_support():
    buffer = io.BytesIO()
    info = zipfile.ZipInfo("opaque")
    info.compress_type = 200  # A method unavailable to Python's codecs.
    info.file_size = 100
    info.compress_size = 6
    info.CRC = 123
    with ZipFile(buffer, "w") as output:
        output.write_raw(info, io.BytesIO(b"opaque"))
    target = io.BytesIO()
    with ZipFile(buffer) as source:
        with ZipFile(target, "w") as destination:
            destination.splice(source)
    target.seek(0)
    with ZipFile(target) as result:
        assert result.getinfo("opaque").compress_type == 200
        with result.open_raw("opaque") as stream:
            assert stream.read() == b"opaque"
        with pytest.raises(NotImplementedError):
            result.read("opaque")


def test_cp437_filename_reencoded_consistently():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as source:
        source.writestr("é", b"contents")
    data = bytearray(buffer.getvalue())
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, 6, 0)
    struct.pack_into("<H", data, central + 8, 0)
    data[30:32] = b"\x82x"
    data[central + 46:central + 48] = b"\x82x"
    target = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as source:
        assert source.namelist() == ["éx"]
        with ZipFile(target, "w") as destination:
            destination.splice(source)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == ["éx"]
        assert result.read("éx") == b"contents"
        assert result.getinfo("éx").flag_bits & 0x800


def test_splice_file_sources_into_nonseekable_destination(tmp_path):
    paths = []
    for name in ["one", "two"]:
        path = tmp_path / (name + ".zip")
        with zipfile.ZipFile(path, "w") as source:
            source.writestr(name, name.encode())
        paths.append(path)
    target = io.BytesIO()
    with ZipFile(NonSeekable(target), "w") as output:
        for path in paths:
            with zipfile.ZipFile(path) as source:
                output.splice(source)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == ["one", "two"]
        assert result.testzip() is None


@pytest.mark.parametrize("select", ["src", "src/"])
@pytest.mark.parametrize("dst", ["dst", "dst/", ""])
@pytest.mark.parametrize("explicit_selection", [False, True])
def test_splice_prefix_boundaries_and_directory_entries(
    select, dst, explicit_selection
):
    buffer = io.BytesIO()
    entries = {
        "src/": b"",
        "src/file": b"file contents",
        "src/nested/": b"",
        "src/nested/file": b"nested contents",
        "src/empty": b"",
        "src/empty-directory/": b"",
        "src": b"a sibling file with the same name as the prefix",
        "src-other/file": b"outside the selected directory",
        "elsewhere/src/file": b"outside the selected directory",
    }
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as src:
        for name, data in entries.items():
            src.writestr(name, data)
    target = io.BytesIO()
    prefix = "dst/" if dst else ""
    with zipfile.ZipFile(buffer) as source, ZipFile(target, "w") as output:
        output.writestr("existing", b"existing")
        output.splice(
            source, dst, select_prefix=select,
            select=source.infolist() if explicit_selection else None,
        )
        assert source.fp is not None and output.fp is not None
        expected = ["existing"] + [
            prefix + name[4:] for name in entries
            if name.startswith("src/") and name != "src/"
        ]
        assert output.namelist() == expected
        for name in expected[1:]:
            original = "src/" + name[len(prefix):]
            assert raw_payload(output, output.getinfo(name)) == raw_payload(
                source, source.getinfo(original)
            )
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == expected
        assert result.read(prefix + "file") == b"file contents"
        assert result.read(prefix + "nested/file") == b"nested contents"
        assert result.read(prefix + "empty") == b""
        assert result.getinfo(prefix + "empty-directory/").is_dir()
        assert result.testzip() is None


def test_splice_many_sources_and_repeat_source_under_distinct_prefixes():
    target = io.BytesIO()
    with ZipFile(target, "w") as output:
        for i in range(10):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as src:
                src.writestr("src/file", str(i).encode())
                src.writestr("excluded", b"outside")
            with zipfile.ZipFile(buffer) as source:
                output.splice(source, f"{i}/", select_prefix="src/")
                output.splice(source, f"copy/{i}/", select_prefix="src/")
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert len(result.infolist()) == 20
        for i in range(10):
            assert result.read(f"{i}/file") == str(i).encode()
            assert result.read(f"copy/{i}/file") == str(i).encode()
        assert result.testzip() is None


def test_splice_collision_leaves_already_copied_entries():
    one = io.BytesIO()
    two = io.BytesIO()
    with zipfile.ZipFile(one, "w") as src:
        src.writestr("left/same", b"first")
    with zipfile.ZipFile(two, "w") as src:
        # Earlier entries are copied before a later collision fails.
        src.writestr("right/new", b"new")
        src.writestr("right/same", b"last")
    target = io.BytesIO()
    with ZipFile(target, "w") as output:
        with zipfile.ZipFile(one) as source:
            output.splice(source, "dst/", select_prefix="left/")
        before = target.getvalue()
        with zipfile.ZipFile(two) as source:
            with pytest.raises(ValueError, match="Duplicate"):
                output.splice(source, "dst/", select_prefix="right/")
        assert target.getvalue() != before
        assert output.namelist() == ["dst/same", "dst/new"]
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.read("dst/same") == b"first"


@pytest.mark.parametrize("prefix", [
    "/absolute/", "./relative/", "../parent/", "a/../b/", "a//b/",
    "a//", "a\\b/", "a\x00b/",
])
@pytest.mark.parametrize("argument", ["dst", "select_prefix"])
def test_splice_rejects_invalid_prefixes(prefix, argument):
    target = io.BytesIO()
    with ZipFile(source_zip()) as source, ZipFile(target, "w") as output:
        before = target.getvalue()
        with pytest.raises(ValueError, match="relative directory"):
            output.splice(source, **{argument: prefix})
        assert output.namelist() == []
        assert target.getvalue() == before



def test_splice_checks_prefix_types_and_open_write_handles():
    with ZipFile(source_zip()) as source, ZipFile(io.BytesIO(), "w") as target:
        for argument in ["dst", "select_prefix"]:
            with pytest.raises(TypeError, match="string"):
                target.splice(source, **{argument: None})
        with target.open("busy", "w"):
            with pytest.raises(ValueError, match="writing handle"):
                target.splice(source)
    with ZipFile(io.BytesIO(), "w") as target:
        with pytest.raises(ValueError, match="Source ZIP is closed"):
            target.splice(source)
    with ZipFile(source_zip()) as source:
        with pytest.raises(ValueError, match="closed"):
            target.splice(source)
        with ZipFile(source_zip()) as other:
            with pytest.raises(ValueError, match="not writable"):
                source.splice(other)
    with ZipFile(io.BytesIO(), "w") as source:
        source.writestr("existing", b"contents")
        with ZipFile(io.BytesIO(), "w") as target:
            with source.open("busy", "w"):
                with pytest.raises(ValueError, match="writing handle"):
                    target.splice(source)



def test_splice_consumes_selection_as_entries_are_written():
    target = io.BytesIO()
    with ZipFile(source_zip()) as source, ZipFile(target, "w") as output:
        def selection():
            yield source.getinfo("empty")
            assert output.namelist() == ["dst/empty"]
            yield source.getinfo("other")

        output.splice(source, "dst/", select=selection())
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == ["dst/empty", "dst/other"]
        assert result.testzip() is None


def test_truncated_source_payload_is_detected_and_stream_released():
    with ZipFile(source_zip()) as source:
        info = source.getinfo("original/ünicode.bin")
        with source.open_raw(info) as raw:
            payload_end = raw.raw._offset + info.compress_size
        source.fp.truncate(payload_end - 1)
        with ZipFile(io.BytesIO(), "w") as target:
            with pytest.raises(zipfile.BadZipFile, match="Truncated"):
                target.copy_member(source, info)
            assert source._fileRefCnt == 1
            assert not target._writing


def test_splice_rejects_collision_with_ordinary_write():
    with ZipFile(source_zip()) as source:
        target = io.BytesIO()
        with ZipFile(target, "w") as output:
            output.writestr("empty", b"existing")
            with pytest.raises(ValueError, match="Duplicate"):
                output.splice(source, select=[source.getinfo("empty")])
            assert output.namelist() == ["empty"]
            assert output.read("empty") == b"existing"


def test_splice_does_not_read_unselected_encrypted_members():
    buffer = source_zip(zip64=True)
    target = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(target, "w") as output:
        source.getinfo("other").flag_bits |= 1
        output.splice(source, "pack/", select_prefix="original/")
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == ["pack/ünicode.bin"]
        assert result.read("pack/ünicode.bin") == PAYLOAD


def test_splice_from_open_stdlib_writer_and_read_target_before_close():
    source_buffer = io.BytesIO()
    target_buffer = io.BytesIO()
    with zipfile.ZipFile(source_buffer, "w") as source:
        source.writestr("src/ünicode", b"contents")
        with ZipFile(target_buffer, "w") as target:
            target.writestr("padding", b"padding" * 100)
            target.splice(source, "dst/", select_prefix="src/")
            assert target.read("dst/ünicode") == b"contents"
            with target.open_raw("dst/ünicode") as stream:
                assert stream.read() == b"contents"
    target_buffer.seek(0)
    with zipfile.ZipFile(target_buffer) as result:
        assert result.testzip() is None


def test_copy_older_duplicate_infos_does_not_rescan_for_each_member():
    class CountedDirectory(list):
        scans = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        for i in range(100):
            writer.writestr(str(i), b"first")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                writer.writestr(str(i), b"last")
    target = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(target, "w") as output:
        selected = source.infolist()[::2]
        source.filelist = CountedDirectory(source.filelist)
        for info in selected:
            output.copy_member(source, info)
        assert source.filelist.scans == 0
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert len(result.infolist()) == 100
        assert all(result.read(info) == b"first" for info in result.infolist())


@pytest.mark.parametrize("container", [list, tuple, iter])
def test_splice_explicit_selection_order_names_and_empty_entries(container):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("src/a", b"a")
        writer.writestr("src/empty", b"")
        writer.writestr("src/nested/", b"")
        writer.writestr("elsewhere/b", b"b")
    target = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(target, "w") as output:
        selected = container([
            source.getinfo("elsewhere/b"), source.getinfo("src/nested/"),
            source.getinfo("src/empty"),
        ])
        output.splice(source, "dst/", select=selected)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == [
            "dst/elsewhere/b", "dst/src/nested/", "dst/src/empty"
        ]
        assert result.read("dst/elsewhere/b") == b"b"
        assert result.getinfo("dst/src/nested/").is_dir()
        assert result.read("dst/src/empty") == b""


def test_splice_one_thousand_partitions_without_source_or_target_scans(
    monkeypatch
):
    class CountedDirectory(list):
        scans = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    def forbidden():
        raise AssertionError("Explicit selection must not scan the source")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        for i in range(1000):
            writer.writestr(f"group/{i}/file", str(i).encode())
    target = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(target, "w") as output:
        # Partition once; no source directory access should be necessary while
        # splicing those partitions into unrelated output paths.
        selected = source.infolist()[:]
        source.filelist = CountedDirectory(source.filelist)
        output.filelist = CountedDirectory(output.filelist)
        with monkeypatch.context() as patch:
            patch.setattr(source, "infolist", forbidden)
            for i, info in enumerate(selected):
                output.splice(
                    source, f"out/{i}/", select=[info],
                    select_prefix=f"group/{i}/",
                )
        assert source.filelist.scans == 0
        assert output.filelist.scans == 0
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert len(result.infolist()) == 1000
        for i in range(1000):
            assert result.read(f"out/{i}/file") == str(i).encode()


def test_splice_invalid_selection_leaves_previous_entries():
    target = io.BytesIO()
    with ZipFile(source_zip()) as source:
        with ZipFile(target, "w") as output:
            output.writestr("existing", b"untouched")
            before = target.getvalue()
            selected = [source.getinfo("empty"), "other"]
            with pytest.raises(TypeError, match="ZipInfo"):
                output.splice(source, "dst/", select=selected)
            assert target.getvalue() != before
            assert output.namelist() == ["existing", "dst/empty"]


def test_splice_explicit_selection_and_prefix_combine_by_intersection():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("src/a", b"inside")
        writer.writestr("src-other/b", b"outside")
        writer.writestr("src/unselected", b"unselected")
    target = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(target, "w") as output:
        selected = [source.getinfo("src/a"), source.getinfo("src-other/b")]
        output.splice(source, "dst/", select=selected, select_prefix="src/")
        assert output.namelist() == ["dst/a"]
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.read("dst/a") == b"inside"


def test_splice_explicit_duplicate_infos_choose_exact_occurrence():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("same", b"first")
        with pytest.warns(UserWarning, match="Duplicate"):
            writer.writestr("same", b"last")
    target = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(target, "w") as output:
        first, last = source.infolist()
        output.splice(source, "first/", select=[first])
        output.splice(source, "last/", select=[last])
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.read("first/same") == b"first"
        assert result.read("last/same") == b"last"


def test_splice_explicit_empty_selection_preserves_existing_target():
    target = io.BytesIO()
    with ZipFile(source_zip()) as source, ZipFile(target, "w") as output:
        output.writestr("existing", b"untouched")
        before = target.getvalue()
        output.splice(source, select=[])
        assert target.getvalue() == before
        assert output.namelist() == ["existing"]


def test_splice_explicit_selection_with_and_without_prefix_removal():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("src/nested/a", b"a")
        writer.writestr("src/other", b"excluded")
    target = io.BytesIO()
    with ZipFile(buffer) as source, ZipFile(target, "w") as output:
        selected = [source.getinfo("src/nested/a")]
        output.splice(source, "dst/", select=selected, select_prefix="src/")
        output.splice(source, "full/", select=selected)
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.namelist() == ["dst/nested/a", "full/src/nested/a"]
        assert result.testzip() is None


@pytest.mark.parametrize("selection", [123, "src/", b"src/", [1], ["empty"]])
def test_splice_rejects_invalid_explicit_selections(selection):
    target = io.BytesIO()
    with ZipFile(source_zip()) as source, ZipFile(target, "w") as output:
        before = target.getvalue()
        with pytest.raises(TypeError, match="ZipInfo"):
            output.splice(source, select=selection)
        assert output.namelist() == []
        assert target.getvalue() == before


@pytest.mark.parametrize("metadata_source", ["copy", "matching_foreign"])
def test_supplied_zipinfo_metadata_is_trusted(metadata_source):
    target = io.BytesIO()
    with ZipFile(source_zip()) as source:
        original = source.getinfo("original/ünicode.bin")
        if metadata_source == "copy":
            info = copy.copy(original)
        else:
            with zipfile.ZipFile(source_zip()) as other:
                info = other.getinfo(original.filename)
        # Equal record metadata is accepted regardless of object identity or
        # originating reader. The bytes still come from the supplied source.
        assert info is not original
        with source.open_raw(info) as stream:
            assert stream.read() == raw_payload(source, original)
        with ZipFile(target, "w") as output:
            output.copy_member(source, info, arcname="copied")
            output.splice(source, "spliced/", select=[info])
    target.seek(0)
    with zipfile.ZipFile(target) as result:
        assert result.read("copied") == PAYLOAD
        assert result.read("spliced/original/ünicode.bin") == PAYLOAD



def test_raw_copy_ignores_orig_filename_metadata():
    with ZipFile(source_zip()) as source:
        original = source.infolist()[0]
        info = copy.copy(original)
        info.orig_filename = "mismatched-name"
        with source.open_raw(info) as stream:
            assert stream.read() == raw_payload(source, original)


def test_one_thousand_empty_packs_without_source_ownership_checks(monkeypatch):
    class UnindexedNames(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Supplied ZipInfo must not need name lookup")

    class UnindexedDirectory(list):
        def __iter__(self):
            raise AssertionError("Supplied ZipInfo must not scan the source")

    def forbidden():
        raise AssertionError("Supplied ZipInfo must not need infolist")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        for i in range(1000):
            writer.writestr(f"group/{i}/file", str(i).encode())
    with ZipFile(buffer) as source:
        selected = source.infolist()[:]
        source.NameToInfo = UnindexedNames(source.NameToInfo)
        source.filelist = UnindexedDirectory(source.filelist)
        with monkeypatch.context() as patch:
            patch.setattr(source, "infolist", forbidden)
            for i, info in enumerate(selected):
                target = io.BytesIO()
                with ZipFile(target, "w") as output:
                    output.splice(
                        source, "node/", select=[copy.copy(info)],
                        select_prefix=f"group/{i}/",
                    )
                target.seek(0)
                with zipfile.ZipFile(target) as result:
                    assert result.namelist() == ["node/file"]
                    assert result.read("node/file") == str(i).encode()


def test_raw_reads_position_once_without_tell_or_intermediate_read():
    class CountedSource(io.BytesIO):
        seeks = 0
        tells = 0
        reads = 0
        buffer_reads = 0

        def seek(self, *args):
            self.seeks += 1
            return super().seek(*args)

        def tell(self):
            self.tells += 1
            return super().tell()

        def read(self, size=-1):
            self.reads += 1
            return super().read(size)

        def readinto(self, buffer):
            self.buffer_reads += 1
            return super().readinto(buffer)

    buffer = CountedSource()
    chunk_size = 1024 * 1024
    contents = b"a" * chunk_size + b"b" * chunk_size + b"c" * chunk_size
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("member", contents)
    with ZipFile(buffer) as source:
        with (
            source.open_raw("member") as one,
            source.open_raw("member") as two,
        ):
            # Header reads have completed. Each payload read should seek once
            # under the source lock and fill the buffer directly, even when
            # readers move the shared file or explicitly seek backwards.
            buffer.seeks = buffer.tells = 0
            buffer.reads = buffer.buffer_reads = 0
            assert one.read(chunk_size) == b"a" * chunk_size
            assert one.read(chunk_size) == b"b" * chunk_size
            assert two.read(chunk_size) == b"a" * chunk_size
            assert one.read(chunk_size) == b"c" * chunk_size
            assert two.read(chunk_size) == b"b" * chunk_size
            assert buffer.seeks == 5
            assert buffer.buffer_reads == 5
            assert buffer.tells == buffer.reads == 0
            one.seek(0)
            assert one.read(chunk_size) == b"a" * chunk_size
            assert two.read(chunk_size) == b"c" * chunk_size
            assert buffer.seeks == buffer.buffer_reads == 7
            assert buffer.tells == buffer.reads == 0
