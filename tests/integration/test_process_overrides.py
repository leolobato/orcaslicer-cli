"""End-to-end: client process_overrides overlay onto a real fixture slice.

Runs against the live container. Uses fixture 01 (single-filament benchy)
because its small and well-understood by the existing tests.

Uses only stdlib (urllib + json) so it runs in the container without any
extra dependencies (same pattern as test_slice_v2_fidelity.py).
"""
from __future__ import annotations

import io
import json
import os
import uuid
import urllib.request
import urllib.error
from pathlib import Path

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8070")

FIXTURE_INPUT = (
    Path(__file__).resolve().parents[2]
    / "_fixture" / "01"
    / "reference-benchy-orca-no-filament-custom-settings.3mf"
)


def _post_multipart_file(url: str, file_path: Path) -> dict:
    """POST a single file as multipart/form-data with field name 'file'."""
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
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30.0) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180.0) as r:
        return json.loads(r.read().decode())


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10.0) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30.0) as r:
        return r.read()


@pytest.mark.skipif(
    not FIXTURE_INPUT.exists(),
    reason=f"fixture missing: {FIXTURE_INPUT}",
)
def test_slice_v2_applies_process_override_layer_height() -> None:
    # 1. Upload the 3MF.
    up = _post_multipart_file(f"{API}/3mf", FIXTURE_INPUT)
    input_token = up["token"]

    # 2. Pick reasonable defaults from /profiles/* — match the test_slice_v2_fidelity
    # pattern: A1 mini, 0.20mm Standard process, default PLA filament.
    machines = _get_json(f"{API}/profiles/machines")
    a1m = next(m for m in machines["machines"]
               if "A1 mini" in m["name"] and m.get("nozzle_diameter") == "0.4")

    processes = _get_json(f"{API}/profiles/processes")
    proc = next(p for p in processes["processes"]
                if p["name"].startswith("0.20mm Standard")
                and a1m["setting_id"] in p.get("compatible_printers", []))

    filaments = _get_json(f"{API}/profiles/filaments?ams_assignable=true")
    fil = next(f for f in filaments["filaments"]
               if f["filament_type"] == "PLA"
               and a1m["setting_id"] in f.get("compatible_printers", []))

    # 3. Slice with a process_overrides that changes layer_height.
    payload = {
        "input_token": input_token,
        "machine_id": a1m["setting_id"],
        "process_id": proc["setting_id"],
        "filament_settings_ids": [fil["setting_id"]],
        "process_overrides": {"layer_height": "0.16"},
    }
    body = _post_json(f"{API}/slice/v2", payload)

    # 4. The settings_transfer carries the process_overrides_applied report.
    applied = body["settings_transfer"]["process_overrides_applied"]
    assert isinstance(applied, list)
    assert any(e["key"] == "layer_height" and e["value"] == "0.16"
               for e in applied), applied
    # `previous` should be the resolved system default (not "0.16").
    layer_entry = next(e for e in applied if e["key"] == "layer_height")
    assert layer_entry["previous"] != "0.16"

    # 5. Sanity: the slice succeeded (download URL works).
    out_token = body["output_token"]
    data = _get_bytes(f"{API}/3mf/{out_token}")
    assert len(data) > 1000


@pytest.mark.skipif(
    not FIXTURE_INPUT.exists(),
    reason=f"fixture missing: {FIXTURE_INPUT}",
)
def test_slice_v2_omitting_process_overrides_is_a_noop() -> None:
    """Existing callers that don't pass process_overrides still work."""
    up = _post_multipart_file(f"{API}/3mf", FIXTURE_INPUT)
    input_token = up["token"]

    machines = _get_json(f"{API}/profiles/machines")
    a1m = next(m for m in machines["machines"]
               if "A1 mini" in m["name"] and m.get("nozzle_diameter") == "0.4")

    processes = _get_json(f"{API}/profiles/processes")
    proc = next(p for p in processes["processes"]
                if p["name"].startswith("0.20mm Standard")
                and a1m["setting_id"] in p.get("compatible_printers", []))

    filaments = _get_json(f"{API}/profiles/filaments?ams_assignable=true")
    fil = next(f for f in filaments["filaments"]
               if f["filament_type"] == "PLA"
               and a1m["setting_id"] in f.get("compatible_printers", []))

    payload = {
        "input_token": input_token,
        "machine_id": a1m["setting_id"],
        "process_id": proc["setting_id"],
        "filament_settings_ids": [fil["setting_id"]],
        # No process_overrides at all.
    }
    body = _post_json(f"{API}/slice/v2", payload)

    # The new field should be present and empty.
    applied = body["settings_transfer"].get("process_overrides_applied", [])
    assert applied == []
