"""Cache V2 acceptance tests: archive semantics and ownership invariants."""

import concurrent.futures
import json
from pathlib import Path
import pickle
import uuid
import zipfile

import pytest

from rachis import Cache, CacheV1, CacheV2, Artifact, Note
from rachis.core.archive.archiver_v2 import commit_whiteouts, pack_path
from rachis.core.testing.type import IntSequence1


@pytest.fixture
def cache(tmp_path):
    cache = Cache(tmp_path / "cache")
    with cache:
        yield cache
    cache.close()


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


def test_named_keys_forward_and_survive_retarget(cache):
    a = artifact()
    cache.save(a, "a")
    one, two = cache.load("a"), cache.load("a")
    assert one._archiver.ref_id != two._archiver.ref_id
    assert cache.editable_ref(one._archiver.ref_id) == a._archiver.ref_id
    one.add_annotation(Note("shared", text="contents"))
    assert two.get_annotation("shared").contents == "contents"
    cache.save(artifact(), "a")
    cache.remove("a")
    assert one.view(list) == [1, 2, 3]
    assert two.get_annotation("shared").contents == "contents"
    one.remove_annotation("shared")
    assert list(two.iter_annotations()) == []


def test_uuid_hit_keeps_independent_annotations(cache, tmp_path):
    a = artifact()
    a.add_annotation(Note("a", text="first"))
    first = tmp_path / "first.qza"
    a.save(first)
    a.remove_annotation("a")
    a.add_annotation(Note("b", text="second"))
    second = tmp_path / "second.qza"
    a.save(second)
    b, c = Artifact.load(first), Artifact.load(second)
    assert b._archiver.ref_id != c._archiver.ref_id
    assert (
        cache.attachment(b._archiver.ref_id)[1]
        == cache.attachment(c._archiver.ref_id)[1]
    )
    assert b.get_annotation("a").contents == "first"
    assert c.get_annotation("b").contents == "second"
    with pytest.raises(KeyError):
        b.get_annotation("b")


def test_snapshot_consistent_and_explicit_materialization(cache):
    a = artifact()
    a.add_annotation(Note("first", text="keep"))
    with a._archiver.snapshot() as snapshot:
        members = snapshot.members()
        a.remove_annotation("first")
        a.add_annotation(Note("second", text="new"))
        assert snapshot.members() == members
        with snapshot.materialize("annotations") as materialized:
            assert (
                list(materialized.path.rglob("note.txt"))[0].read_text()
                == "keep"
            )
    with pytest.raises(TypeError):
        Path(a._archiver.root_dir)


@pytest.mark.parametrize("failure", ["opening", "indexing"])
def test_failed_snapshot_releases_every_opened_pack_and_scope(
    cache, monkeypatch, failure
):
    import rachis.core.archive.archiver_v2 as module

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
    a = artifact()
    a.add_annotation(Note("note", text="current contents"))
    annotation_id = str(a.get_annotation("note").id)
    root = a._archiver.root_dir
    with a.snapshot() as view:
        expected_members = view.members()

    def unexpected(*args, **kwargs):
        pytest.fail("an individual path query must not allocate a snapshot")

    monkeypatch.setattr(a._archiver, "snapshot", unexpected)
    with monkeypatch.context() as patch:
        patch.setattr(cache.root_scope, "child", unexpected)
        patch.setattr(cache, "garbage_collection", unexpected)
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
        assert all((root / member).is_file() for member in expected_members)
        assert {p.name for p in root.iterdir()} == {
            "VERSION", "metadata.yaml", "checksums.sha512", "data",
            "provenance", "annotations",
        }
        assert {p.member for p in root.rglob("*")} == set(expected_members)
        with pytest.raises(FileNotFoundError):
            (root / "provenance/missing").read_bytes()
        with pytest.raises(FileNotFoundError):
            (root / "checksums.md5").read_bytes()


def test_single_member_reads_only_open_the_selected_pack(cache, monkeypatch):
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
            pytest.fail("individual reads must not load pack checksum indexes")
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
    a = artifact()
    contents = "retained contents " * 10000
    a.add_annotation(Note("note", text=contents))
    identity = str(a.get_annotation("note").id)
    cache.save(a, "shared")
    reader, writer = cache.load("shared"), cache.load("shared")
    ref = cache.object_path("ref", cache.editable_ref(reader._archiver.ref_id))
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


