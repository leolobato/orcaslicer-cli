"""Unit tests for app.inspect.parse_inspect_data."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.inspect import parse_inspect_data

FIXTURE_ROOT = Path(__file__).resolve().parents[1].parent / "_fixture"


@pytest.fixture
def fixture_01_input_bytes() -> bytes:
    p = FIXTURE_ROOT / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


@pytest.fixture
def fixture_01_sliced_bytes() -> bytes:
    p = (
        FIXTURE_ROOT
        / "01"
        / "gui-benchy-orca-no-filament-custom-settings_sliced_gui.gcode.3mf.3mf"
    )
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


def test_parse_unsliced_input(fixture_01_input_bytes):
    result = parse_inspect_data(fixture_01_input_bytes)

    assert result["is_sliced"] is False
    assert result["plate_count"] >= 1
    # Reference benchy is single-filament PLA.
    assert len(result["filaments"]) == 1
    f = result["filaments"][0]
    assert f["slot"] == 0
    assert f["type"] == "PLA"
    assert f["color"].startswith("#")
    assert f["filament_id"] == "GFA00"
    assert f["settings_id"] == "Bambu PLA Basic @BBL A1M"
    # Un-sliced has no estimate.
    assert result["estimate"] is None
    # Bounding box and printer hints come from the project_settings.
    assert result["printer_model"] == "Bambu Lab A1 mini"
    assert result["printer_variant"] == "0.4"
    assert result["curr_bed_type"] == "Textured PEI Plate"


def test_parse_sliced_output(fixture_01_sliced_bytes):
    result = parse_inspect_data(fixture_01_sliced_bytes)

    assert result["is_sliced"] is True
    assert result["estimate"] is not None
    e = result["estimate"]
    assert e["time_seconds"] > 0
    assert e["weight_g"] > 0
    # `slice_info.config` lists the filament slots that the plate actually uses.
    assert result["plates"][0]["used_filament_indices"] == [0]
