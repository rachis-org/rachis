"""Cache V2 acceptance tests: archive semantics and ownership invariants."""

import concurrent.futures
import json
from pathlib import Path
import pickle
import uuid
import zipfile

import pytest

from rachis import Cache, CacheV1, CacheV2, Artifact, Note
from rachis.core.archive.pack import pack_path
from rachis.core.testing.type import IntSequence1
from rachis.core.archive.format.util import artifact_version
from rachis.core.exceptions import ValidationError
from rachis.plugins import dummy_plugin


@pytest.fixture
def cache(tmp_path):
    """Create a fresh V2 cache; each test explicitly selects its cache."""
    cache = CacheV2(tmp_path / "target-cache")
    try:
        yield cache
    finally:
        cache.close()


@pytest.fixture(params=[CacheV1, CacheV2], ids=["source-v1", "source-v2"])
def source_cache(request, tmp_path):
    """Create an independent source cache without selecting it implicitly."""
    source = request.param(tmp_path / "source-cache")
    try:
        yield source
    finally:
        if isinstance(source, CacheV2):
            source.close()


def artifact():
    return Artifact.import_data(IntSequence1, [1, 2, 3])


def archive_members(path):
    with zipfile.ZipFile(path) as source:
        root = source.namelist()[0].split("/")[0] + "/"
        return {
            n[len(root) :]: source.read(n)
            for n in source.namelist()
            if not n.endswith("/")
        }


