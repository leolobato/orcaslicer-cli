"""Unit tests for app.options — loader, allowlist filter, and cache."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import options


@pytest.fixture
def fake_layout(tmp_path: Path) -> Path:
    p = tmp_path / "process_pages.json"
    p.write_text(json.dumps({
        "extracted_from_path": "vendor/.../Tab.cpp",
        "extracted_from_sha": "deadbeef",
        "pages": [
            {
                "label": "Quality",
                "optgroups": [
                    {"label": "Layer height",
                     "options": ["layer_height", "initial_layer_print_height"]},
                    {"label": "Seam",
                     "options": ["seam_position"]},
                ],
            },
            {
                "label": "Strength",
                "optgroups": [
                    {"label": "Walls",
                     "options": ["wall_loops"]},
                ],
            },
        ],
    }))
    return p


@pytest.fixture
def fake_allowlist(tmp_path: Path) -> Path:
    p = tmp_path / "process_allowlist.json"
    p.write_text(json.dumps({
        "revision": "test.1",
        "options": ["layer_height", "wall_loops"],
    }))
    return p


@pytest.fixture
def fake_catalogue() -> dict:
    return {
        "options": [
            {"key": "layer_height", "label": "Layer height", "category": "Quality",
             "type": "coFloat", "min": 0.0, "max": 0.6, "default": "0.2",
             "tooltip": "", "sidetext": "mm", "enum_values": None,
             "enum_labels": None, "mode": "simple", "gui_type": "",
             "nullable": False, "readonly": False},
            {"key": "wall_loops", "label": "Wall loops", "category": "Strength",
             "type": "coInt", "min": 0, "max": 1000, "default": "2",
             "tooltip": "", "sidetext": "", "enum_values": None,
             "enum_labels": None, "mode": "simple", "gui_type": "",
             "nullable": False, "readonly": False},
            {"key": "seam_position", "label": "Seam position", "category": "Quality",
             "type": "coEnum", "min": None, "max": None, "default": "aligned",
             "tooltip": "", "sidetext": "",
             "enum_values": ["nearest", "aligned", "back", "random"],
             "enum_labels": ["Nearest", "Aligned", "Back", "Random"],
             "mode": "simple", "gui_type": "", "nullable": False, "readonly": False},
        ],
    }


def test_filter_layout_drops_non_allowlisted_options(
    fake_layout: Path, fake_allowlist: Path,
) -> None:
    layout_doc = json.loads(fake_layout.read_text())
    allowlist = json.loads(fake_allowlist.read_text())
    filtered = options.filter_layout(layout_doc["pages"], set(allowlist["options"]))

    # Only Quality > Layer height (with layer_height kept) and
    # Strength > Walls (with wall_loops) survive.
    assert [p["label"] for p in filtered] == ["Quality", "Strength"]
    quality = filtered[0]
    assert [og["label"] for og in quality["optgroups"]] == ["Layer height"]
    assert quality["optgroups"][0]["options"] == ["layer_height"]
    assert filtered[1]["optgroups"][0]["options"] == ["wall_loops"]


def test_filter_layout_drops_empty_optgroups_and_pages(
    fake_layout: Path,
) -> None:
    layout_doc = json.loads(fake_layout.read_text())
    # Allowlist that knocks out everything in Strength.
    filtered = options.filter_layout(layout_doc["pages"], {"layer_height"})
    assert [p["label"] for p in filtered] == ["Quality"]
    assert [og["label"] for og in filtered[0]["optgroups"]] == ["Layer height"]


def test_filter_layout_preserves_option_order_within_optgroup(
    fake_layout: Path,
) -> None:
    layout_doc = json.loads(fake_layout.read_text())
    filtered = options.filter_layout(
        layout_doc["pages"],
        {"layer_height", "initial_layer_print_height"},
    )
    assert filtered[0]["optgroups"][0]["options"] == \
        ["layer_height", "initial_layer_print_height"]


async def test_load_into_cache_populates_metadata_and_layout(
    monkeypatch, fake_layout: Path, fake_allowlist: Path, fake_catalogue: dict,
) -> None:
    monkeypatch.setattr(options, "_LAYOUT_PATH", fake_layout)
    monkeypatch.setattr(options, "_ALLOWLIST_PATH", fake_allowlist)

    fake_client = AsyncMock()
    fake_client.dump_options = AsyncMock(return_value=fake_catalogue)
    cache = await options.load_options_cache(binary_client=fake_client)

    # /options/process payload — unfiltered; all three keys present.
    assert set(cache.metadata["options"]) == \
        {"layer_height", "wall_loops", "seam_position"}

    # /options/process/layout payload — filtered to allowlist.
    layout = cache.layout
    assert layout["allowlist_revision"] == "test.1"
    page_labels = [p["label"] for p in layout["pages"]]
    assert page_labels == ["Quality", "Strength"]
    layer_optgroup = layout["pages"][0]["optgroups"][0]
    assert layer_optgroup["options"] == ["layer_height"]