def test_bulk_export_keeps_capture_when_named_view_changes(
    cache, tmp_path, monkeypatch
):
    import hashlib
    from rachis.core.archive.archiver_v2 import ArchiveSnapshot
    from rachis.core.util import to_checksum_format

    source = artifact()
    original = tmp_path / "original.qza"
    source.save(original)
    identity = str(uuid.uuid4())
    members = {
        name: data.replace(str(source.uuid).encode(), identity.encode())
        for name, data in archive_members(original).items()
        if name != "checksums.sha512"
    }
    members["provenance/action/action.yaml"] += (
        b"parameters: [{metadata: !metadata metadata.tsv}]\n"
    )
    members["provenance/action/metadata.tsv"] = b"id\tvalue\none\toriginal\n"
    members["checksums.sha512"] = (
        "\n".join(
            to_checksum_format(name, hashlib.sha512(data).hexdigest())
            for name, data in members.items()
        ) + "\n"
    ).encode()
    full = tmp_path / "full.qza"
    write_archive(full, identity, members)
    a = Artifact.load(full)
    a.add_annotation(Note("old", text="original annotation"))
    cache.save(a, "shared")
    reader, writer = cache.load("shared"), cache.load("shared")
    baseline = tmp_path / "baseline.qza"
    reader.save(baseline)
    original_open = ArchiveSnapshot.open_member
    edited = False

    def open_member(snapshot, member):
        nonlocal edited
        if not edited:
            edited = True
            writer.redact_metadata()
            writer.remove_annotation("old")
            writer.add_annotation(Note("new", text="later annotation"))
        return original_open(snapshot, member)

    with monkeypatch.context() as patch:
        patch.setattr(ArchiveSnapshot, "open_member", open_member)
        exported = tmp_path / "exported.qza"
        reader.save(exported)
    assert edited
    assert archive_members(exported) == archive_members(baseline)
    assert reader.metadata_paths()[0][0].read_bytes() == b""
    assert reader.get_annotation("new").contents == "later annotation"
    with pytest.raises(KeyError):
        reader.get_annotation("old")
    diff = Artifact.load(exported)._archiver.validate_checksums()
    assert not (diff.added or diff.removed or diff.changed)


def test_scopes_adopt_and_recycle_retains_contents(cache):
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
    child = cache.root_scope.child()
    with pytest.raises(ValueError, match="acyclic"):
        child.retain_scope(cache.root_scope)
    fake = child.path / "ref.fake.iref"
    fake.write_text("../../escape/\n")
    with pytest.raises(ValueError):
        cache.marker_target(fake)
    fake.unlink()
    child.close()


def test_pickle_dispatch(cache):
    assert pickle.loads(pickle.dumps(cache)) is cache


@pytest.mark.parametrize("version", range(1, 7))
def test_legacy_versions_roundtrip_and_indexes(cache, tmp_path, version):
    data = Path(__file__).parents[1] / "archive/provenance_lib/tests/data"
    directory = data / f"concated-ints-v{version}"
    assert directory.is_dir(), f"Missing legacy archive fixture: {directory}"
    root, = (p for p in directory.iterdir() if p.is_dir())
    src = tmp_path / f"v{version}.qza"
    with zipfile.ZipFile(src, "w") as z:
        for file in root.rglob("*"):
            if file.is_file():
                z.write(
                    file,
                    root.name + "/" + file.relative_to(root).as_posix(),
                )
    before = archive_members(src)
    loaded = cache.import_archive(src, replay=True)
    dest = tmp_path / f"out-v{version}.qza"
    loaded.save(dest)
    assert archive_members(dest) == before
    with loaded.snapshot() as view:
        assert loaded.view.members() == view.members()
        for member in view.members():
            assert loaded.view.is_file(member)
        for algorithm in view.original:
            member = "checksums." + algorithm
            with loaded.view.open_member(member) as stream:
                assert stream.read() == view.manifest(algorithm)
    _, physical = cache.attachment(loaded.ref_id)
    assert (physical / "data-checksums.sha512").is_file()
    for pack in (physical / "provenance").glob("*.zip"):
        with zipfile.ZipFile(pack) as z:
            index = z.read("checksums.sha512").decode()
            assert index


