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
    token, sha, size, _ = cache.put(payload)
    assert isinstance(token, str) and len(token) > 0
    assert sha == expected_sha
    assert size == len(payload)


def test_get_returns_path_to_stored_bytes(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    payload = b"some 3mf bytes"
    token, _, _, _ = cache.put(payload)
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
    token, _, _, _ = cache.put(payload)
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


def test_max_files_eviction(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=2)
    t1, _, _, _ = cache.put(b"a")
    t2, _, _, _ = cache.put(b"b")
    t3, _, _, _ = cache.put(b"c")
    # t1 should be evicted (oldest)
    with pytest.raises(KeyError):
        cache.path(t1)
    assert cache.path(t2).read_bytes() == b"b"
    assert cache.path(t3).read_bytes() == b"c"


def test_get_marks_most_recently_used(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=2)
    t1, _, _, _ = cache.put(b"a")
    t2, _, _, _ = cache.put(b"b")
    cache.path(t1)  # touch t1, making t2 the LRU
    t3, _, _, _ = cache.put(b"c")
    with pytest.raises(KeyError):
        cache.path(t2)
    assert cache.path(t1).read_bytes() == b"a"
    assert cache.path(t3).read_bytes() == b"c"


def test_max_files_eviction_returns_evicted_tokens(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=2)
    t1, _, _, _ = cache.put(b"a")
    t2, _, _, _ = cache.put(b"b")
    t3, _, _, evicted = cache.put(b"c")
    assert evicted == [t1]
    assert t2 != t1 and t3 != t1


def test_max_bytes_eviction(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=2, max_files=100)
    t1, _, _, _ = cache.put(b"a")
    t2, _, _, _ = cache.put(b"b")
    t3, _, _, evicted = cache.put(b"c")
    assert evicted == [t1]
    with pytest.raises(KeyError):
        cache.path(t1)
    assert cache.path(t2).read_bytes() == b"b"
    assert cache.path(t3).read_bytes() == b"c"


def test_payload_exceeding_max_bytes_keeps_new_entry(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=10, max_files=100)
    token, _, _, _ = cache.put(b"x" * 100)
    # The new entry is over-cap, but must still be retrievable.
    assert cache.path(token).read_bytes() == b"x" * 100


def test_delete_token(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    t, _, _, _ = cache.put(b"abc")
    assert cache.delete(t) is True
    with pytest.raises(KeyError):
        cache.path(t)


def test_delete_unknown_returns_false(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    assert cache.delete("nope") is False


def test_clear_removes_all(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    cache.put(b"a")
    cache.put(b"b")
    evicted, freed = cache.clear()
    assert evicted == 2
    assert freed == 2
    assert cache.stats()["count"] == 0


def test_stats_shape(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=100, max_files=5)
    cache.put(b"hello")
    s = cache.stats()
    assert s == {"count": 1, "total_bytes": 5, "max_bytes": 100, "max_files": 5}
