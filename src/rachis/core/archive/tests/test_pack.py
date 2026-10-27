# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Pack recipes and incoming archive adapters preserve logical and ZIP data."""

import errno
import gc
import hashlib
import struct
import uuid
import zipfile

import pytest

from rachis import Artifact, CacheV1, CacheV2, Note
from rachis.core.archive import ZipPack
from rachis.core.archive.archiver import (
    _CacheV2Archive,
    _NoOpArchive,
    _ZipArchive,
)
from rachis.core.archive.view import _ArchiveCapture
from rachis.core.archive.pack import manifest_bytes, materialize_pack
from rachis.core.archive.view import LiveArchiveView
from rachis.core.testing.type import IntSequence1
from rachis.plugins import dummy_plugin
from rachis.core.zip import ZipFile


ROOT = "00000000-0000-4000-8000-000000000001"
ANCESTOR = "00000000-0000-4000-8000-000000000002"
ANNOTATION = "00000000-0000-4000-8000-000000000003"
VERSION = b"QIIME 2\narchive: 7.1\nframework: 2026.10\n"


def compressed_bytes(source, entry):
    """Inspect the payload independently of the splicing implementation."""
    source.fp.seek(entry.header_offset)
    header = source.fp.read(30)
    name_size, extra_size = struct.unpack_from("<HH", header, 26)
    source.fp.seek(name_size + extra_size, 1)
    return source.fp.read(entry.compress_size)


@pytest.fixture
def incoming(tmp_path):
    """ZIP-level input for partitioning, codecs and directory-record tests."""
    members = {
        "VERSION": VERSION,
        "metadata.yaml": (
            f"uuid: {ROOT}\ntype: IntSequence1\n"
            "format: IntSequenceDirectoryFormat\n"
        ).encode(),
        "data/ints.txt": b"1\n2\n3\n",
        "data/sample-ID/": b"",
        "provenance/VERSION": VERSION,
        "provenance/metadata.yaml": b"own node\n",
        "provenance/action/action.yaml": b"action: {type: import}\n",
        "provenance/action/empty.tsv": b"",
        "provenance/action/empty-folder/": b"",
        f"provenance/artifacts/{ANCESTOR}/VERSION": VERSION,
        f"provenance/artifacts/{ANCESTOR}/metadata.yaml": b"ancestor\n",
        f"annotations/{ANNOTATION}/metadata.yaml": b"annotation\n",
        f"annotations/{ANNOTATION}/note.txt": b"a note\n",
        f"annotations/{ANNOTATION}/checksums.sha512": b"native manifest\n",
    }
    members["checksums.sha512"] = manifest_bytes(
        {
            name: hashlib.sha512(content).hexdigest()
            for name, content in members.items()
            if not name.endswith("/") and not name.startswith("annotations/")
        }
    )
    path = tmp_path / "incoming.qza"
    directory = tmp_path / ROOT
    directory.mkdir()
    with ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, content in members.items():
            output.writestr(ROOT + "/" + name, content)
            target = directory / name
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    return path, directory, members


