"""Tests for scripts.check_allowlist drift detection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_allowlist import check_drift


def _write_layout(path: Path, options_per_optgroup: list[list[str]]) -> None:
    pages = [{
        "label": "Quality",
        "optgroups": [
            {"label": f"og{i}", "options": opts}
            for i, opts in enumerate(options_per_optgroup)
        ],
    }]
    path.write_text(json.dumps({
        "extracted_from_path": "vendor/.../Tab.cpp",
        "extracted_from_sha": "0",
        "pages": pages,
    }))


def _write_allowlist(path: Path, options: list[str]) -> None:
    path.write_text(json.dumps({"revision": "test.1", "options": options}))


def _write_catalogue(path: Path, keys: list[str]) -> None:
    path.write_text(json.dumps({
        "options": [{"key": k, "label": k, "category": "Quality"} for k in keys],
    }))


def test_clean_state_returns_no_errors(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height", "wall_loops"]])
    _write_allowlist(allow, ["layer_height", "wall_loops"])
    _write_catalogue(cat, ["layer_height", "wall_loops"])
    errors = check_drift(layout, allow, cat)
    assert errors == []


def test_allowlist_key_missing_from_catalogue(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height"]])
    _write_allowlist(allow, ["layer_height", "typo_key"])
    _write_catalogue(cat, ["layer_height"])
    errors = check_drift(layout, allow, cat)
    assert any("typo_key" in e for e in errors)
    assert any("not in dump-options" in e for e in errors)


def test_allowlist_key_missing_from_layout(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height"]])
    _write_allowlist(allow, ["layer_height", "wall_loops"])
    _write_catalogue(cat, ["layer_height", "wall_loops"])
    errors = check_drift(layout, allow, cat)
    assert any("wall_loops" in e for e in errors)
    assert any("not surfaced in process_pages.json" in e for e in errors)


def test_layout_key_missing_from_catalogue(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height", "stale_key"]])
    _write_allowlist(allow, ["layer_height"])
    _write_catalogue(cat, ["layer_height"])
    errors = check_drift(layout, allow, cat)
    assert any("stale_key" in e for e in errors)
    assert any("Tab.cpp references" in e for e in errors)