def write_archive(path, identity, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(identity + "/" + name, data)


def test_dispatch_and_reject_without_changes(tmp_path):
    v1 = CacheV1(tmp_path / "v1")
    v2 = Cache(tmp_path / "v2")
    assert Cache(v1.path) is v1
    assert Cache(v2.path) is v2
    assert isinstance(v1, Cache) and isinstance(v2, Cache)
    assert isinstance(v2, CacheV2)
    assert "data" not in {p.name for p in v2.path.iterdir()}
    invalid = tmp_path / "bad"
    invalid.mkdir()
    (invalid / "VERSION").write_text(
        "QIIME 2\ncache: v99\nframework: 2026.10\n"
    )
    before = list(invalid.iterdir())
    with pytest.raises(ValueError):
        Cache(invalid)
    assert list(invalid.iterdir()) == before
    with pytest.raises(ValueError):
        CacheV1(v2.path)
    v2.close()


def test_named_keys_share_refdir_and_survive_retarget(cache, tmp_path):
    with CacheV1(tmp_path / "source-cache"):
        original = artifact()
        replacement = Artifact.import_data(IntSequence1, [4, 5, 6])
    with cache:
        cache.save(original, "result")
        one, two = cache.load("result"), cache.load("result")
        assert one.uuid == two.uuid == original.uuid
        assert (
            list(one.iter_annotations()) == list(two.iter_annotations()) == []
        )
        one.add_annotation(Note("first", text="from first handle"))
        two.add_annotation(Note("second", text="from second handle"))
        for result in (one, two):
            assert {n.name for n in result.iter_annotations()} == {
                "first",
                "second",
            }
        cache.save(replacement, "result")
        assert cache.load("result").view(list) == [4, 5, 6]
        cache.remove("result")
        assert one.view(list) == two.view(list) == [1, 2, 3]
        one.remove_annotation("first")
        assert two.get_annotation("second").contents == "from second handle"
        two.remove_annotation("second")
        assert (
            list(one.iter_annotations()) == list(two.iter_annotations()) == []
        )


def test_uuid_hit_keeps_independent_annotations(cache, tmp_path):
    first, second = tmp_path / "first.qza", tmp_path / "second.qza"
    with CacheV1(tmp_path / "source-cache"):
        source = artifact()
        source.add_annotation(Note("first", text="first version"))
        source.save(first)
        source.remove_annotation("first")
        source.add_annotation(Note("second", text="second version"))
        source.save(second)
    with cache:
        assert cache.get_data() == []
        one, two = Artifact.load(first), Artifact.load(second)
        assert one.uuid == two.uuid == source.uuid
        assert one.get_annotation("first").contents == "first version"
        assert two.get_annotation("second").contents == "second version"
        with pytest.raises(KeyError):
            one.get_annotation("second")
        two.remove_annotation("second")
        assert one.get_annotation("first").contents == "first version"


def test_snapshot_consistent_and_explicit_materialization(cache, tmp_path):
    with CacheV1(tmp_path / "source-cache"):
        source = artifact()
        source.add_annotation(Note("first", text="keep"))
    with cache:
        result = cache.save(source, "result")
        with result.snapshot() as view:
            members = view.members()
            result.remove_annotation("first")
            result.add_annotation(Note("second", text="new"))
            assert view.members() == members
            with view.materialize("annotations") as directory:
                assert (
                    list(directory.path.rglob("note.txt"))[0].read_text()
                    == "keep"
                )
            with pytest.raises(TypeError):
                Path(view.root)


@pytest.mark.parametrize("failure", ["opening", "indexing"])
def test_failed_snapshot_releases_every_opened_pack_and_scope(
    cache, monkeypatch, failure
):
    with cache:
        import rachis.core.archive.view as module

        a = artifact()
        a.add_annotation(Note("first", text="one"))
        a.add_annotation(Note("second", text="two"))
        original_open = Path.open
        original_pack = module.zipfile.ZipFile
        opened = []
        cache.garbage_collection(deep=True)
        scopes = set((cache.path / "mutable/scopes").glob("*/*/.iref"))

        def open_file(path, *args, **kwargs):
            if path.suffix == ".zip":
                if failure == "opening" and len(opened) == 2:
                    raise OSError("simulated snapshot failure")
                file = original_open(path, *args, **kwargs)
                opened.append(file)
                return file
            return original_open(path, *args, **kwargs)

        def open_pack(*args, **kwargs):
            if failure == "indexing":
                raise OSError("simulated snapshot failure")
            return original_pack(*args, **kwargs)

        with monkeypatch.context() as patch:
            # Isolate snapshot acquisition from catalogue reconciliation I/O.
            patch.setattr(cache, "refresh_provenance", lambda *args: None)
            patch.setattr(Path, "open", open_file)
            patch.setattr(module.zipfile, "ZipFile", open_pack)
            with pytest.raises(OSError, match="simulated snapshot failure"):
                a.snapshot()
        assert opened and all(file.closed for file in opened)
        assert set((cache.path / "mutable/scopes").glob("*/*/.iref")) == scopes
        assert a.get_annotation("first").contents == "one"
        assert a.get_annotation("second").contents == "two"


def test_live_path_queries_do_not_allocate_snapshots_or_run_gc(
    cache, monkeypatch
):
    with cache:
        import rachis.core.archive.view as module

        a = artifact()
        a.add_annotation(Note("note", text="current contents"))
        annotation_id = str(a.get_annotation("note").id)
        root = a._archiver.root_dir
        with a.snapshot() as view:
            expected_members = view.members()

        def unexpected(*args, **kwargs):
            pytest.fail(
                "an individual path query must not allocate a snapshot"
            )

        monkeypatch.setattr(a._archiver, "snapshot", unexpected)
        with monkeypatch.context() as patch:
            patch.setattr(module._ArchiveCapture, "__init__", unexpected)
            patch.setattr(cache.root_scope, "child", unexpected)
            patch.setattr(cache, "garbage_collection", unexpected)
            assert a.get_annotation("note").contents == "current contents"
            assert [n.name for n in a.iter_annotations()] == ["note"]
            assert a._archiver.citations
            assert root.is_dir() and root.exists()
            assert (root / "data").is_dir()
            assert (root / "provenance").is_dir()
            assert (root / "provenance/action").is_dir()
            assert not (root / "provenance/artifacts").exists()
            assert (root / "annotations").is_dir()
            assert (root / ("annotations/" + annotation_id)).is_dir()
            assert (root / "metadata.yaml").read_bytes()
            assert (root / "data/ints.txt").read_text() == "1\n2\n3\n"
            assert (root / "provenance/action/action.yaml").read_bytes()
            note = root / ("annotations/" + annotation_id + "/note.txt")
            assert note.read_text() == "current contents"
            assert not (root / "missing").exists()
            assert not (root / "provenance/missing").exists()
            assert not (root / "annotations/not-a-uuid").exists()
            assert root.view.members() == expected_members
            assert all(
                (root / member).is_file() for member in expected_members
            )
            assert {p.name for p in root.iterdir()} == {
                "VERSION",
                "metadata.yaml",
                "checksums.sha512",
                "data",
                "provenance",
                "annotations",
            }
            assert {p.member for p in root.rglob("*")} == set(expected_members)
            with pytest.raises(FileNotFoundError):
                (root / "provenance/missing").read_bytes()
            with pytest.raises(FileNotFoundError):
                (root / "checksums.md5").read_bytes()


def test_single_member_reads_only_open_the_selected_pack(cache, monkeypatch):
    with cache:
        import rachis.core.archive.view as module

        a = artifact()
        a.add_annotation(Note("first", text="selected"))
        a.add_annotation(Note("second", text="unrelated"))
        selected = str(a.get_annotation("first").id)
        root = a._archiver.root_dir
        opened = []
        original = module.zipfile.ZipFile
        original_read = original.read

        def read(pack, member, *args, **kwargs):
            if member == "checksums.sha512":
                pytest.fail(
                    "individual reads must not load pack checksum indexes"
                )
            return original_read(pack, member, *args, **kwargs)

        def open_pack(file, *args, **kwargs):
            opened.append(Path(file.name))
            return original(file, *args, **kwargs)

        monkeypatch.setattr(original, "read", read)
        monkeypatch.setattr(module.zipfile, "ZipFile", open_pack)
        assert (root / "VERSION").read_bytes()
        assert (root / "data/ints.txt").read_bytes()
        assert opened == []
        note = root / ("annotations/" + selected + "/note.txt")
        assert note.read_text() == "selected"
        ref = cache.object_path("ref", a._archiver.ref_id)
        assert opened == [ref / "annotations" / (selected + ".zip")]


def test_open_pack_stream_survives_named_membership_removal_and_gc(
    cache, monkeypatch
):
    with cache:
        a = artifact()
        contents = "retained contents " * 10000
        a.add_annotation(Note("note", text=contents))
        identity = str(a.get_annotation("note").id)
        cache.save(a, "shared")
        reader, writer = cache.load("shared"), cache.load("shared")
        ref = cache.object_path("ref", reader._archiver.ref_id)
        binding = ref / "annotations" / (identity + ".zip")
        files = []
        original_open = Path.open

        def open_file(path, *args, **kwargs):
            file = original_open(path, *args, **kwargs)
            if path == binding:
                files.append(file)
            return file

        monkeypatch.setattr(Path, "open", open_file)
        member = reader._archiver.root_dir / (
            "annotations/" + identity + "/note.txt"
        )
        with member.open("rb") as stream:
            retained = files[-1]
            assert stream.read(9) == contents.encode()[:9]
            writer.remove_annotation("note")
            cache.garbage_collection(deep=True)
            assert not member.exists()
            assert not binding.exists()
            assert not pack_path(cache, "annotations", identity).exists()
            assert not retained.closed
            assert stream.read() == contents.encode()[9:]
        assert retained.closed
        stream.close()  # Repeated closure must release resources only once.


def test_failed_member_lookup_closes_pack_file(cache, monkeypatch):
    with cache:
        a = artifact()
        _, physical = cache.attachment(a._archiver.ref_id)
        binding = physical / "provenance" / (str(a.uuid) + ".zip")
        files = []
        original_open = Path.open

        def open_file(path, *args, **kwargs):
            file = original_open(path, *args, **kwargs)
            if path == binding:
                files.append(file)
            return file

        monkeypatch.setattr(Path, "open", open_file)
        with pytest.raises(FileNotFoundError):
            (a._archiver.root_dir / "provenance/missing").read_bytes()
        assert files and all(file.closed for file in files)


def test_bulk_save_keeps_capture_when_named_view_changes(
    cache, tmp_path, monkeypatch, sample_metadata
):
    full = tmp_path / "original.qza"
    with CacheV1(tmp_path / "source-cache"):
        source = dummy_plugin.actions.identity_with_metadata(
            artifact(), sample_metadata
        ).out
        source.add_annotation(Note("old", text="original annotation"))
        source.save(full)
    with cache:
        cache.save(Artifact.load(full), "shared")
        reader, writer = cache.load("shared"), cache.load("shared")
        baseline = tmp_path / "baseline.qza"
        reader.save(baseline)
        original_capture = reader._archiver._storage.snapshot
        edited = False

        def capture():
            # Simulate another writer after saving has captured its selections.
            nonlocal edited
            view = original_capture()
            if not edited:
                edited = True
                writer.redact_metadata()
                writer.remove_annotation("old")
                writer.add_annotation(Note("new", text="later annotation"))
            return view

        saved = tmp_path / "saved.qza"
        with monkeypatch.context() as patch:
            patch.setattr(reader._archiver._storage, "snapshot", capture)
            reader.save(saved)
        assert edited
        assert archive_members(saved) == archive_members(baseline)
        assert all(p.read_bytes() == b"" for p in reader.metadata_paths()[0])
        assert reader.get_annotation("new").contents == "later annotation"
        with pytest.raises(KeyError):
            reader.get_annotation("old")
    verification = CacheV2(tmp_path / "verification-cache")
    try:
        with verification:
            restored = Artifact.load(saved)
            restored.validate(level="max")
            assert all(p.read_bytes() for p in restored.metadata_paths()[0])
            assert restored.get_annotation("old").contents == (
                "original annotation"
            )
    finally:
        verification.close()


def test_scopes_adopt_and_recycle_retains_contents(cache):
    with cache:
        parent = cache.root_scope.child()
        child = parent.child()
        directory = child.acquire_directory()
        marker = cache.object_path("ref", directory.ref_id) / ".iref"
        inode = marker.stat().st_ino
        parent.adopt(directory.ref_id)
        child.close()
        cache.garbage_collection()
        assert marker.stat().st_ino == inode
        recycle = cache.pools / "scope"
        recycle.mkdir()
        cache.retain(recycle, "scope", parent.scope_id)
        parent.close()
        cache.garbage_collection()
        assert directory.path.exists()
        cache.release(recycle, "scope", parent.scope_id)
        cache.garbage_collection()
        assert not marker.exists()


def test_marker_validation_and_cycles(cache):
    with cache:
        child = cache.root_scope.child()
        first, second = child.acquire_directory(), child.acquire_directory()
        first.reference(second)
        with pytest.raises(ValueError, match="acyclic"):
            second.reference(first)
        fake = child.path / "ref.fake.iref"
        fake.write_text("../../escape/\n")
        with pytest.raises(ValueError):
            cache.marker_target(fake)
        fake.unlink()
        child.close()


def test_pickle_dispatch(cache):
    with cache:
        assert pickle.loads(pickle.dumps(cache)) is cache


@pytest.mark.parametrize("version", range(1, 7))
def test_legacy_versions_roundtrip_and_indexes(cache, tmp_path, version):
    # Keep historical archive bytes, including native MD5 checksums, as inputs.
    data = Path(__file__).parents[1] / "archive/provenance_lib/tests/data"
    directory = data / f"concated-ints-v{version}"
    assert directory.is_dir(), f"Missing legacy archive fixture: {directory}"
    (root,) = (p for p in directory.iterdir() if p.is_dir())
    source = tmp_path / f"v{version}.qza"
    with zipfile.ZipFile(source, "w") as archive:
        for file in root.rglob("*"):
            if file.is_file():
                archive.write(
                    file, root.name + "/" + file.relative_to(root).as_posix()
                )
    before = archive_members(source)
    with cache:
        loaded = Artifact.load(source)
        assert loaded.archive_version == str(version)
        loaded.validate(level="max")
        saved = tmp_path / f"saved-v{version}.qza"
        loaded.save(saved)
        assert archive_members(saved) == before
        with loaded.snapshot() as view:
            assert set(view.members()) == set(before)
            for member, content in before.items():
                assert (view.root / member).read_bytes() == content
        # These assertions specifically exercise the cache storage layout.
        (physical,) = cache.path.glob(f"immutable/archives/*/{loaded.uuid}")
        assert not (physical / "data-checksums.sha512").exists()
        for algorithm in ("md5", "sha512"):
            name = "checksums." + algorithm
            if name in before:
                assert (physical / name).read_bytes() == before[name]
            else:
                assert not (physical / name).exists()
        for pack in (physical / "provenance").glob("*.zip"):
            with zipfile.ZipFile(pack) as archive:
                assert archive.read("checksums.sha512")


def test_redacted_first_import_keeps_independent_metadata(
    cache, source_cache, tmp_path, sample_metadata
):
    original_qza = tmp_path / "original.qza"
    redacted_qza = tmp_path / "redacted.qza"
    with source_cache:
        source = dummy_plugin.actions.identity_with_metadata(
            artifact(), sample_metadata
        ).out
        source.save(original_qza)
        source.redact_metadata()
        source.save(redacted_qza)
    with cache:
        assert cache.get_data() == []
        redacted = Artifact.load(redacted_qza)
        original = Artifact.load(original_qza)
        assert redacted.uuid == original.uuid == source.uuid
        assert all(p.read_bytes() == b"" for p in redacted.metadata_paths()[0])
        assert all(p.read_bytes() for p in original.metadata_paths()[0])
        redacted_saved = tmp_path / "redacted-saved.qza"
        original_saved = tmp_path / "original-saved.qza"
        redacted.save(redacted_saved)
        original.save(original_saved)
        assert archive_members(redacted_saved) == archive_members(redacted_qza)
        assert archive_members(original_saved) == archive_members(original_qza)
        redacted.validate(level="max")
        original.validate(level="max")


def test_named_key_redaction_updates_shared_handles(
    cache, tmp_path, sample_metadata
):
    source_qza = tmp_path / "original.qza"
    with CacheV1(tmp_path / "source-cache"):
        source = dummy_plugin.actions.identity_with_metadata(
            artifact(), sample_metadata
        ).out
        source.save(source_qza)
    with cache:
        cache.save(Artifact.load(source_qza), "shared")
        one, two = cache.load("shared"), cache.load("shared")
        one.redact_metadata()
        two.add_annotation(Note("second", text="from another handle"))
        assert all(p.read_bytes() == b"" for p in two.metadata_paths()[0])
        assert one.get_annotation("second").contents == "from another handle"
        one.validate(level="max")
        two.validate(level="max")
        # A fresh import of the original is independent of the named-key edits.
        independent = Artifact.load(source_qza)
        assert all(p.read_bytes() for p in independent.metadata_paths()[0])
        independent.validate(level="max")


def test_metadata_lock_excludes_threads(cache):
    with cache:
        counter = [0]

        def increment(_):
            for i in range(15):
                with cache.lock:
                    with cache.lock:
                        value = counter[0]
                        counter[0] = value + 1

        with concurrent.futures.ThreadPoolExecutor(4) as executor:
            list(executor.map(increment, range(4)))
        assert counter == [60]


def test_incomplete_ref_retained_after_process_close(tmp_path):
    cache = Cache(tmp_path / "cache")
    reserved = str(uuid.uuid4())
    result = cache.reserve_result(reserved, coordinates=[0, 1])
    cache.retain_incomplete(result, "incomplete")
    ref_id = result.ref_id
    cache.close()
    reopened = Cache(cache.path)
    with reopened.resume("incomplete") as producer:
        assert producer.ref_id == ref_id
        assert (
            json.loads((producer.path / "resumption.json").read_text())["uuid"]
            == reserved
        )
        producer.commit_cell("000001", "attempt1", b"outcome")
        assert (producer.path / "cells/000001/attempt1/outcome.json").exists()
    with pytest.raises(ValueError, match="incomplete"):
        reopened.load("incomplete")
    reopened.remove("incomplete")
    for reference in list(reopened.root_scope.references()):
        if reference.target.parent.name == ref_id:
            reopened.root_scope.unreference(reference)
    reopened.garbage_collection(deep=True)
    assert not producer.path.exists()
    reopened.close()


@pytest.mark.parametrize("filesystem", ["nfs", "unknown"])
def test_gc_runs_on_all_filesystem_types(tmp_path, monkeypatch, filesystem):
    from types import SimpleNamespace
    import rachis.core.cache_v2 as module

    monkeypatch.setattr(
        module.psutil,
        "disk_partitions",
        lambda all: [
            SimpleNamespace(mountpoint=str(tmp_path), fstype=filesystem)
        ],
    )
    cache = Cache(tmp_path / "cache")
    try:
        scope = cache.root_scope.child()
        directory = scope.acquire_directory()
        scope.close()
        cache.garbage_collection(deep=True)
        assert not directory.path.exists()
    finally:
        cache.close()


@pytest.mark.parametrize(
    "kind", ["scope", "provenance_pack", "annotation_pack", "deep_pack"]
)
def test_gc_uses_fresh_locked_count_for_every_retirement(
    cache, monkeypatch, kind
):
    with cache:
        import rachis.core.cache_v2 as module
        from rachis.core.cache_stat import OwnershipStat

        if kind == "scope":
            scope = cache.root_scope.child()
            path = scope.path
            marker = path / ".iref"
            scope.close()
        else:
            identity = str(uuid.uuid4())
            category = (
                "annotations" if kind == "annotation_pack" else "provenance"
            )
            path = marker = pack_path(cache, category, identity)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"orphan pack")
            if kind != "deep_pack":
                cache.schedule(kind, identity)
        original = module.ownership_stat
        checked = []

        def fresh_stat(candidate):
            result = original(candidate)
            if candidate == marker:
                locked = bool(getattr(cache.lock._local, "depth", 0))
                checked.append(locked)
                if locked:
                    # Ordinary stat sees an unowned candidate; the fresh read
                    # discovers another owner and must prevent retirement.
                    assert candidate.stat().st_nlink == 1
                    return OwnershipStat(result.st_dev, result.st_ino, 2)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(module, "ownership_stat", fresh_stat)
            cache.garbage_collection(deep=kind == "deep_pack")
        assert checked == [False, True]
        assert path.exists()
        cache.garbage_collection(deep=True)
        assert not path.exists()