def test_shared_redaction_and_enrichment(cache, tmp_path):
    a = artifact()
    src = tmp_path / "base.qza"
    a.save(src)
    members = archive_members(src)
    # Add eligible metadata to the node's action while retaining its UUID.
    action = members["provenance/action/action.yaml"]
    action += b"parameters: [{metadata: !metadata metadata.tsv}]\n"
    members["provenance/action/action.yaml"] = action
    members["provenance/action/metadata.tsv"] = b"id\tvalue\nsecret\tprivate\n"
    members.pop("checksums.sha512")
    identity = str(uuid.uuid4())
    # Rewrite both self metadata UUIDs so this is a new logical node.
    old = str(a.uuid)
    members = {
        n: d.replace(old.encode(), identity.encode())
        for n, d in members.items()
    }
    full = tmp_path / "full.qza"
    write_archive(full, identity, members)
    redacted = dict(members)
    redacted["provenance/action/metadata.tsv"] = b""
    redacted_path = tmp_path / "redacted.qza"
    write_archive(redacted_path, identity, redacted)
    first = Artifact.load(redacted_path)
    second = Artifact.load(full)
    assert first.metadata_paths()[0][0].read_bytes() == b""
    assert second.metadata_paths()[0][0].read_bytes().startswith(b"id")
    cache.save(second, "shared")
    one, two = cache.load("shared"), cache.load("shared")
    record = {(identity, "action/metadata.tsv")}
    commit_whiteouts([one._archiver, two._archiver], record)
    assert two.metadata_paths()[0][0].read_bytes() == b""
    dest = tmp_path / "export.qza"
    first.save(dest)
    assert archive_members(dest)["provenance/action/metadata.tsv"] == b""
    assert b"private" not in dest.read_bytes()
    with zipfile.ZipFile(pack_path(cache, "provenance", identity)) as z:
        assert b"private" in z.read(identity + "/action/metadata.tsv")


def test_metadata_lock_excludes_threads(cache):
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
    reopened.root_scope.release(ref_id)
    reopened.garbage_collection(deep=True)
    assert not reopened.object_path("ref", ref_id).exists()
    reopened.close()


