"""Tests for scripts/extract_tab_layout.py.

The extractor runs a regex pass over Tab.cpp's TabPrint::build() function
to harvest the page → optgroup → option ordering. We test it against
synthetic Tab.cpp snippets so the test doesn't depend on the vendored
source's exact contents.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.extract_tab_layout import extract_print_layout


def test_simple_one_page_one_optgroup() -> None:
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            auto page = add_options_page(L("Quality"), "empty");
                auto optgroup = page->new_optgroup(L("Layer height"));
                optgroup->append_single_option_line("layer_height","anchor1");
                optgroup->append_single_option_line("initial_layer_print_height","anchor2");
        }
        """).strip()
    layout = extract_print_layout(src)
    assert layout == [
        {
            "label": "Quality",
            "optgroups": [
                {
                    "label": "Layer height",
                    "options": ["layer_height", "initial_layer_print_height"],
                },
            ],
        },
    ]


def test_multiple_pages_and_optgroups() -> None:
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            page = add_options_page(L("Quality"), "empty");
                optgroup = page->new_optgroup(L("Layer height"));
                optgroup->append_single_option_line("layer_height","a");
                optgroup = page->new_optgroup(L("Line width"));
                optgroup->append_single_option_line("line_width","b");
            page = add_options_page(L("Strength"), "empty");
                optgroup = page->new_optgroup(L("Walls"));
                optgroup->append_single_option_line("wall_loops","c");
        }
        void TabPrint::other_method()
        {
            // Should NOT be picked up — outside TabPrint::build().
            auto page = add_options_page(L("Bogus"), "empty");
        }
        """).strip()
    layout = extract_print_layout(src)
    page_labels = [p["label"] for p in layout]
    assert page_labels == ["Quality", "Strength"], \
        "must scope to TabPrint::build, must not leak from other_method"
    quality = layout[0]
    assert [og["label"] for og in quality["optgroups"]] == \
        ["Layer height", "Line width"]


def test_skips_commented_lines() -> None:
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            auto page = add_options_page(L("Quality"), "empty");
                auto optgroup = page->new_optgroup(L("Layer height"));
                // optgroup->append_single_option_line("commented_out","x");
                optgroup->append_single_option_line("layer_height","y");
        }
        """).strip()
    layout = extract_print_layout(src)
    keys = layout[0]["optgroups"][0]["options"]
    assert keys == ["layer_height"]
    assert "commented_out" not in keys


def test_orphan_option_before_any_optgroup_is_dropped() -> None:
    """A safety net for malformed Tab.cpp shapes — we should not crash."""
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            optgroup->append_single_option_line("orphan","x");
            auto page = add_options_page(L("Quality"), "empty");
                auto optgroup = page->new_optgroup(L("Layer height"));
                optgroup->append_single_option_line("layer_height","y");
        }
        """).strip()
    layout = extract_print_layout(src)
    # The orphan must not appear anywhere.
    all_keys = [k for p in layout for og in p["optgroups"] for k in og["options"]]
    assert "orphan" not in all_keys
    assert all_keys == ["layer_height"]
