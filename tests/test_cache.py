import hashlib
import time
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


def test_put_cache_hit_refreshes_last_access(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    payload = b"same content"
    token, _, _ = cache.put(payload)
    first_access = cache._entries[token].last_access
    # Sleep just enough for time.time() to tick on systems with low-resolution clocks
    time.sleep(0.01)
    cache.put(payload)  # cache hit
    second_access = cache._entries[token].last_access
    assert second_access > first_access


def test_put_writes_atomically_via_temp_path(cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the implementation uses temp-path + rename, not direct write."""
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    seen_paths: list[str] = []
    real_replace = __import__("os").replace
    def spy_replace(src, dst):
        seen_paths.append(str(src))
        return real_replace(src, dst)
    monkeypatch.setattr("os.replace", spy_replace)
    cache.put(b"some content")
    assert len(seen_paths) == 1
    assert ".tmp-" in seen_paths[0]
