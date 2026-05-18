from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.threemf import read_plate_thumbnail


def _build_3mf(project_settings: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps(project_settings).encode("utf-8"),
        )
    return out.getvalue()


def _read_project_settings(file_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        return json.loads(zf.read("Metadata/project_settings.config").decode("utf-8"))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.cache import TokenCache

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Don't enter TestClient as a context manager — that would fire the
    # FastAPI lifespan, which loads vendor profiles and pokes at the real
    # orca-headless binary. We only need a hot token cache to exercise the
    # 3MF + prepare endpoints.
    main.app.state.token_cache = TokenCache(
        cache_dir=cache_dir,
        max_bytes=5_000_000,
        max_files=20,
    )

    def _fake_get_profile(category: str, slug: str) -> dict:
        if category == "machine" and slug == "GM020":
            return {"name": "Bambu Lab A1 mini 0.4 nozzle"}
        if category == "process" and slug == "GP000":
            return {"name": "0.20mm Standard @BBL A1M"}
        raise KeyError((category, slug))

    monkeypatch.setattr(main, "get_profile", _fake_get_profile)
    monkeypatch.setattr(
        main,
        "_resolve_plate_type_label",
        lambda machine_id, plate_type: "Textured PEI Plate" if plate_type else "",
        raising=False,
    )

    yield TestClient(main.app)


def test_prepare_unknown_token_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/3mf/missing/prepare",
        json={"machine_id": "GM020", "process_id": "GP000"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "token_unknown"


def test_prepare_bakes_settings_and_thumbnail(client: TestClient) -> None:
    payload = _build_3mf({
        "printer_settings_id": "Old Printer",
        "print_settings_id": "Old Process",
        "curr_bed_type": "Cool Plate",
        "different_settings_to_system": ["existing_key"],
    })
    up = client.post(
        "/3mf",
        files={"file": ("input.3mf", payload, "application/octet-stream")},
    )
    input_token = up.json()["token"]

    png_bytes = b"\x89PNG\r\n\x1a\nfake-prepared-png"
    resp = client.post(
        f"/3mf/{input_token}/prepare",
        json={
            "machine_id": "GM020",
            "process_id": "GP000",
            "plate_type": "textured_pei_plate",
            "process_overrides": {"layer_height": "0.16", "wall_loops": "3"},
            "thumbnail_png_base64": base64.b64encode(png_bytes).decode("ascii"),
        },
    )

    assert resp.status_code == 200, resp.text
    prepared_token = resp.json()["input_token"]
    assert prepared_token != input_token

    dl = client.get(f"/3mf/{prepared_token}")
    assert dl.status_code == 200
    settings = _read_project_settings(dl.content)

    assert settings["printer_settings_id"] == "Bambu Lab A1 mini 0.4 nozzle"
    assert settings["print_settings_id"] == "0.20mm Standard @BBL A1M"
    assert settings["curr_bed_type"] == "Textured PEI Plate"
    assert settings["layer_height"] == "0.16"
    assert settings["wall_loops"] == "3"
    # `different_settings_to_system[0]` should now include the override keys
    # alongside the pre-existing process-domain entries.
    first_group = set(settings["different_settings_to_system"][0].split(";"))
    assert {"existing_key", "layer_height", "wall_loops"} <= first_group

    assert read_plate_thumbnail(dl.content, plate=1, kind="main") == png_bytes


def test_prepare_with_empty_overrides_only_rewrites_preset_names(client: TestClient) -> None:
    payload = _build_3mf({
        "printer_settings_id": "Old Printer",
        "print_settings_id": "Old Process",
    })
    up = client.post(
        "/3mf",
        files={"file": ("input.3mf", payload, "application/octet-stream")},
    )

    resp = client.post(
        f"/3mf/{up.json()['token']}/prepare",
        json={"machine_id": "GM020", "process_id": "GP000"},
    )
    assert resp.status_code == 200, resp.text
    prepared = client.get(f"/3mf/{resp.json()['input_token']}")
    settings = _read_project_settings(prepared.content)

    assert settings["printer_settings_id"] == "Bambu Lab A1 mini 0.4 nozzle"
    assert settings["print_settings_id"] == "0.20mm Standard @BBL A1M"
    # No bed type override when plate_type wasn't supplied.
    assert "curr_bed_type" not in settings
