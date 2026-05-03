"""Fidelity test: /slice/v2 vs GUI-authored output 3MF.

The fixtures in ``_fixture/01/`` are a reference 3MF that the GUI sliced
into ``gui-...gcode.3mf.3mf``. We slice the input through ``/slice/v2``
with the same machine/process/filament selection and assert the metadata
in ``slice_info.config`` (time, weight, layer count, start XY,
``printer_model_id``) falls within tolerance of the GUI's numbers.

Opt-in: requires a running container reachable at ``$ORCASLICER_API``
(default ``http://localhost:8000``) with ``USE_HEADLESS_BINARY=1``.
Skipped when not reachable.

Uses only stdlib (urllib + zipfile) so it runs on any host with Python
without needing a venv for ``httpx``.
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8000")
# tests/integration/test.py -> tests/integration -> tests -> orcaslicer-cli
# -> bambu_workspace, then `_fixture/`
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
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60.0) as r:
        return r.read()


def _read_slice_info(threemf_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        xml = z.read("Metadata/slice_info.config").decode()
    out: dict[str, str] = {}
    for m in re.finditer(r'<metadata key="(\w+)" value="([^"]*)"/>', xml):
        out[m.group(1)] = m.group(2)
    return out


def _first_xy(threemf_bytes: bytes) -> tuple[float, float]:
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        gcode = z.read("Metadata/plate_1.gcode").decode()
    parts = gcode.split("\nM624 ", 1)
    if len(parts) < 2:
        raise AssertionError("no M624 marker in gcode")
    for line in parts[1].splitlines():
        m = re.match(r"G1 X([\d.]+) Y([\d.]+)", line)
        if m:
            return float(m.group(1)), float(m.group(2))
    raise AssertionError("no G1 X/Y after first M624")


def _slice_and_compare(
    input_path: Path,
    gui_path: Path,
    *,
    machine_id: str,
    process_id: str,
    filament_settings_ids: list[str],
    time_tol: float = 0.02,
    weight_tol: float = 0.015,
    first_layer_time_tol: float = 0.01,
    xy_tol_mm: float = 0.01,
) -> None:
    """Slice ``input_path`` through ``/slice/v2`` and compare to GUI ground truth.

    Asserts the metadata stamped into ``Metadata/slice_info.config`` and the
    first toolpath XY land within tolerance of the GUI's sliced output.
    """
    assert input_path.exists(), f"missing fixture: {input_path}"
    assert gui_path.exists(), f"missing fixture: {gui_path}"

    upload = _post_multipart_file(f"{API}/3mf", input_path)
    token = upload["token"]

    slice_resp = _post_json(
        f"{API}/slice/v2",
        {
            "input_token": token,
            "machine_id": machine_id,
            "process_id": process_id,
            "filament_settings_ids": filament_settings_ids,
            "recenter": False,
        },
    )
    out_token = slice_resp["output_token"]
    ours = _get_bytes(f"{API}/3mf/{out_token}")
    gui = gui_path.read_bytes()
    ours_info = _read_slice_info(ours)
    gui_info = _read_slice_info(gui)

    ours_time = float(ours_info["prediction"])
    gui_time = float(gui_info["prediction"])
    assert abs(ours_time - gui_time) / gui_time < time_tol, (
        f"time drift {ours_time} vs {gui_time}"
    )

    ours_w = float(ours_info["weight"])
    gui_w = float(gui_info["weight"])
    assert abs(ours_w - gui_w) / gui_w < weight_tol, (
        f"weight drift {ours_w} vs {gui_w}"
    )

    assert ours_info.get("printer_model_id") == gui_info.get("printer_model_id")
    assert ours_info.get("label_object_enabled") == gui_info.get(
        "label_object_enabled"
    )
    assert ours_info.get("nozzle_diameters") == gui_info.get("nozzle_diameters")

    ours_flt = float(ours_info["first_layer_time"])
    gui_flt = float(gui_info["first_layer_time"])
    assert ours_flt > 0.0, "first_layer_time should be populated"
    assert abs(ours_flt - gui_flt) / gui_flt < first_layer_time_tol, (
        f"first_layer_time drift {ours_flt} vs {gui_flt}"
    )

    ours_xy = _first_xy(ours)
    gui_xy = _first_xy(gui)
    assert abs(ours_xy[0] - gui_xy[0]) < xy_tol_mm, (
        f"start X {ours_xy[0]} vs {gui_xy[0]}"
    )
    assert abs(ours_xy[1] - gui_xy[1]) < xy_tol_mm, (
        f"start Y {ours_xy[1]} vs {gui_xy[1]}"
    )


def test_fixture_01_matches_gui_within_tolerance() -> None:
    """Single-filament A1 mini benchy with process+printer customizations."""
    _slice_and_compare(
        FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf",
        FIXTURE_DIR / "01" / "gui-benchy-orca-no-filament-custom-settings_sliced_gui.gcode.3mf.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=["GFSA00_02"],
    )


def test_fixture_03_matches_gui_within_tolerance() -> None:
    """Single-filament with FILAMENT-side customizations (filament_max_volumetric_speed,
    filament_flow_ratio in `different_settings_to_system[1]`). Exercises the
    `applied` branch of the per-filament name guard."""
    _slice_and_compare(
        FIXTURE_DIR / "03" / "reference-benchy-with-filament-customizations.3mf",
        FIXTURE_DIR / "03" / "gui-reference-benchy-with-filament-customizations_sliced.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=["GFSA00_02"],
    )


def test_fixture_05_matches_gui_within_tolerance() -> None:
    """Same as fixture 01 but with curr_bed_type = Cool Plate (vs Textured PEI).
    Verifies bed-type carry-through and bed-temperature lookups."""
    _slice_and_compare(
        FIXTURE_DIR / "05" / "reference-benchy-cool-plate.3mf",
        FIXTURE_DIR / "05" / "gui-reference-benchy-cool-plate_sliced.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=["GFSA00_02"],
    )


@pytest.mark.xfail(
    reason="Multi-filament composition now uses PresetBundle::construct_full_config "
    "(GUI-authoritative path) but Print::process still rejects the resulting config "
    "with `Flow::spacing produced negative spacing` during Slicing mesh — some "
    "extrusion width is computing to <= 0. Likely an extruder-variant reshaping "
    "mismatch we still need to chase in `cpp/src/slice_mode.cpp` setup."
)
def test_fixture_04_matches_gui_within_tolerance() -> None:
    """5 filament slots spanning 3 vendors (Bambu, SUNLU, Overture). Geometry
    is bound to a single slot via `extruder` metadata, but the project
    declares all 5 — exercises multi-slot materialisation, cross-vendor
    profile resolution, and 5×5 flush-volume matrix sizing."""
    _slice_and_compare(
        FIXTURE_DIR / "04" / "reference-bird-orca.3mf",
        FIXTURE_DIR / "04" / "gui-bird-orca_sliced_gui.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=[
            "GFSA00_02",   # Bambu PLA Basic @BBL A1M
            "GFSNLS03_07", # SUNLU PLA+ @BBL A1M
            "GFSL05_05",   # Overture Matte PLA @BBL A1M
            "GFSA00_02",   # Bambu PLA Basic @BBL A1M (slot 3)
            "GFSA00_02",   # Bambu PLA Basic @BBL A1M (slot 4)
        ],
    )
