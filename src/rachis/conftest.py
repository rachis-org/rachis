"""Backend-specific fixtures for tests that intentionally mutate V1 trees."""
import pytest


@pytest.fixture
def legacy_tree_cache(tmp_path):
    from rachis.core.cache import CacheV1, _CACHE
    previous = getattr(_CACHE, 'cache', None)
    _CACHE.cache = CacheV1(tmp_path / 'legacy')
    try:
        yield
    finally:
        _CACHE.cache = previous
