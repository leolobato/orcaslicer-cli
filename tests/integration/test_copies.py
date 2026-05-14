"""Integration tests for the copies feature.

Exercises the C++ `copies` path end-to-end against a live container.

Fixture: ``tests/fixtures/copies/small.3mf`` — single-object Benchy on
A1 mini (same file as ``_fixture/01/reference-benchy-orca-no-filament-
custom-settings.3mf``). Profile IDs from the validated fidelity suite:
- machine  GM020  — Bambu Lab A1 mini 0.4 nozzle
- process  GP000  — 0.20mm Standard @BBL A1M
- filament GFSA00_02 — Bambu PLA Basic @BBL A1M

Opt-in: requires a running container reachable at ``$ORCASLICER_API``
(default ``http://localhost:8070``). Skipped when not reachable.
"""
from __future__ import annotations

import io
import json
import os
import urllib.request
import uuid
import zipfile
from pathlib import Path

import pytest


API = os.environ.get("ORCASLICER_API", "http://localhost:8070")
# parents[0] = tests/integration/, parents[1] = tests/, parents[2] = project root
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "copies" / "small.3mf"


def _container_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_reachable(),
    reason=f"orcaslicer-headless not reachable at {API}",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upload_fixture() -> str:
    """Upload the fixture and return the token."""
    boundary = f"----pytest{uuid.uuid4().hex}"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        b'Content-Disposition: form-data; name="file"; filename="small.3mf"\r\n'
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(FIXTURE.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{API}/3mf",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30.0) as r:
        return json.loads(r.read().decode())["token"]


def _slice(token: str, copies: int) -> tuple[int, dict]:
    """POST /slice/v2 and return (status_code, body)."""
    payload = json.dumps({
        "input_token": token,
        "machine_id": "GM020",
        "process_id": "GP000",
        "filament_settings_ids": ["GFSA00_02"],
        "auto_center": True,
        "copies": copies,
    }).encode()
    req = urllib.request.Request(
        f"{API}/slice/v2",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300.0) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60.0) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_copies_1_is_default_behavior() -> None:
    """copies=1 slices successfully and the response echoes copies=1."""
    token = _upload_fixture()
    status, body = _slice(token, copies=1)
    assert status == 200, f"expected 200, got {status}: {body}"
    assert body.get("copies") == 1, f"response did not echo copies=1: {body}"
    assert "output_token" in body, f"missing output_token: {body}"


def test_copies_4_succeeds_on_small_fixture() -> None:
    """copies=4 slices successfully and the output 3MF references 4 object instances.

    Verification: ``Metadata/slice_info.config`` in the sliced 3MF has one
    ``<object …/>`` entry per arranged instance.  The binary writes entries from
    ``PlateData::objects_and_instances`` (bbs_3mf.cpp:7939-7965), one entry per
    (obj_id, inst_id) pair — so 4 copies of a single object produce 4 entries.

    Note: ``Metadata/model_settings.config`` in the *output* 3MF never contains
    ``<model_instance>`` entries — those appear only in project-format input 3MFs.
    The output uses ``SaveStrategy::SkipModel`` so the model section is omitted
    entirely (bbs_3mf.cpp:7779 checks ``!m_skip_model`` before writing instances).
    """
    token = _upload_fixture()
    status, body = _slice(token, copies=4)
    assert status == 200, f"expected 200, got {status}: {body}"
    assert body.get("copies") == 4, f"response did not echo copies=4: {body}"

    out_token = body["output_token"]
    out_bytes = _get_bytes(f"{API}/3mf/{out_token}")
    out_path = Path("/tmp/copies_test_out.3mf")
    out_path.write_bytes(out_bytes)

    with zipfile.ZipFile(out_path) as zf:
        with zf.open("Metadata/slice_info.config") as sic:
            content = sic.read().decode()

    # Count `<object identify_id=` entries — one per arranged instance.
    object_count = content.count("<object identify_id=")
    assert object_count == 4, (
        f"expected 4 <object identify_id=...> entries in slice_info.config, "
        f"found {object_count}. Inspect /tmp/copies_test_out.3mf"
    )


def test_copies_too_many_fails_with_copies_dont_fit() -> None:
    """copies=100 of a Benchy cannot all fit on the A1 mini (180×180) bed.

    The A1 mini bed is 180×180 mm.  A Benchy is roughly 60×31 mm at the
    base, so a 6×5 grid (30 copies) already fills the bed.  100 copies
    is comfortably above that ceiling and should fail with
    ``copies_dont_fit``.
    """
    token = _upload_fixture()
    status, body = _slice(token, copies=100)
    assert status == 500, (
        f"expected 500 for overflow, got {status}: {body}. "
        f"100 Benchies should not fit on A1 mini's 180×180 mm bed."
    )
    assert body.get("code") == "copies_dont_fit", (
        f"expected code='copies_dont_fit', got: {body}"
    )
    msg = body.get("message", "")
    assert "copies" in msg.lower() or "place" in msg.lower(), (
        f"error message doesn't mention copies/place: {msg!r}"
    )