@pytest.mark.parametrize("filesystem", ["nfs", "unknown"])
def test_gc_runs_on_all_filesystem_types(tmp_path, monkeypatch, filesystem):
    from types import SimpleNamespace
    import rachis.core.cache_v2 as module

    monkeypatch.setattr(
        module.psutil, "disk_partitions",
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
    import rachis.core.cache_v2 as module
    from rachis.core.cache_stat import OwnershipStat

    if kind == "scope":
        scope = cache.root_scope.child()
        path = scope.path
        marker = path / ".iref"
        scope.close()
    else:
        identity = str(uuid.uuid4())
        category = "annotations" if kind == "annotation_pack" else "provenance"
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
    a = artifact()
    source = tmp_path / "modern.qza"
    a.save(source)
    members = archive_members(source)
    zero = {
        n: d
        for n, d in members.items()
        if n.startswith("data/") or n == "metadata.yaml"
    }
    zero["VERSION"] = b"QIIME 2\narchive: 0\nframework: 2.0.0\n"
    old = tmp_path / "zero.qza"
    write_archive(old, str(a.uuid), zero)
    loaded = Artifact.load(old)
    assert loaded.archive_version == "0"
    assert loaded._archiver.provenance_dir is None
    with loaded.snapshot() as view:
        assert not (view.root / "provenance").exists()
    dest = tmp_path / "zero-out.qza"
    loaded.save(dest)
    assert archive_members(dest) == zero


def test_snapshot_ref_keeps_independent_edits(cache):
    from rachis.core.archive.archiver_v2 import V2Archiver

    a = artifact()
    a.add_annotation(Note("original", text="value"))
    independent = Artifact._from_archiver(
        V2Archiver(cache, cache.snapshot_ref(a._archiver.ref_id))
    )
    independent.remove_annotation("original")
    assert a.get_annotation("original").contents == "value"
    assert list(independent.iter_annotations()) == []


def test_immutable_payload_corruption_is_detected(cache):
    a = artifact()
    (a._archiver.data_dir / "ints.txt").write_text("999\n")
    diff = a._archiver.validate_checksums()
    assert "data/ints.txt" in diff.changed
    (a._archiver.data_dir / "extra.txt").write_text("extra")
    diff = a._archiver.validate_checksums()
    assert "data/extra.txt" in diff.added


def test_pack_publication_race_and_conflict(cache, tmp_path):
    a = artifact()
    src = tmp_path / "base.qza"
    a.save(src)
    members = archive_members(src)
    identity = str(uuid.uuid4())
    members = {
        n: d.replace(str(a.uuid).encode(), identity.encode())
        for n, d in members.items()
    }
    members.pop("checksums.sha512")
    members["provenance/action/action.yaml"] += (
        b"parameters: [{metadata: !metadata metadata.tsv}]\n"
    )
    members["provenance/action/metadata.tsv"] = b"secret originals"
    full = tmp_path / "full.qza"
    write_archive(full, identity, members)
    redacted = dict(members)
    redacted["provenance/action/metadata.tsv"] = b""
    hidden = tmp_path / "hidden.qza"
    write_archive(hidden, identity, redacted)

    def load(path):
        return cache.import_archive(path)

    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        results = list(executor.map(load, [full, hidden]))
    with zipfile.ZipFile(pack_path(cache, "provenance", identity)) as z:
        assert z.read(identity + "/action/metadata.tsv") == b"secret originals"
    for result, visible in zip(results, [b"secret originals", b""]):
        assert result.metadata_paths()[0][0].read_bytes() == visible
    conflicting = dict(members)
    conflicting["provenance/action/metadata.tsv"] = b"different originals"
    bad = tmp_path / "bad.qza"
    write_archive(bad, identity, conflicting)
    with pytest.raises(ValueError, match="Conflicting provenance originals"):
        cache.import_archive(bad)
    with zipfile.ZipFile(pack_path(cache, "provenance", identity)) as z:
        assert z.read(identity + "/action/metadata.tsv") == b"secret originals"


def test_publication_crash_keeps_pending_ownership(cache, monkeypatch):
    original_rename = Path.rename

    def interrupt(self, target):
        if self.name.startswith(".pending.scope."):
            raise RuntimeError("simulated interruption after directory rename")
        return original_rename(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt)
        with pytest.raises(RuntimeError):
            cache.root_scope.child()
    marker = next(cache.root_scope.path.glob(".pending.scope.*.iref"))
    _, identity, target = cache.marker_target(marker)
    cache.garbage_collection(deep=True)
    assert target.exists()
    assert (cache.root_scope.path / f"scope.{identity}.iref").exists()
    cache.release(cache.root_scope.path, "scope", identity)
    cache.garbage_collection()
    assert not target.exists()


def test_acquisition_race_with_gc_is_retained_or_clean_failure(cache):
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
    a = artifact()
    legacy = CacheV1(tmp_path / "legacy")
    old = legacy.save(a, "old")
    assert old.view(list) == [1, 2, 3]
    modern = cache.save(old, "modern")
    assert modern.uuid == a.uuid
    assert modern.view(list) == [1, 2, 3]


def test_annotation_broadcast_deduplicates_forwarding_handles(cache):
    a, b = artifact(), artifact()
    cache.save(a, "shared")
    one, two = cache.load("shared"), cache.load("shared")
    cache.broadcast_annotation([one, two, b], Note("broadcast", text="value"))
    assert one.get_annotation("broadcast").contents == "value"
    assert (
        two.get_annotation("broadcast").id
        == one.get_annotation("broadcast").id
    )
    assert b.get_annotation("broadcast").contents == "value"
    assert len(list(a.iter_annotations())) == 1


def test_finalization_preserves_reserved_ref_marker(cache, tmp_path):
    import hashlib
    from rachis.core.util import to_checksum_format

    a = artifact()
    exported = tmp_path / "a.qza"
    a.save(exported)
    members = archive_members(exported)
    reserved = str(uuid.uuid4())
    members = {
        n: d.replace(str(a.uuid).encode(), reserved.encode())
        for n, d in members.items()
        if n != "checksums.sha512"
    }
    members["checksums.sha512"] = (
        "\n".join(
            to_checksum_format(n, hashlib.sha512(data).hexdigest())
            for n, data in members.items()
        )
        + "\n"
    ).encode()
    root = tmp_path / reserved
    for name, data in members.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    workspace = cache.reserve_result(reserved)
    ref_id = workspace.ref_id
    marker = cache.object_path("ref", ref_id) / ".iref"
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


def test_uuid_hits_preserve_ref_owned_root_bytes_and_manifest(cache, tmp_path):
    import hashlib
    from rachis.core.util import to_checksum_format

    a = artifact()
    source = tmp_path / "first.qza"
    a.save(source)
    original = archive_members(source)
    changed = dict(original)
    changed["metadata.yaml"] += (
        b"\nsource-description: second outer metadata\n"
    )
    changed["VERSION"] = b"QIIME 2\narchive: 7.1\nframework: 2026.9.0\n"
    covered = {
        n: d
        for n, d in changed.items()
        if n != "checksums.sha512" and not n.startswith("annotations/")
    }
    changed["checksums.sha512"] = (
        "\r\n".join(
            to_checksum_format(n, hashlib.sha512(data).hexdigest())
            for n, data in reversed(list(covered.items()))
        )
        + "\r\n"
    ).encode()
    second = tmp_path / "second.qza"
    write_archive(second, str(a.uuid), changed)
    one, two = Artifact.load(source), Artifact.load(second)
    marker = cache.attachment(one._archiver.ref_id)[1] / ".iref"
    assert (
        marker.stat().st_ino
        == (cache.attachment(two._archiver.ref_id)[1] / ".iref").stat().st_ino
    )
    exported = tmp_path / "exported.qza"
    two.save(exported)
    assert archive_members(exported) == changed
    one.save(exported)
    assert archive_members(exported) == original
