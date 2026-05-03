"""HTTP-level integration test for GET /3mf/{token}/inspect.

Skipped when the container at $ORCASLICER_API isn't reachable. Uses
stdlib HTTP so no `httpx` venv is needed on the host (consistent with
test_slice_v2_fidelity.py).
"""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8000")
FIXTURE_DIR = Path(__file__).resolve().parents[2].parent / "_fixture"


def _container_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_reachable(),
    reason=f"orcaslicer-cli not reachable at {API}",
)


def _post_multipart_file(url: str, file_path: Path) -> dict:
    boundary = f"----pytest{uuid.uuid4().hex}"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{file_path.name}"\r\n'.encode()
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(file_path.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url, data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30.0) as r:
        return json.loads(r.read().decode())


def test_inspect_unsliced_fixture_01() -> None:
    fp = FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    assert fp.exists()
    upload = _post_multipart_file(f"{API}/3mf", fp)
    token = upload["token"]

    data = _get_json(f"{API}/3mf/{token}/inspect")
    assert data["is_sliced"] is False
    assert data["plate_count"] == 1
    assert data["printer_model"] == "Bambu Lab A1 mini"
    assert len(data["filaments"]) == 1
    f0 = data["filaments"][0]
    assert f0["type"] == "PLA"
    assert f0["filament_id"] == "GFA00"
    assert data["bbox"] is not None
    # `use-set` now backfills used_filament_indices for un-sliced 3MFs.
    assert data["plates"][0]["used_filament_indices"] == [0]
    # Per-plate shape for un-sliced 3MFs.
    p0 = data["plates"][0]
    assert p0["name"] == ""
    assert p0["estimate"] is None
    assert p0["warnings"] == []


def test_inspect_token_unknown_returns_404() -> None:
    bogus = "no-such-token"
    req = urllib.request.Request(f"{API}/3mf/{bogus}/inspect")
    try:
        urllib.request.urlopen(req, timeout=10.0)
    except urllib.error.HTTPError as e:
        assert e.code == 404
        body = json.loads(e.read().decode())
        assert body["code"] == "token_unknown"
        return
    raise AssertionError("expected 404")


def test_inspect_thumbnail_url_serves_png() -> None:
    fp = FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    upload = _post_multipart_file(f"{API}/3mf", fp)
    token = upload["token"]

    data = _get_json(f"{API}/3mf/{token}/inspect")
    assert data["thumbnail_urls"], "inspect should list at least one thumbnail"
    main = next(
        (t for t in data["thumbnail_urls"] if t["kind"] == "main"), None,
    )
    assert main is not None

    with urllib.request.urlopen(f"{API}{main['url']}", timeout=10.0) as r:
        assert r.status == 200
        assert r.headers["content-type"] == "image/png"
        png = r.read()
    # PNG signature.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_inspect_unsliced_use_set_via_binary() -> None:
    fp = FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    upload = _post_multipart_file(f"{API}/3mf", fp)
    token = upload["token"]

    data = _get_json(f"{API}/3mf/{token}/inspect")
    assert data["is_sliced"] is False
    # `use-set` should fill in plate 1.
    assert data["use_set_per_plate"], "binary should have populated use_set_per_plate"
    assert data["use_set_per_plate"]["1"] == [0]
    assert data["plates"][0]["used_filament_indices"] == [0]