@pytest.mark.parametrize("backend", [_ZipArchive, _NoOpArchive])
def test_archive_pack_partition_and_data_only_extraction(
    backend, incoming, tmp_path
):
    qza, directory, members = incoming
    with backend(qza if backend is _ZipArchive else directory) as archive:
        own = archive.self_provenance
        ancestors = list(archive.iter_provenance_packs())
        annotations = list(archive.iter_annotation_packs())
        assert own.uuid == ROOT
        assert [p.uuid for p in ancestors] == [ANCESTOR]
        assert [p.uuid for p in annotations] == [ANNOTATION]
        assert own.whiteouts == {"action/empty.tsv"}
        for recipe, prefix in [
            (own, "provenance/"),
            (ancestors[0], f"provenance/artifacts/{ANCESTOR}/"),
            (annotations[0], f"annotations/{ANNOTATION}/"),
        ]:
            destination = tmp_path / (recipe.uuid + ".zip")
            assert recipe.materialize(destination) == destination
            with zipfile.ZipFile(destination) as pack:
                expected = {
                    recipe.uuid + "/" + name[len(prefix) :]: content
                    for name, content in members.items()
                    if not name.endswith("/")
                    and name.startswith(prefix)
                    and (
                        recipe is not own
                        or not name.startswith("provenance/artifacts/")
                    )
                }
                assert {
                    n: pack.read(n)
                    for n in pack.namelist()
                    if not n.endswith("/")
                } == expected
                if recipe is own:
                    assert pack.getinfo(
                        ROOT + "/action/empty-folder/"
                    ).is_dir()
                assert pack.testzip() is None
        data = archive.extract_data(tmp_path / "data-only")
        assert (data / "ints.txt").read_bytes() == b"1\n2\n3\n"
        assert (data / "sample-ID").is_dir()
        assert set(p.name for p in data.iterdir()) == {"ints.txt", "sample-ID"}
        assert not (data.parent / "provenance").exists()
        if backend is _ZipArchive:
            mounted = tmp_path / "mounted" / ROOT
            mounted.mkdir(parents=True)
            archive.mount(mounted)
            assert (mounted / "provenance/action/empty-folder").is_dir()
            assert (mounted / "annotations" / ANNOTATION / "note.txt").exists()
        else:
            assert archive.mount(directory).root == directory


def test_qza_partition_never_uses_a_codec(incoming, tmp_path, monkeypatch):
    qza, _, _ = incoming
    with _ZipArchive(qza) as archive:
        recipe = archive.self_provenance
        entries = recipe._select
        before = {
            info.filename: compressed_bytes(archive._reader, info)
            for info in entries
        }

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "Pack materialization must not invoke a codec"
            )

        with monkeypatch.context() as patch:
            patch.setattr(zipfile, "_get_compressor", forbidden)
            patch.setattr(zipfile, "_get_decompressor", forbidden)
            assert recipe.whiteouts == {"action/empty.tsv"}
            path = recipe.materialize(tmp_path / "own.zip")
        with ZipFile(path) as result:
            for info in entries:
                suffix = info.filename[len(ROOT + "/provenance/") :]
                target = result.getinfo(ROOT + "/" + suffix)
                assert (
                    compressed_bytes(result, target) == before[info.filename]
                )
                assert target.CRC == info.CRC
                assert target.compress_type == info.compress_type


