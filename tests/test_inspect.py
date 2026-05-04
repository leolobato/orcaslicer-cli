"""Unit tests for app.inspect.parse_inspect_data."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.inspect import INSPECT_SCHEMA_VERSION, InspectCache, parse_inspect_data

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

    # New top-level fields from project_settings.
    assert result["printer_settings_id"] == "Bambu Lab A1 mini 0.4 nozzle"
    assert result["print_settings_id"] == "0.20mm Standard @BBL A1M"
    assert result["layer_height"] == "0.25"

    # Per-plate shape for un-sliced 3MFs.
    assert len(result["plates"]) >= 1
    p0 = result["plates"][0]
    assert p0["name"] == ""
    assert p0["estimate"] is None
    assert p0["support_used"] is None
    assert p0["label_object_enabled"] is None
    assert p0["nozzle_diameters"] is None
    assert p0["filament_maps"] is None
    assert p0["limit_filament_maps"] is None
    assert p0["outside"] is None
    assert p0["warnings"] == []
    # Per-plate objects from model_settings.config
    assert p0["objects"] == [{"id": "156", "name": "3DBenchy.stl"}]


def test_parse_sliced_output(fixture_01_sliced_bytes):
    result = parse_inspect_data(fixture_01_sliced_bytes)

    assert result["is_sliced"] is True
    assert result["estimate"] is not None
    e = result["estimate"]
    assert e["time_seconds"] > 0
    assert e["weight_g"] > 0
    # `slice_info.config` lists the filament slots that the plate actually uses.
    assert result["plates"][0]["used_filament_indices"] == [0]

    # Per-plate sliced metadata.
    p0 = result["plates"][0]
    assert p0["estimate"] is not None
    assert p0["estimate"]["time_seconds"] > 0
    assert p0["estimate"]["weight_g"] > 0
    assert p0["estimate"]["first_layer_time"] > 0
    assert p0["support_used"] is False
    assert p0["label_object_enabled"] is True
    assert p0["nozzle_diameters"] == "0.4"
    assert len(p0["warnings"]) >= 1
    # Per-plate objects from slice_info.config <object> children
    assert p0["objects"] == [{"id": "156", "name": "3DBenchy.stl"}]


def test_inspect_cache_hit_and_miss() -> None:
    c = InspectCache()
    assert c.get("sha-a") is None
    c.put("sha-a", {"is_sliced": False})
    assert c.get("sha-a") == {"is_sliced": False}
    assert c.get("sha-b") is None


def test_inspect_cache_invalidate() -> None:
    c = InspectCache()
    c.put("sha-a", {"is_sliced": False})
    c.invalidate("sha-a")
    assert c.get("sha-a") is None


def test_inspect_cache_evicts_oldest_when_full() -> None:
    c = InspectCache()
    # Fill past MAX_ENTRIES.
    for i in range(InspectCache.MAX_ENTRIES + 5):
        c.put(f"sha-{i}", {"i": i})
    # The first 5 should have been evicted.
    for i in range(5):
        assert c.get(f"sha-{i}") is None
    for i in range(5, InspectCache.MAX_ENTRIES + 5):
        assert c.get(f"sha-{i}") == {"i": i}


def test_inspect_cache_schema_version_invalidates_logically() -> None:
    """Bumping INSPECT_SCHEMA_VERSION must drop cached entries.

    We simulate the bump by stuffing an entry with a stale version
    directly into the internal dict, then verifying ``get`` misses.
    """
    c = InspectCache()
    c._entries[("sha-x", INSPECT_SCHEMA_VERSION - 1)] = {"stale": True}
    assert c.get("sha-x") is None
