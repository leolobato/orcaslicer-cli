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
        "filament_names": ["Mock Filament 0"],
        "printer_model_id": "",
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


def test_slice_v2_forwards_plate_type_as_orca_label(
    client: TestClient, tmp_path: Path,
) -> None:
    """``plate_type`` (snake_case) is resolved against the machine and
    forwarded to the binary as the OrcaSlicer ``curr_bed_type`` label, so
    the C++ side can stamp it on top of whatever the input 3MF authored."""
    payload = b"PK\x03\x04 fake input 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    fake_paths = {
        "machine": str(tmp_path / "m.json"),
        "process": str(tmp_path / "p.json"),
        "filaments": [str(tmp_path / "f0.json")],
        "filament_names": ["Mock Filament 0"],
        "printer_model_id": "",
    }
    for fp in [fake_paths["machine"], fake_paths["process"]] + fake_paths["filaments"]:
        Path(fp).write_text("{}")

    captured: dict = {}

    async def fake_slice(self, request):
        captured.update(request)
        Path(request["output_3mf"]).write_bytes(b"sliced bytes")
        return {
            "status": "ok",
            "output_3mf": request["output_3mf"],
            "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
            "settings_transfer": {"status": "applied"},
        }

    async def fake_materialize(machine_id, process_id, filament_setting_ids):
        return fake_paths

    def fake_resolve_plate(machine_id, requested, *, plate_type_api_to_orca):
        # Pretend the machine supports the requested type as-is.
        return {"resolved": requested, "match": "unchanged"}

    with patch("app.binary_client.BinaryClient.slice", new=fake_slice), \
         patch("app.main.materialize_profiles_for_binary", new=fake_materialize), \
         patch("app.main.resolve_plate_type_for_machine", new=fake_resolve_plate):
        resp = client.post("/slice/v2", json={
            "input_token": token,
            "machine_id": "GM014",
            "process_id": "GP001",
            "filament_settings_ids": ["GFSA00"],
            "plate_id": 1,
            "plate_type": "textured_pei_plate",
        })

    assert resp.status_code == 200, resp.text
    assert captured.get("plate_type") == "Textured PEI Plate", (
        "binary should receive the OrcaSlicer label, not the API value"
    )


def test_slice_v2_omits_plate_type_when_unset(
    client: TestClient, tmp_path: Path,
) -> None:
    """Without ``plate_type`` the binary receives an empty string and
    falls back to the input 3MF's authored ``curr_bed_type``."""
    payload = b"PK\x03\x04 fake input 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    fake_paths = {
        "machine": str(tmp_path / "m.json"),
        "process": str(tmp_path / "p.json"),
        "filaments": [str(tmp_path / "f0.json")],
        "filament_names": ["Mock Filament 0"],
        "printer_model_id": "",
    }
    for fp in [fake_paths["machine"], fake_paths["process"]] + fake_paths["filaments"]:
        Path(fp).write_text("{}")

    captured: dict = {}

    async def fake_slice(self, request):
        captured.update(request)
        Path(request["output_3mf"]).write_bytes(b"sliced bytes")
        return {
            "status": "ok",
            "output_3mf": request["output_3mf"],
            "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
            "settings_transfer": {},
        }

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
    assert captured.get("plate_type") == ""


def test_slice_stream_v2_emits_progress_and_result(client: TestClient, tmp_path: Path) -> None:
    payload = b"PK\x03\x04 fake input 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    fake_paths = {
        "machine": str(tmp_path / "m.json"),
        "process": str(tmp_path / "p.json"),
        "filaments": [str(tmp_path / "f0.json")],
        "filament_names": ["Mock Filament 0"],
        "printer_model_id": "",
    }
    for fp in [fake_paths["machine"], fake_paths["process"]] + fake_paths["filaments"]:
        Path(fp).write_text("{}")

    async def fake_stream(self, request):
        # Pretend the binary writes the output 3MF before yielding the result event.
        Path(request["output_3mf"]).write_bytes(b"sliced")
        yield {"type": "progress", "payload": {"phase": "loading_3mf", "percent": 0}}
        yield {"type": "progress", "payload": {"phase": "done", "percent": 100}}
        yield {"type": "result", "payload": {
            "status": "ok",
            "output_3mf": request["output_3mf"],
            "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
            "settings_transfer": {},
        }}

    async def fake_materialize(machine_id, process_id, filament_setting_ids):
        return fake_paths

    with patch("app.binary_client.BinaryClient.slice_stream", new=fake_stream), \
         patch("app.main.materialize_profiles_for_binary", new=fake_materialize):
        resp = client.post("/slice-stream/v2", json={
            "input_token": token,
            "machine_id": "GM014",
            "process_id": "GP001",
            "filament_settings_ids": ["GFSA00"],
            "plate_id": 1,
        })

    assert resp.status_code == 200
    text = resp.text
    # SSE format: each event is "event: <type>\ndata: <json>\n\n"
    assert "event: progress" in text
    assert "event: result" in text
    # The result event payload should include the output_token
    assert "output_token" in text
    assert "download_url" in text