@pytest.mark.parametrize("zip64", [False, True], ids=["ordinary", "zip64"])
def test_v2_save_splices_packs_and_preserves_empty_directories(
    incoming, tmp_path, monkeypatch, zip64
):
    if zip64:
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 64)
    _, _, members = incoming
    qza = tmp_path / "mixed-compression.qza"
    methods = (
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    )
    before = {}
    with ZipFile(qza, "w") as output:
        for index, (name, content) in enumerate(members.items()):
            info = zipfile.ZipInfo(ROOT + "/" + name)
            info.compress_type = methods[index % len(methods)]
            info.file_size = len(content)
            with output.open(info, "w", force_zip64=zip64) as member:
                member.write(content)
    with ZipFile(qza) as source:
        for info in source.infolist():
            name = info.filename[len(ROOT) + 1 :]
            if name.startswith(("provenance/", "annotations/")):
                before[info.filename] = (
                    compressed_bytes(source, info),
                    info.CRC,
                    info.compress_type,
                    info.file_size,
                )

    cache = CacheV2(tmp_path / "cache")
    try:
        with cache:
            archiver = cache.import_archive(qza)
            # Index/schema reads belong to snapshot preparation. Once that
            # capture exists, save must copy pack payloads without decoding
            # them or opening a normal compressed writer for those members.
            with archiver.snapshot() as view, monkeypatch.context() as patch:
                patch.setattr(archiver._storage, "snapshot", lambda: view)
                original_member = view.open_member
                original_open = ZipFile.open
                splices = []
                original_splice = ZipFile.splice

                def open_member(member):
                    if member.startswith(("provenance/", "annotations/")):
                        raise AssertionError("Save must splice pack members")
                    return original_member(member)

                def open_zip(archive, member, mode="r", *args, **kwargs):
                    name = getattr(member, "filename", member)
                    if mode == "w" and name.startswith(
                        (
                            ROOT + "/provenance/",
                            ROOT + "/annotations/",
                        )
                    ):
                        raise AssertionError("Pack members must splice")
                    return original_open(
                        archive, member, mode, *args, **kwargs
                    )

                def splice(output, source, destination="", **kwargs):
                    splices.append(destination)
                    return original_splice(
                        output, source, destination, **kwargs
                    )

                patch.setattr(view, "open_member", open_member)
                patch.setattr(ZipFile, "open", open_zip)
                patch.setattr(ZipFile, "splice", splice)
                exported = tmp_path / "exported.qza"
                archiver.save(exported)

            assert set(splices) == {
                ROOT + "/provenance/",
                ROOT + "/provenance/artifacts/" + ANCESTOR + "/",
                ROOT + "/annotations/" + ANNOTATION + "/",
            }
            with ZipFile(exported) as result:
                assert set(result.namelist()) == {
                    ROOT + "/" + name for name in members
                }
                for name, expected in before.items():
                    info = result.getinfo(name)
                    assert (
                        compressed_bytes(result, info),
                        info.CRC,
                        info.compress_type,
                        info.file_size,
                    ) == expected
                for name, content in members.items():
                    assert result.read(ROOT + "/" + name) == content
                assert result.getinfo(ROOT + "/data/sample-ID/").is_dir()
                assert result.getinfo(
                    ROOT + "/provenance/action/empty-folder/"
                ).is_dir()
                if zip64:
                    assert (
                        result.getinfo(
                            ROOT + "/provenance/action/action.yaml"
                        ).extract_version
                        >= 45
                    )
                assert result.testzip() is None
    finally:
        cache.close()


def test_partition_recipe_survives_adapter_variable_and_iterator(
    incoming, tmp_path
):
    qza, _, _ = incoming
    recipes = list(_ZipArchive(qza).iter_provenance_packs())
    gc.collect()
    result = recipes[0].materialize(tmp_path / "ancestor.zip")
    with ZipFile(result) as source:
        assert source.read(ANCESTOR + "/metadata.yaml") == b"ancestor\n"


@pytest.mark.parametrize(
    "link_error",
    [
        None,
        errno.EXDEV,
        errno.EMLINK,
        errno.EPERM,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        errno.ENOSYS,
    ],
)
def test_literal_pack_link_or_copy(
    incoming, tmp_path, monkeypatch, link_error
):
    qza, _, _ = incoming
    with _ZipArchive(qza) as archive:
        original = archive.self_provenance.materialize(
            tmp_path / "original.zip"
        )
    original_bytes = original.read_bytes()
    if link_error is not None:

        def unavailable(*args, **kwargs):
            raise OSError(link_error, "link unavailable")

        monkeypatch.setattr("rachis.core.archive.pack.os.link", unavailable)
    recipe = ZipPack(ROOT, original)
    assert recipe.whiteouts == {"action/empty.tsv"}
    destination = recipe.materialize(tmp_path / "copy.zip")
    assert destination.read_bytes() == original_bytes
    if link_error is None:
        assert destination.stat().st_ino == original.stat().st_ino
    else:
        assert destination.stat().st_ino != original.stat().st_ino


def test_separate_masks_do_not_rewrite_literal_pack(incoming, tmp_path):
    qza, _, _ = incoming
    with _ZipArchive(qza) as archive:
        original = archive.self_provenance.materialize(
            tmp_path / "original.zip"
        )
    recipe = ZipPack(ROOT, original, whiteouts={"metadata.yaml"})
    assert recipe.whiteouts == {"action/empty.tsv", "metadata.yaml"}
    result = recipe.materialize(tmp_path / "masked.zip")
    assert result.read_bytes() == original.read_bytes()
    with ZipFile(result) as source:
        assert source.read(ROOT + "/metadata.yaml") == b"own node\n"


