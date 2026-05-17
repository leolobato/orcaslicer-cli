from pathlib import Path


def test_stl_auto_orient_uses_gui_default_min_area_mode():
    """Headless STL auto-orient should match Orca GUI's default option state."""
    source = Path("cpp/src/stl_draft_mode.cpp").read_text()

    auto_orient_body = source.split("void auto_orient_all", 1)[1].split(
        "bool arrange_draft_instances", 1
    )[0]

    assert "OrientParamsArea" in auto_orient_body
    assert "params.min_volume = false" in auto_orient_body
    assert "params.min_volume = true" not in auto_orient_body
