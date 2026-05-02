import hashlib
from pathlib import Path

import pytest

from app.cache import TokenCache


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d


def test_put_returns_token_and_sha(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    payload = b"hello world"
    expected_sha = hashlib.sha256(payload).hexdigest()
    token, sha, size = cache.put(payload)
    assert isinstance(token, str) and len(token) > 0
    assert sha == expected_sha
    assert size == len(payload)


def test_get_returns_path_to_stored_bytes(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    payload = b"some 3mf bytes"
    token, _, _ = cache.put(payload)
    path = cache.path(token)
    assert path.exists()
    assert path.read_bytes() == payload


def test_unknown_token_raises(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    with pytest.raises(KeyError):
        cache.path("nonexistent")