def test_literal_pack_stream_is_copied_and_kept_open(incoming, tmp_path):
    qza, _, _ = incoming
    with _ZipArchive(qza) as archive:
        original = archive.self_provenance.materialize(
            tmp_path / "original.zip"
        )
    with original.open("rb") as stream:
        recipe = ZipPack(ROOT, stream)
        assert recipe.whiteouts == {"action/empty.tsv"}
        stream.seek(10)
        result = recipe.materialize(tmp_path / "copy.zip")
        assert result.read_bytes() == original.read_bytes()
        assert not stream.closed


def test_cache_indexing_never_mutates_a_linked_incoming_pack(
    incoming, tmp_path
):
    qza, _, _ = incoming
    with _ZipArchive(qza) as archive:
        original = archive.self_provenance.materialize(
            tmp_path / "original.zip"
        )
    before = original.read_bytes()
    mode = original.stat().st_mode
    destination = tmp_path / "indexed.zip"
    materialize_pack(destination, ZipPack(ROOT, original))
    assert original.read_bytes() == before
    assert original.stat().st_mode == mode
    assert destination.stat().st_ino != original.stat().st_ino
    with ZipFile(original) as z:
        assert "checksums.sha512" not in z.namelist()
    with ZipFile(destination) as z:
        assert z.read("checksums.sha512")
        assert z.read(ROOT + "/metadata.yaml") == b"own node\n"


def test_large_qza_is_partitioned_once(tmp_path, monkeypatch):
    path = tmp_path / "large.qza"
    identities = [str(uuid.uuid4()) for _ in range(1000)]
    with ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(ROOT + "/VERSION", VERSION)
        for identity in identities:
            output.writestr(
                ROOT + "/provenance/artifacts/" + identity + "/metadata.yaml",
                identity.encode(),
            )
    with _ZipArchive(path) as archive:
        recipes = list(archive.iter_provenance_packs())
        assert len(recipes) == len(identities)

        def forbidden():
            raise AssertionError(
                "Each partition must use its supplied ZipInfos"
            )

        monkeypatch.setattr(archive._reader, "infolist", forbidden)
        monkeypatch.setattr(archive._reader, "getinfo", forbidden)
        for recipe in recipes:
            assert recipe.whiteouts == set()
            result = recipe.materialize(tmp_path / (recipe.uuid + ".zip"))
            with ZipFile(result) as source:
                assert source.read(recipe.uuid + "/metadata.yaml") == (
                    recipe.uuid.encode()
                )


def test_v2_import_materializes_only_data_and_streams_packs(
    incoming, tmp_path, monkeypatch
):
    qza, _, members = incoming
    cache = CacheV2(tmp_path / "cache")
    original = _ZipArchive.open_binary
    reads = []

    def open_binary(self, member):
        reads.append(str(member))
        return original(self, member)

    def no_mount(*args):
        raise AssertionError("V2 import must not mount the complete archive")

    monkeypatch.setattr(_ZipArchive, "open_binary", open_binary)
    monkeypatch.setattr(_ZipArchive, "mount", no_mount)
    try:
        with cache:
            result = cache.import_archive(qza)
            assert result.data_dir.joinpath("sample-ID").is_dir()
            own = result.self_provenance
            ancestors = list(result.iter_provenance_packs())
            annotations = list(result.iter_annotation_packs())
            assert own.whiteouts == {"action/empty.tsv"}
            assert [p.uuid for p in ancestors] == [ANCESTOR]
            assert [p.uuid for p in annotations] == [ANNOTATION]
            for recipe in [own, *ancestors, *annotations]:
                path = recipe.materialize(tmp_path / (recipe.uuid + ".zip"))
                with ZipFile(path) as pack:
                    assert "checksums.sha512" in pack.namelist()
            assert all(
                member.startswith("data/") or "/" not in member
                for member in reads
            )
            # Indexing/decompression uses pack streams, never files extracted
            # into a provenance or annotations tree during import.
            assert "provenance/metadata.yaml" not in reads
            assert (result.provenance_dir / "metadata.yaml").read_bytes() == (
                members["provenance/metadata.yaml"]
            )
            for recipe in [own, *ancestors, *annotations]:
                recipe._owner.close()
    finally:
        cache.close()


