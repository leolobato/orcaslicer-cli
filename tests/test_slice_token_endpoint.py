from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import config as cfg
from app import main
from app import profiles


@pytest.fixture
def client(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    user_dir = tmp_path / "user"
    profiles_dir = tmp_path / "vendor_profiles"
    for p in (cache_dir, user_dir, profiles_dir):
        p.mkdir(parents=True, exist_ok=True)

    old_cache_dir = cfg.CACHE_DIR
    old_cache_max_bytes = cfg.CACHE_MAX_BYTES
    old_cache_max_files = cfg.CACHE_MAX_FILES
    old_use_flag = cfg.USE_HEADLESS_BINARY
    old_main_upd = main.USER_PROFILES_DIR
    old_profiles_dir = profiles.PROFILES_DIR
    old_profiles_upd = profiles.USER_PROFILES_DIR

    cfg.CACHE_DIR = cache_dir
    cfg.CACHE_MAX_BYTES = 1_000_000
    cfg.CACHE_MAX_FILES = 10
    cfg.USE_HEADLESS_BINARY = True
    main.USER_PROFILES_DIR = str(user_dir)
    profiles.PROFILES_DIR = str(profiles_dir)
    profiles.USER_PROFILES_DIR = str(user_dir)

    with TestClient(main.app) as c:
        yield c

    cfg.CACHE_DIR = old_cache_dir
    cfg.CACHE_MAX_BYTES = old_cache_max_bytes
    cfg.CACHE_MAX_FILES = old_cache_max_files
    cfg.USE_HEADLESS_BINARY = old_use_flag
    main.USER_PROFILES_DIR = old_main_upd
    profiles.PROFILES_DIR = old_profiles_dir
    profiles.USER_PROFILES_DIR = old_profiles_upd


def test_slice_v2_uses_binary(client: TestClient, tmp_path: Path) -> None:
    payload = b"PK\x03\x04 fake input 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    fake_result = {
        "status": "ok",
        "output_3mf": "/tmp/out.3mf",
        "estimate": {"time_seconds": 60, "weight_g": 1.0, "filament_used_m": [0.5]},
        "settings_transfer": {"status": "applied"},
    }

    fake_paths = {
        "machine": str(tmp_path / "m.json"),
        "process": str(tmp_path / "p.json"),
        "filaments": [str(tmp_path / "f0.json")],
    }
    # Touch the files so any read-checks pass
    for fp in [fake_paths["machine"], fake_paths["process"]] + fake_paths["filaments"]:
        Path(fp).write_text("{}")

    async def fake_slice(self, request):
        # Verify the binary received the cached input path
        assert Path(request["input_3mf"]).read_bytes() == payload
        # Pretend the binary produced this output
        Path(request["output_3mf"]).write_bytes(b"sliced bytes")
        return fake_result

    async def fake_materialize(machine_id, process_id, filament_setting_ids):
        return fake_paths

    with patch("app.binary_client.BinaryClient.slice", new=fake_slice), \
         patch("app.main.materialize_profiles_for_binary", new=fake_materialize):
        resp = client.post("/slice/v2", json={
            "input_token": token,
            "machine_id": "GM014",
            "process_id": "GP001",
            "filament_settings_ids": ["GFSA00"],
            "plate_id": 1,
        })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estimate"]["time_seconds"] == 60
    assert body["input_token"] == token
    assert "output_token" in body
    assert "output_sha256" in body
    assert body["download_url"].startswith("/3mf/")
    assert body["settings_transfer"] == {"status": "applied"}