def test_version_zero_has_no_invented_provenance(cache, tmp_path):
    source_qza = tmp_path / "version-zero.qza"
    with artifact_version("0"), CacheV1(tmp_path / "source-cache"):
        source = artifact()
        source.save(source_qza)
    with cache:
        loaded = Artifact.load(source_qza)
        assert loaded.uuid == source.uuid
        assert loaded.archive_version == "0"
        with loaded.snapshot() as view:
            assert not (view.root / "provenance").exists()
        saved = tmp_path / "saved.qza"
        loaded.save(saved)
        assert archive_members(saved) == archive_members(source_qza)
        loaded.validate(level="max")


def test_snapshot_ref_keeps_independent_edits(cache):
    with cache:
        from rachis.core.archive.archiver import Archiver, _CacheV2Archive

        a = artifact()
        a.add_annotation(Note("original", text="value"))
        independent = Artifact._from_archiver(
            Archiver.from_archive(
                _CacheV2Archive(cache, cache.snapshot_ref(a._archiver.ref_id))
            )
        )
        independent.remove_annotation("original")
        assert a.get_annotation("original").contents == "value"
        assert list(independent.iter_annotations()) == []


def test_immutable_payload_corruption_is_detected(cache):
    with cache:
        result = artifact()
        (data_dir,) = cache.path.glob(
            f"immutable/archives/*/{result.uuid}/data"
        )
        (data_dir / "ints.txt").write_text("999\n")
        with pytest.raises(ValidationError, match="Changed files"):
            result.validate_checksums()
        (data_dir / "ints.txt").write_text("1\n2\n3\n")
        (data_dir / "extra.txt").write_text("extra")
        with pytest.raises(ValidationError, match="Unrecognized files"):
            result.validate_checksums()


