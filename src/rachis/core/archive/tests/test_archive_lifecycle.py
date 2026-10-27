# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Both cache backends publish archives through the same lifecycle."""

import pytest

from rachis import Artifact, CacheV1, CacheV2, Note
from rachis.core.archive import Archiver
from rachis.core.testing.type import IntSequence1


@pytest.mark.parametrize("backend", [CacheV1, CacheV2])
def test_finalize_publishes_storage_and_builds_the_common_archiver(
    backend, tmp_path
):
    source_qza = tmp_path / "source.qza"
    with CacheV1(tmp_path / "source-cache"):
        source = Artifact.import_data(IntSequence1, [1, 2, 3])
        source.add_annotation(Note("note", text="finalized annotation"))
        source.save(source_qza)
    cache = backend(tmp_path / "target-cache")
    archive = None
    try:
        with cache, Archiver.get_archive(source_qza) as incoming:
            # The lifecycle API is the subject of this storage unit test.
            archive = cache.create_archive(str(source.uuid))
            marker = None
            if isinstance(cache, CacheV2):
                marker = cache.refdir(archive.ref_id).root / ".iref"
                inode = marker.stat().st_ino
            archive.import_from(incoming)
            assert (archive.path / "data/ints.txt").read_text() == "1\n2\n3\n"
            assert str(source.uuid) not in cache.get_data()
            assert archive.finalize() is archive
            assert str(source.uuid) in cache.get_data()
            if marker is not None:
                assert marker.stat().st_ino == inode
            handle = Archiver.from_archive(archive)
            assert type(handle) is Archiver
            saved = tmp_path / "finalized.qza"
            handle.save(saved)
        with CacheV1(tmp_path / "verification-cache"):
            restored = Artifact.load(saved)
            assert restored.uuid == source.uuid
            assert restored.view(list) == [1, 2, 3]
            assert restored.get_annotation("note").contents == (
                "finalized annotation"
            )
            restored.validate(level="max")
    finally:
        if archive is not None:
            archive.close()
        if isinstance(cache, CacheV2):
            cache.close()


@pytest.mark.parametrize("backend", [CacheV1, CacheV2])
def test_failed_construction_releases_its_unpublished_storage(
    backend, tmp_path, monkeypatch
):
    from rachis.core.path import OwnedPath

    cache = backend(tmp_path / "cache")
    try:
        with cache:
            before = set(cache.get_data())
            if isinstance(cache, CacheV2):
                markers = set(cache.path.glob("mutable/archives/*/*/.iref"))
            else:
                aliases = {
                    p
                    for p in cache.process_pool.path.iterdir()
                    if p.name != "tmp"
                }

            move = OwnedPath._move_or_copy

            def fail(path, destination):
                # Fail while populating an allocated archive, after transforms.
                if (destination.parent / "VERSION").exists():
                    (destination / "partial.txt").write_text("unfinished")
                    raise RuntimeError("construction failed")
                return move(path, destination)

            monkeypatch.setattr(OwnedPath, "_move_or_copy", fail)
            with pytest.raises(RuntimeError, match="construction failed"):
                Artifact.import_data(IntSequence1, [1, 2, 3])
            cache.garbage_collection()
            assert set(cache.get_data()) == before
            if isinstance(cache, CacheV2):
                assert (
                    set(cache.path.glob("mutable/archives/*/*/.iref"))
                    == markers
                )
            else:
                # Formats may create the reusable tmp container.
                assert {
                    p
                    for p in cache.process_pool.path.iterdir()
                    if p.name != "tmp"
                } == aliases
    finally:
        if isinstance(cache, CacheV2):
            cache.close()
