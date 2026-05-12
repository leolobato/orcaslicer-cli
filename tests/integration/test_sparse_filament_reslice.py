"""Regression test for re-slicing a sliced 3MF with a sparse AMS slot.

Fixture 09 (`reference-gravity-broom-holder.3mf`) is a P1P-authored
sliced 3MF with exactly one filament authored on AMS slot 1 (slot 0
unused). This case used to fail end-to-end with the gateway surfacing
``Slicing failed: Expecting value: line 1 column 1 (char 0)``. Two
distinct bugs chained:

1. **Sparse-slot padding.** The wrapper passed
   ``filament_settings_ids`` through positionally, so libslic3r's
   ``DynamicPrintConfig::update_values_to_printer_extruders_for_multiple_filaments``
   walked ``filament_count = filament_maps.size()`` and tripped on the
   filament-index lookup when authored slot was 1 but the array length
   was 1. Fixed by ``app.slicer.pad_filament_settings_for_sparse_3mf``,
   which pads to ``max(authored_slot)+1``.
2. **libslic3r stdout pollution.** Vendored
   ``Support/TreeSupportCommon.hpp:597`` calls raw ``printf`` for tree-
   support warnings (commented "todo Remove! ONLY FOR PUBLIC BETA"),
   which prepends "Error: Not precalculated Placeable areas requested,
   radius 0, layer 0, critical: 0" lines to stdout and corrupts the JSON
   protocol. Fixed by ``redirect_libslic3r_stdout_pollution`` in the
   binary, which dup2's stderr over fd 1 and writes the JSON envelope to
   the saved real-stdout fd.

This test exercises both fixes together: a slice request with a
positional length-1 ``filament_settings_ids`` against a sliced 3MF
authoring slot 1, and asserts the ``/slice/v2`` response is a parseable
success envelope (proving stdout wasn't corrupted) with a non-empty
``output_token``.

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
FIXTURE_DIR = Path(__file__).resolve().parents[2].parent / "_fixture"


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
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300.0) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60.0) as r:
        return r.read()


def test_fixture_09_sliced_3mf_with_sparse_ams_slot_1() -> None:
    """Re-slicing a P1P-authored sliced 3MF with one filament on AMS
    slot 1 (slot 0 unused) must complete cleanly through ``/slice/v2``.

    Caller sends positional ``filament_settings_ids = ["GFSA04_10"]`` of
    length 1 — the wrapper's padding promotes that to length 2 internally
    so libslic3r's filament-index lookup doesn't trip. The response
    envelope must parse as JSON (proving the libslic3r-stdout-pollution
    redirect kept the protocol clean) and produce a downloadable sliced
    3MF containing valid gcode.
    """
    input_path = FIXTURE_DIR / "09" / "reference-gravity-broom-holder.3mf"
    assert input_path.exists(), f"missing fixture: {input_path}"

    upload = _post_multipart_file(f"{API}/3mf", input_path)
    token = upload["token"]

    slice_resp = _post_json(f"{API}/slice/v2", {
        "input_token": token,
        "machine_id": "GM013",          # Bambu Lab P1P 0.4 nozzle
        "process_id": "GP015",          # 0.20mm Standard @BBL P1P
        "filament_settings_ids": ["GFSA04_10"],  # length 1 — sparse-slot padding kicks in
        "plate_id": 1,
    })

    assert "output_token" in slice_resp, (
        f"slice failed (response did not contain output_token): {slice_resp!r}"
    )

    # Estimate sanity: a multi-hour print of ~30g of PLA. Tolerances are
    # loose because the slicer's strategy can drift; the regression we're
    # locking in is "did not crash + produced a result", not fidelity.
    estimate = slice_resp.get("estimate", {})
    assert estimate.get("time_seconds", 0) > 60, (
        f"unexpectedly short slice time: {estimate}"
    )
    assert estimate.get("weight_g", 0) > 1.0, (
        f"unexpectedly low weight: {estimate}"
    )

    # The sliced 3MF must be a real, parseable archive with gcode in it.
    out_token = slice_resp["output_token"]
    sliced_bytes = _get_bytes(f"{API}/3mf/{out_token}")
    assert len(sliced_bytes) > 100_000, (
        f"sliced 3MF unexpectedly small: {len(sliced_bytes)} bytes"
    )
    with zipfile.ZipFile(io.BytesIO(sliced_bytes)) as zf:
        names = zf.namelist()
    assert any("gcode" in n.lower() for n in names), (
        f"sliced 3MF has no gcode entries: {names!r}"
    )
    assert "Metadata/slice_info.config" in names, (
        f"sliced 3MF missing slice_info.config: {names!r}"
    )