def test_pack_publication_race_and_conflict(cache, tmp_path, sample_metadata):
    import hashlib
    from rachis.core.util import to_checksum_format

    original_qza = tmp_path / "original.qza"
    redacted_qza = tmp_path / "redacted.qza"
    with CacheV1(tmp_path / "source-cache"):
        source = dummy_plugin.actions.identity_with_metadata(
            artifact(), sample_metadata
        ).out
        original_metadata = source.metadata_paths()[0][0].read_bytes()
        source.save(original_qza)
        source.redact_metadata()
        source.save(redacted_qza)

    with cache:

        def load(path):
            with cache:
                return Artifact.load(path)

        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            original, redacted = executor.map(
                load, [original_qza, redacted_qza]
            )
        assert (
            original.metadata_paths()[0][0].read_bytes() == original_metadata
        )
        assert redacted.metadata_paths()[0][0].read_bytes() == b""
        original.validate(level="max")
        redacted.validate(level="max")

        # Corrupt the same node's original metadata to test rejection.
        conflicting = archive_members(original_qza)
        conflicting["provenance/action/metadata.tsv"] = b"different originals"
        conflicting["checksums.sha512"] = (
            "\n".join(
                to_checksum_format(name, hashlib.sha512(data).hexdigest())
                for name, data in sorted(conflicting.items())
                if name != "checksums.sha512"
                and not name.startswith("annotations/")
            )
            + "\n"
        ).encode()
        bad = tmp_path / "conflicting.qza"
        write_archive(bad, str(source.uuid), conflicting)
        with pytest.raises(
            ValueError, match="Conflicting provenance originals"
        ):
            Artifact.load(bad)
        original.validate(level="max")
        assert (
            original.metadata_paths()[0][0].read_bytes() == original_metadata
        )


