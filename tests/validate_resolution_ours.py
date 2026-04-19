"""Validate app/profiles.py resolves the eSUN chain to manually-resolved-04.json.

Builds a fake vendor dir from the test files, points app.profiles at it,
runs the real loader, and diffs the resolved leaf against the manual reference.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

PROFILE_DIR = Path(__file__).resolve().parent / "fixtures" / "profile_resolution"
LEAF_NAME = "eSUN PLA-Basic @BBL A1M"
LEAF_FILE = "eSUN PLA-Basic @BBL A1M Filament.json"
MANUAL_REF = PROFILE_DIR / "manually-resolved-04.json"

CHAIN_FILES = [
    "eSUN PLA-Basic @BBL A1M Filament.json",
    "Bambu PLA Basic @BBL A1M.json",
    "Bambu PLA Basic @base.json",
    "fdm_filament_pla.json",
    "fdm_filament_common.json",
]


def build_fake_vendor(dst_root: Path) -> None:
    """Lay out the 5 chain files as a single-vendor profiles dir."""
    vendor = "TestVendor"
    filament_dir = dst_root / vendor / "filament"
    filament_dir.mkdir(parents=True)
    for fname in CHAIN_FILES:
        shutil.copy(PROFILE_DIR / fname, filament_dir / fname)

    index = {
        "name": vendor,
        "version": "01.00.00.00",
        "machine_model_list": [],
        "machine_list": [],
        "process_list": [],
        "filament_list": [
            {"name": LEAF_NAME, "sub_path": f"filament/{LEAF_FILE}"},
        ],
    }
    (dst_root / f"{vendor}.json").write_text(json.dumps(index, indent=2))


def diff_dicts(expected: dict, actual: dict) -> list[str]:
    diffs: list[str] = []
    e_keys = set(expected.keys())
    a_keys = set(actual.keys())
    for key in sorted(a_keys - e_keys):
        diffs.append(f"  + extra key: {key!r} = {actual[key]!r}")
    for key in sorted(e_keys - a_keys):
        diffs.append(f"  - missing key: {key!r} (expected {expected[key]!r})")
    for key in sorted(e_keys & a_keys):
        if expected[key] != actual[key]:
            diffs.append(
                f"  ~ {key!r}\n      expected: {expected[key]!r}\n      actual:   {actual[key]!r}"
            )
    return diffs


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        build_fake_vendor(tmp_path)
        user_dir = tmp_path / "user"
        user_dir.mkdir()

        os.environ["PROFILES_DIR"] = str(tmp_path)
        os.environ["USER_PROFILES_DIR"] = str(user_dir)

        from app import profiles

        profiles.PROFILES_DIR = str(tmp_path)
        profiles.USER_PROFILES_DIR = str(user_dir)

        counts = profiles.load_all_profiles()
        print(f"Loaded profiles: {counts}")

        resolved = profiles.resolve_profile_by_name(LEAF_NAME)
        if resolved is None:
            print("FAIL: resolve_profile_by_name returned None")
            return 1

        # The manual reference omits `inherits`; strip it before comparing.
        actual = {k: v for k, v in resolved.items() if k != "inherits"}

        with open(MANUAL_REF) as f:
            expected = json.load(f)

        diffs = diff_dicts(expected, actual)
        if diffs:
            print(f"\nFAIL: {len(diffs)} difference(s) vs {MANUAL_REF.name}:")
            for line in diffs:
                print(line)
            return 1

        print(f"\nPASS: resolved leaf matches {MANUAL_REF.name} ({len(actual)} keys)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
