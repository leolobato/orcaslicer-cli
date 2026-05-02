from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config as cfg
from app import main
from app import profiles
from app.cache import TokenCache


@pytest.fixture
def client(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    user_dir = tmp_path / "user"
    profiles_dir = tmp_path / "vendor_profiles"
    cache_dir.mkdir(parents=True, exist_ok=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)

    # Patch all module-level path constants that lifespan / load_all_profiles use.
    old_cache_dir = cfg.CACHE_DIR
    old_cache_max_bytes = cfg.CACHE_MAX_BYTES
    old_cache_max_files = cfg.CACHE_MAX_FILES
    old_main_upd = main.USER_PROFILES_DIR
    old_profiles_dir = profiles.PROFILES_DIR
    old_profiles_upd = profiles.USER_PROFILES_DIR

    cfg.CACHE_DIR = cache_dir
    cfg.CACHE_MAX_BYTES = 1_000_000
    cfg.CACHE_MAX_FILES = 10
    main.USER_PROFILES_DIR = str(user_dir)
    profiles.PROFILES_DIR = str(profiles_dir)
    profiles.USER_PROFILES_DIR = str(user_dir)

    with TestClient(main.app) as c:
        yield c

    cfg.CACHE_DIR = old_cache_dir
    cfg.CACHE_MAX_BYTES = old_cache_max_bytes
    cfg.CACHE_MAX_FILES = old_cache_max_files
    main.USER_PROFILES_DIR = old_main_upd
    profiles.PROFILES_DIR = old_profiles_dir
    profiles.USER_PROFILES_DIR = old_profiles_upd


def test_upload_returns_token(client: TestClient) -> None:
    payload = b"PK\x03\x04 fake 3mf bytes"
    resp = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert "sha256" in body
    assert body["size"] == len(payload)


def test_upload_same_file_returns_same_token(client: TestClient) -> None:
    payload = b"deterministic content"
    r1 = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    r2 = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    assert r1.json()["token"] == r2.json()["token"]


def test_download_returns_bytes(client: TestClient) -> None:
    payload = b"some content"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]
    resp = client.get(f"/3mf/{token}")
    assert resp.status_code == 200
    assert resp.content == payload


def test_download_unknown_404(client: TestClient) -> None:
    resp = client.get("/3mf/nonexistent-token")
    assert resp.status_code == 404
    assert resp.json()["code"] == "token_unknown"


def test_delete_token(client: TestClient) -> None:
    up = client.post("/3mf", files={"file": ("a.3mf", b"x", "application/octet-stream")})
    token = up.json()["token"]
    resp = client.delete(f"/3mf/{token}")
    assert resp.status_code == 204
    assert client.get(f"/3mf/{token}").status_code == 404


def test_delete_unknown_404(client: TestClient) -> None:
    assert client.delete("/3mf/nonexistent").status_code == 404


def test_clear_cache(client: TestClient) -> None:
    client.post("/3mf", files={"file": ("a.3mf", b"a", "application/octet-stream")})
    client.post("/3mf", files={"file": ("b.3mf", b"b", "application/octet-stream")})
    resp = client.delete("/3mf/cache")
    assert resp.status_code == 200
    assert resp.json()["evicted"] == 2
    assert resp.json()["freed_bytes"] == 2


def test_cache_stats(client: TestClient) -> None:
    client.post("/3mf", files={"file": ("a.3mf", b"hello", "application/octet-stream")})
    resp = client.get("/3mf/cache/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["total_bytes"] == 5
    assert "max_bytes" in body
    assert "max_files" in body