def test_publication_crash_keeps_pending_ownership(cache, monkeypatch):
    with cache:
        original_rename = Path.rename

        def interrupt(self, target):
            if self.name.startswith(".pending.mutable.scopes."):
                raise RuntimeError(
                    "simulated interruption after directory rename"
                )
            return original_rename(self, target)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "rename", interrupt)
            with pytest.raises(RuntimeError):
                cache.root_scope.child()
        marker = next(
            cache.process_root.glob(".pending.mutable.scopes.*.iref")
        )
        _, identity, target = cache.marker_target(marker)
        cache.garbage_collection(deep=True)
        assert target.exists()
        assert (
            cache.process_root / marker.name.removeprefix(".pending.")
        ).exists()
        cache.release(cache.process_root, "scope", identity)
        cache.garbage_collection()
        assert not target.exists()


def test_acquisition_race_with_gc_is_retained_or_clean_failure(cache):
    with cache:
        child = cache.root_scope.child()
        directory = child.acquire_directory()
        ref_id = directory.ref_id
        child.close()

        def acquire():
            try:
                cache.root_scope.adopt(ref_id)
                return True
            except KeyError:
                return False

        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            future = executor.submit(acquire)
            executor.submit(cache.garbage_collection).result()
            retained = future.result()
        assert directory.path.exists() == retained