@pytest.mark.parametrize("backend", [CacheV1, CacheV2])
def test_archiver_pack_iteration_for_both_caches(backend, tmp_path):
    cache = backend(tmp_path / backend.__name__)
    try:
        with cache:
            result = Artifact.import_data(IntSequence1, [1, 2, 3])
            result.add_annotation(Note("note", text="preserve this note"))
            own = result._archiver.self_provenance
            (annotation,) = result._archiver.iter_annotation_packs()
            assert list(result._archiver.iter_provenance_packs()) == []
            assert list(result.iter_annotations())[0].name == "note"
            own_path = own.materialize(tmp_path / "own.zip")
            note_path = annotation.materialize(tmp_path / "note.zip")
            with ZipFile(own_path) as z:
                assert z.read(own.uuid + "/metadata.yaml")
            with ZipFile(note_path) as z:
                assert (
                    z.read(annotation.uuid + "/note.txt")
                    == b"preserve this note"
                )
            own._owner.close()
            annotation._owner.close()
    finally:
        if isinstance(cache, CacheV2):
            cache.close()


def test_v2_pack_recipes_use_live_membership_without_a_snapshot(
    tmp_path, monkeypatch
):
    cache = CacheV2(tmp_path / "cache")
    try:
        with cache:
            result = Artifact.import_data(IntSequence1, [1, 2, 3])

            def forbidden(*args, **kwargs):
                raise AssertionError("Pack iteration must not index archive")

            monkeypatch.setattr(_ArchiveCapture, "__init__", forbidden)
            monkeypatch.setattr(LiveArchiveView, "members", forbidden)
            with result._archiver._archive() as adapter:
                assert list(adapter.iter_annotation_packs()) == []
                result.add_annotation(Note("new", text="added after adapter"))
                (annotation,) = adapter.iter_annotation_packs()
                own = adapter.self_provenance
                assert list(adapter.iter_provenance_packs()) == []

                # Retain just the requested stream, even if its binding is
                # subsequently removed. This does not freeze membership.
                result.remove_annotation("new")
                cache.garbage_collection()
                assert list(adapter.iter_annotation_packs()) == []
                path = annotation.materialize(tmp_path / "removed.zip")
                with ZipFile(path) as source:
                    assert source.read(annotation.uuid + "/note.txt") == (
                        b"added after adapter"
                    )
                own.materialize(tmp_path / "own.zip")
    finally:
        cache.close()


def test_v2_adapter_reads_and_retains_storage_without_an_archiver(
    incoming, tmp_path
):
    qza, _, members = incoming
    cache = CacheV2(tmp_path / "cache")
    adapter = None
    try:
        with cache:
            with cache.root_scope.child(recycle=False):
                archiver = cache.import_archive(qza)
                ref_id = archiver.ref_id
                adapter = _CacheV2Archive(cache, ref_id)
                del archiver
            gc.collect()
            cache.garbage_collection(deep=True)

            # The originating frame has returned; the adapter's own reference
            # keeps both the RefDir and its immutable archive reachable.
            assert cache.object_path("archives", ref_id).is_dir()
            assert adapter.uuid == ROOT
            assert adapter.version == "7.1"
            data = adapter.extract_data(tmp_path / "data")
            assert (data / "ints.txt").read_bytes() == members["data/ints.txt"]
            assert (data / "sample-ID").is_dir()
            with adapter.open_binary("checksums.sha512") as stream:
                assert stream.read() == members["checksums.sha512"]
            own = adapter.self_provenance.materialize(tmp_path / "own.zip")
            with ZipFile(own) as source:
                assert (
                    source.read(ROOT + "/metadata.yaml")
                    == (members["provenance/metadata.yaml"])
                )
            assert [p.uuid for p in adapter.iter_provenance_packs()] == [
                ANCESTOR
            ]
            assert [p.uuid for p in adapter.iter_annotation_packs()] == [
                ANNOTATION
            ]
            adapter.close()
            adapter.close()
            cache.garbage_collection(deep=True)
            assert not cache.object_path("archives", ref_id).exists()
    finally:
        if adapter is not None:
            adapter.close()
        cache.close()


