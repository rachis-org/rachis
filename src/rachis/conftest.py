"""Explicit source metadata and legacy filesystem fixtures."""

import pytest


@pytest.fixture
def legacy_tree_cache(tmp_path):
    from rachis import CacheV1

    with CacheV1(tmp_path / "legacy"):
        yield


@pytest.fixture
def sample_metadata(tmp_path):
    from rachis import Metadata

    path = tmp_path / "metadata.tsv"
    path.write_text("sample-id\tvalue\none\tprivate\n")
    return Metadata.load(path)