def test_collection_and_recycle_api(cache):
    with cache:
        from rachis import ResultCollection

        one, two = artifact(), artifact()
        saved = cache.save_collection(
            ResultCollection({"second": two, "first": one}), "collection"
        )
        assert list(saved) == ["second", "first"]
        assert saved["first"].uuid == one.uuid
        pool = cache.create_pool("recycle")
        with pool:
            pool.save(one)
        assert str(one.uuid) in pool.get_data()
        assert pool.load(str(one.uuid)).view(list) == [1, 2, 3]
        cache.remove("collection")
        assert saved["first"].view(list) == [1, 2, 3]
        cache.remove("recycle")


def test_recursive_deletion_never_holds_metadata_lock(cache, monkeypatch):
    with cache:
        import rachis.core.cache_v2 as module

        original = module.shutil.rmtree
        calls = []

        def check(path, *args, **kwargs):
            assert not getattr(cache.lock._local, "depth", 0)
            calls.append(path)
            return original(path, *args, **kwargs)

        child = cache.root_scope.child()
        child.acquire_directory()
        child.close()
        monkeypatch.setattr(module.shutil, "rmtree", check)
        cache.garbage_collection()
        assert calls


def test_cross_backend_transfer(cache, tmp_path):
    with CacheV1(tmp_path / "source-cache"):
        source = artifact()
    legacy = CacheV1(tmp_path / "intermediate-cache")
    with legacy:
        old = legacy.save(source, "old")
        assert old.view(list) == [1, 2, 3]
    with cache:
        modern = cache.save(old, "modern")
        assert modern.uuid == source.uuid
        assert modern.view(list) == [1, 2, 3]
        modern.validate(level="max")