@pytest.mark.parametrize("backend", [CacheV1, CacheV2])
def test_cache_save_imports_the_source_adapter_without_saving_a_qza(
    backend, tmp_path, monkeypatch
):
    source_cache = backend(tmp_path / "source-cache")
    target_cache = CacheV2(tmp_path / "target-cache")
    try:
        with source_cache:
            original = Artifact.import_data(IntSequence1, [1, 2, 3])
            original.add_annotation(Note("note", text="direct transfer"))
            manifest = original.get_checksums()

        def no_save(*args, **kwargs):
            raise AssertionError("Cache transfer must not save a QZA")

        monkeypatch.setattr(original, "save", no_save)
        import_archive = target_cache.import_archive
        adapters = []

        def import_adapter(archive, *args, **kwargs):
            adapters.append(type(archive))
            return import_archive(archive, *args, **kwargs)

        monkeypatch.setattr(target_cache, "import_archive", import_adapter)
        with target_cache:
            transferred = target_cache.save(original, "transferred")
            assert adapters == [
                _NoOpArchive if backend is CacheV1 else _CacheV2Archive
            ]
            assert transferred.uuid == original.uuid
            assert transferred.view(list) == [1, 2, 3]
            assert transferred.get_annotation("note").contents == (
                "direct transfer"
            )
            assert transferred.get_checksums() == manifest
            transferred.validate(level="max")
            exported = tmp_path / "exported.qza"
            transferred.save(exported)
            restored = Artifact.load(exported)
            assert restored.get_checksums() == manifest
            restored.validate(level="max")
    finally:
        target_cache.close()
        if isinstance(source_cache, CacheV2):
            source_cache.close()


def test_v2_adapter_transfer_keeps_canonical_bytes_and_separate_masks(
    tmp_path, sample_metadata
):
    source_cache = CacheV2(tmp_path / "source-cache")
    target_cache = CacheV2(tmp_path / "target-cache")
    legacy = CacheV1(tmp_path / "legacy-cache")
    try:
        with source_cache:
            result = dummy_plugin.actions.identity_with_metadata(
                Artifact.import_data(IntSequence1, [1, 2, 3]), sample_metadata
            ).out
            identity = str(result.uuid)
            original_metadata = result.metadata_paths()[0][0].read_bytes()
            result.redact_metadata()
            result.validate(level="max")
        # This unit test inspects the adapter's canonical pack separately from
        # the empty logical member exposed by the public Artifact API.
        with result._archiver._archive() as adapter:
            pack = adapter.self_provenance
            assert "action/metadata.tsv" in pack.whiteouts
            canonical = pack.materialize(tmp_path / "canonical.zip")
            with ZipFile(canonical) as archive:
                assert archive.read(identity + "/action/metadata.tsv") == (
                    original_metadata
                )
            saved = tmp_path / "redacted.qza"
            result.save(saved)
            hidden = identity + "/provenance/action/metadata.tsv"
            with ZipFile(saved) as archive:
                assert archive.namelist().count(hidden) == 1
                assert archive.read(hidden) == b""
                assert archive.getinfo(hidden).file_size == 0
                assert archive.getinfo(hidden).CRC == 0
            with ZipFile(canonical) as archive:
                assert archive.read(identity + "/action/metadata.tsv") == (
                    original_metadata
                )
        for destination in (target_cache, legacy):
            with destination:
                transferred = destination.save(result, "redacted")
                assert transferred.uuid == result.uuid
                assert transferred.view(list) == [1, 2, 3]
                assert all(
                    path.read_bytes() == b""
                    for path in transferred.metadata_paths()[0]
                )
                transferred.validate(level="max")
        with target_cache:
            restored = Artifact.load(saved)
            restored.validate(level="max")
            assert all(
                p.read_bytes() == b"" for p in restored.metadata_paths()[0]
            )
    finally:
        source_cache.close()
        target_cache.close()
