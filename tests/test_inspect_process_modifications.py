"""Test that parse_inspect_data exposes the project's modified process keys."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.inspect import parse_inspect_data


def _make_3mf_with_project_settings(settings: dict) -> bytes:
    """Build a minimal 3MF whose Metadata/project_settings.config matches."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(settings))
        # Minimal model relationships so the 3MF parser doesn't choke.
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("3D/3dmodel.model",
                    '<?xml version="1.0"?><model unit="millimeter"/>')
    return buf.getvalue()


def test_process_modifications_lists_modified_keys_and_values() -> None:
    settings = {
        "print_settings_id": "Custom 0.20mm Standard",
        "different_settings_to_system": [
            "layer_height;wall_loops",       # process slot
            "",                               # filament slot 0
            "",                               # printer slot
        ],
        "layer_height": "0.16",
        "wall_loops": "3",
        "sparse_infill_density": "20%",      # NOT in fingerprint, so not modified
    }
    data = parse_inspect_data(_make_3mf_with_project_settings(settings))

    assert "process_modifications" in data
    pm = data["process_modifications"]
    assert pm["process_setting_id"] == "Custom 0.20mm Standard"
    assert sorted(pm["modified_keys"]) == ["layer_height", "wall_loops"]
    assert pm["values"] == {"layer_height": "0.16", "wall_loops": "3"}


def test_process_modifications_empty_when_no_fingerprint() -> None:
    settings = {
        "print_settings_id": "0.20mm Standard @BBL P1S",
        # No different_settings_to_system at all.
        "layer_height": "0.20",
    }
    data = parse_inspect_data(_make_3mf_with_project_settings(settings))
    pm = data["process_modifications"]
    assert pm["modified_keys"] == []
    assert pm["values"] == {}
    # process_setting_id is still surfaced — it's a separate fact.
    assert pm["process_setting_id"] == "0.20mm Standard @BBL P1S"


def test_process_modifications_handles_missing_project_settings() -> None:
    """A 3MF without project_settings.config — process_modifications is empty."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("3D/3dmodel.model",
                    '<?xml version="1.0"?><model unit="millimeter"/>')
    data = parse_inspect_data(buf.getvalue())
    pm = data["process_modifications"]
    assert pm == {"process_setting_id": "", "modified_keys": [], "values": {}}