def test_annotation_broadcast_deduplicates_shared_handles(cache):
    with cache:
        a, b = artifact(), artifact()
        cache.save(a, "shared")
        one, two = cache.load("shared"), cache.load("shared")
        cache.broadcast_annotation(
            [one, two, b], Note("broadcast", text="value")
        )
        assert one.get_annotation("broadcast").contents == "value"
        assert (
            two.get_annotation("broadcast").id
            == one.get_annotation("broadcast").id
        )
        assert b.get_annotation("broadcast").contents == "value"
        assert len(list(a.iter_annotations())) == 1


def test_finalization_preserves_reserved_ref_marker(cache, tmp_path):
    qza = tmp_path / "source.qza"
    with CacheV1(tmp_path / "source-cache"):
        source = artifact()
        source.save(qza)
    root = Path(Artifact.extract(qza, tmp_path / "extracted"))
    with cache:
        reserved = str(source.uuid)
        workspace = cache.reserve_result(reserved)
        ref_id = workspace.ref_id
        marker = workspace.root / ".iref"
        inode = marker.stat().st_ino
        cache.retain_incomplete(workspace, "resume")
        with cache.resume("resume") as producer:
            producer.commit_cell("000000", "attempt1", b"value")
            finalized = producer.finalize(root)
        assert finalized.ref_id == ref_id
        assert str(finalized.uuid) == reserved
        assert marker.stat().st_ino == inode
        assert cache.load("resume").view(list) == [1, 2, 3]
        assert not (marker.parent / "cells").exists()