def test_slice_v2_rejects_out_of_range_filament_map(
    client: TestClient, tmp_path: Path,
) -> None:
    """``filament_map`` carries libslic3r's per-filament-extruder index;
    values outside ``1..nozzle_count`` reach libslic3r as a corrupted
    extruder topology (the GUI never produces them), so we reject at
    the API layer with HTTP 400."""
    payload = b"PK\x03\x04 fake input 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    # Single-extruder A1 mini.
    fake_machine = {"name": "Bambu Lab A1 mini 0.4 nozzle", "nozzle_diameter": ["0.4"]}

    def fake_get_profile(category: str, slug: str):
        return fake_machine if category == "machine" else {}

    with patch("app.main.get_profile", new=fake_get_profile):
        # filament_map=[0, 2] is exactly the bug pattern: AMS tray semantics
        # leaking into libslic3r's per-extruder field.
        resp = client.post("/slice/v2", json={
            "input_token": token,
            "machine_id": "GM020",
            "process_id": "GP000",
            "filament_settings_ids": ["GFSA00_02", "GFSA00_02"],
            "filament_map": [0, 2],
            "plate_id": 1,
        })

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_filament_map"
    assert "out of range" in body["message"]
    assert "1..1" in body["message"]


def test_slice_v2_accepts_in_range_filament_map(
    client: TestClient, tmp_path: Path,
) -> None:
    """``filament_map=[1, 1]`` is valid for a single-extruder machine."""
    payload = b"PK\x03\x04 fake input 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    fake_paths = {
        "machine": str(tmp_path / "m.json"),
        "process": str(tmp_path / "p.json"),
        "filaments": [str(tmp_path / "f0.json")],
        "filament_names": ["Mock"],
        "printer_model_id": "",
    }
    for fp in [fake_paths["machine"], fake_paths["process"]] + fake_paths["filaments"]:
        Path(fp).write_text("{}")

    fake_machine = {"name": "A1 mini", "nozzle_diameter": ["0.4"]}

    async def fake_slice(self, request):
        Path(request["output_3mf"]).write_bytes(b"x")
        return {
            "status": "ok", "output_3mf": request["output_3mf"],
            "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
            "settings_transfer": {},
        }

    async def fake_materialize(machine_id, process_id, filament_setting_ids):
        return fake_paths

    def fake_get_profile(category: str, slug: str):
        return fake_machine if category == "machine" else {}

    with patch("app.binary_client.BinaryClient.slice", new=fake_slice), \
         patch("app.main.materialize_profiles_for_binary", new=fake_materialize), \
         patch("app.main.get_profile", new=fake_get_profile):
        resp = client.post("/slice/v2", json={
            "input_token": token,
            "machine_id": "GM020",
            "process_id": "GP000",
            "filament_settings_ids": ["GFSA00_02", "GFSA00_02"],
            "filament_map": [1, 1],
            "plate_id": 1,
        })

    assert resp.status_code == 200, resp.text


def test_slice_stream_v2_forwards_plate_type_as_orca_label(
    client: TestClient, tmp_path: Path,
) -> None:
    payload = b"PK\x03\x04 fake input 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    fake_paths = {
        "machine": str(tmp_path / "m.json"),
        "process": str(tmp_path / "p.json"),
        "filaments": [str(tmp_path / "f0.json")],
        "filament_names": ["Mock Filament 0"],
        "printer_model_id": "",
    }
    for fp in [fake_paths["machine"], fake_paths["process"]] + fake_paths["filaments"]:
        Path(fp).write_text("{}")

    captured: dict = {}

    async def fake_stream(self, request):
        captured.update(request)
        Path(request["output_3mf"]).write_bytes(b"sliced")
        yield {"type": "result", "payload": {
            "status": "ok",
            "output_3mf": request["output_3mf"],
            "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
            "settings_transfer": {},
        }}

    async def fake_materialize(machine_id, process_id, filament_setting_ids):
        return fake_paths

    def fake_resolve_plate(machine_id, requested, *, plate_type_api_to_orca):
        return {"resolved": requested, "match": "unchanged"}

    with patch("app.binary_client.BinaryClient.slice_stream", new=fake_stream), \
         patch("app.main.materialize_profiles_for_binary", new=fake_materialize), \
         patch("app.main.resolve_plate_type_for_machine", new=fake_resolve_plate):
        resp = client.post("/slice-stream/v2", json={
            "input_token": token,
            "machine_id": "GM014",
            "process_id": "GP001",
            "filament_settings_ids": ["GFSA00"],
            "plate_id": 1,
            "plate_type": "textured_pei_plate",
        })

    assert resp.status_code == 200
    assert captured.get("plate_type") == "Textured PEI Plate"
