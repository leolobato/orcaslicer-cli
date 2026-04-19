"""Validate a faithful Python port of the OrcaSlicer GUI inheritance loader.

Models the core merge in PresetBundle::load_vendor_configs_from_json
(src/libslic3r/PresetBundle.cpp:3754-4284, OrcaSlicer v2.3.2):

  1. Read each profile JSON into a name-keyed map of raw configs.
  2. Process them in inheritance-topological order (parents before children).
  3. For each profile: start from the parent's already-flattened config (or
     empty), then apply the child via key-wise overwrite (semantically equivalent
     to DynamicPrintConfig::apply on the parsed JSON values we have here).
  4. Store the flattened result back into the map so children can inherit it.
  5. instantiation:"false" presets stay in the map as ancestors.

Skipped vs. the real GUI (irrelevant for this single-extruder filament chain):
  - `Preset::remove_invalid_keys`: drops keys not in the slicer's defaults.
  - `extend_default_config_length` / `Preset::normalize`: resize per-extruder
    vectors against `nozzle_diameter` / `filament_diameter` / `*_extruder_variant`.
  - Default-preset baseline for parent-less profiles (the GUI starts from
    `default_preset().config`, hundreds of keys).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent / "fixtures" / "profile_resolution"
LEAF_NAME = "eSUN PLA-Basic @BBL A1M"
MANUAL_REF = PROFILE_DIR / "manually-resolved-04.json"

CHAIN_FILES = [
    "eSUN PLA-Basic @BBL A1M Filament.json",
    "Bambu PLA Basic @BBL A1M.json",
    "Bambu PLA Basic @base.json",
    "fdm_filament_pla.json",
    "fdm_filament_common.json",
]


def load_raw_by_name(files: list[str]) -> dict[str, dict]:
    raw: dict[str, dict] = {}
    for fname in files:
        with open(PROFILE_DIR / fname) as f:
            data = json.load(f)
        # GUI keys the config_maps by the `name` field (BBL_JSON_KEY_NAME).
        # The leaf file omits `name` would be a bug; here all 5 files declare it.
        name = data["name"]
        if name in raw:
            raise RuntimeError(f"duplicate preset name: {name!r}")
        raw[name] = data
    return raw


def topo_sort_by_inherits(raw: dict[str, dict]) -> list[str]:
    """Return profile names ordered so parents come before children."""
    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise RuntimeError(f"inheritance cycle at {name!r}")
        visiting.add(name)
        parent = raw.get(name, {}).get("inherits")
        if parent and parent in raw:
            visit(parent)
        visiting.discard(name)
        visited.add(name)
        ordered.append(name)

    for name in raw:
        visit(name)
    return ordered


def resolve_gui_style(raw: dict[str, dict]) -> dict[str, dict]:
    """Mirror PresetBundle's per-vendor flattening pass."""
    flat: dict[str, dict] = {}
    for name in topo_sort_by_inherits(raw):
        config_src = raw[name]
        parent_name = config_src.get("inherits")
        if parent_name and parent_name in flat:
            base = dict(flat[parent_name])  # parent already flattened
        elif parent_name:
            # GUI would error / fall back to default_preset(); treat as missing.
            raise RuntimeError(
                f"missing parent for {name!r}: inherits={parent_name!r}"
            )
        else:
            base = {}
        # config.apply(config_src) — child overrides parent, key by key.
        base.update(config_src)
        flat[name] = base
    return flat


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
    raw = load_raw_by_name(CHAIN_FILES)
    flat = resolve_gui_style(raw)

    if LEAF_NAME not in flat:
        print(f"FAIL: leaf {LEAF_NAME!r} missing from resolved map")
        return 1

    resolved = flat[LEAF_NAME]
    # Manual reference omits `inherits`; strip it before comparing.
    actual = {k: v for k, v in resolved.items() if k != "inherits"}

    with open(MANUAL_REF) as f:
        expected = json.load(f)

    diffs = diff_dicts(expected, actual)
    if diffs:
        print(f"FAIL: {len(diffs)} difference(s) vs {MANUAL_REF.name}:")
        for line in diffs:
            print(line)
        return 1

    print(f"PASS: GUI-style resolved leaf matches {MANUAL_REF.name} ({len(actual)} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