def _worker_output(cache_path):
    cache = Cache(cache_path)
    pool = cache.create_pool("results", reuse=True)
    with pool:
        output = artifact()
        pool.save(output)
    cache.close()
    return str(output.uuid)


def test_process_exit_preserves_recycle_owned_results(cache):
    with cache:
        import multiprocessing

        pool = cache.create_pool("results")
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(
            1, mp_context=context
        ) as worker:
            identity = worker.submit(_worker_output, str(cache.path)).result(
                timeout=60
            )
        assert pool.load(identity).view(list) == [1, 2, 3]
        cache.garbage_collection(deep=True)
        assert pool.load(identity).view(list) == [1, 2, 3]


def test_pack_catalogues_follow_ordinary_ownership_gc(cache):
    with cache:
        scope = cache.root_scope.child()
        with scope:
            a = artifact()
            a.add_annotation(Note("note", text="contents"))
            aid = str(a.uuid)
            annotation_id = str(a.get_annotation("note").id)
            scope_id = scope.scope_id
        cache.garbage_collection()
        assert not cache.object_path("scope", scope_id).exists()
        assert not cache.object_path("artifact", aid).exists()
        assert not pack_path(cache, "provenance", aid).exists()
        assert not pack_path(cache, "annotations", annotation_id).exists()


def test_uuid_hits_reuse_immutable_archive_metadata(cache, tmp_path):
    with cache:
        a = artifact()
        source = tmp_path / "source.qza"
        a.save(source)
        original = archive_members(source)
        one, two = Artifact.load(source), Artifact.load(source)
        assert one._archiver.ref_id != two._archiver.ref_id
        _, immutable = cache.attachment(one._archiver.ref_id)
        assert cache.attachment(two._archiver.ref_id)[1] == immutable
        assert (immutable / "VERSION").read_bytes() == original["VERSION"]
        assert (immutable / "metadata.yaml").read_bytes() == original[
            "metadata.yaml"
        ]
        assert (immutable / "checksums.sha512").read_bytes() == original[
            "checksums.sha512"
        ]
        for result in (one, two):
            root = cache.object_path("archives", result._archiver.ref_id)
            assert not (root / "archive-metadata").exists()
            assert not (root / "archive-checksums.sha512").exists()
            assert not (root / "annotations-order.json").exists()
            assert not (root / "whiteout.jsonl").exists()
            exported = tmp_path / "exported.qza"
            result.save(exported)
            assert archive_members(exported) == original
