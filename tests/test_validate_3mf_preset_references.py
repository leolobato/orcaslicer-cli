"""Unit tests for slicer.validate_3mf_preset_references (Gap 5)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.profiles import ProfileNotFoundError
from app.slicer import validate_3mf_preset_references

FIXTURE_ROOT = Path(__file__).resolve().parents[1].parent / "_fixture"


@pytest.fixture
def fixture_01_input_bytes() -> bytes:
    p = FIXTURE_ROOT / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


def test_validate_returns_empty_when_all_resolve(fixture_01_input_bytes):
    """Every preset name in the 3MF resolves → no findings."""

    def fake_get(category: str, slug: str):
        return {"name": slug}

    with patch("app.slicer.get_profile_by_id_or_name", side_effect=fake_get):
        findings = validate_3mf_preset_references(fixture_01_input_bytes)

    assert findings == []


def test_validate_reports_missing_filament(fixture_01_input_bytes):
    """A renamed/removed filament name surfaces as a finding."""

    def fake_get(category: str, slug: str):
        if category == "filament":
            raise ProfileNotFoundError(f"missing: {slug}")
        return {"name": slug}

    with patch("app.slicer.get_profile_by_id_or_name", side_effect=fake_get):
        findings = validate_3mf_preset_references(fixture_01_input_bytes)

    # Fixture 01 has one filament: "Bambu PLA Basic @BBL A1M".
    assert findings == [
        {"category": "filament", "name": "Bambu PLA Basic @BBL A1M"},
    ]


def test_validate_reports_missing_machine_and_process(fixture_01_input_bytes):
    """Multiple missing categories all show up in the finding list."""

    def fake_get(category: str, slug: str):
        if category in ("machine", "process"):
            raise ProfileNotFoundError(f"missing: {slug}")
        return {"name": slug}

    with patch("app.slicer.get_profile_by_id_or_name", side_effect=fake_get):
        findings = validate_3mf_preset_references(fixture_01_input_bytes)

    cats = {f["category"] for f in findings}
    assert cats == {"machine", "process"}


def test_validate_returns_empty_for_malformed_3mf():
    """Non-zip / unparseable input must not raise."""
    findings = validate_3mf_preset_references(b"not a 3mf at all")
    assert findings == []
