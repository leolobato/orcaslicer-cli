"""Unit tests for app.threemf.list_plate_thumbnails."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.threemf import list_plate_thumbnails, read_plate_thumbnail

FIXTURE_ROOT = Path(__file__).resolve().parents[1].parent / "_fixture"


@pytest.fixture
def fixture_01_input_bytes() -> bytes:
    p = FIXTURE_ROOT / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


def test_list_plate_thumbnails(fixture_01_input_bytes):
    thumbs = list_plate_thumbnails(fixture_01_input_bytes)
    # Reference benchy has at least the main plate_1.png.
    main = [t for t in thumbs if t["plate"] == 1 and t["kind"] == "main"]
    assert len(main) == 1
    assert main[0]["name"] == "Metadata/plate_1.png"


def test_read_plate_thumbnail_bytes(fixture_01_input_bytes):
    png = read_plate_thumbnail(fixture_01_input_bytes, plate=1, kind="main")
    assert png is not None
    # PNG signature.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_read_plate_thumbnail_missing(fixture_01_input_bytes):
    assert read_plate_thumbnail(fixture_01_input_bytes, plate=99, kind="main") is None
