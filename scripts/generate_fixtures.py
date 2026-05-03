#!/usr/bin/env python3
"""Generate input 3MFs for the /slice/v2 fidelity suite.

The fidelity suite pins our wrapper's output against what the OrcaSlicer
**GUI** produces for the same input — so the GUI must be the source of
truth. This script only does the tedious bit: mutating the embedded
``Metadata/project_settings.config`` of an existing reference 3MF in-zip
to produce variant inputs without re-authoring the project from scratch.

Slicing is **always** done by hand in OrcaSlicer.app: the script prints
GUI steps for every variant after writing the mutated input.

Variants the script can mutate automatically:

- ``03/`` — single-filament with FILAMENT-side customizations in
  ``different_settings_to_system[1]``. Exercises the ``applied`` path of
  the per-filament name guard, which fixture 01 only hits as
  ``no_customizations``.
- ``05/`` — same as fixture 01 but with ``curr_bed_type = "Cool Plate"``
  instead of Textured PEI. Exercises bed-type carry-through.

Variants that need manual authoring from scratch (no script support):

- ``04/`` — multi-filament. Needs painted-mesh data or a multi-part
  model that this script can't safely synthesise. (Already authored
  in-tree as a 5-slot multi-vendor PLA+PETG bird.)
- ``06/`` — different printer family (X1C). Needs printer-profile swap
  and possibly a different process.

Usage::

    python3 scripts/generate_fixtures.py            # write all auto inputs
    python3 scripts/generate_fixtures.py --dry-run  # show what would be written
    python3 scripts/generate_fixtures.py 03         # write only variant 03
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT.parent / "_fixture"
SOURCE_INPUT = (
    FIXTURE_ROOT
    / "01"
    / "reference-benchy-orca-no-filament-custom-settings.3mf"
)


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def _mutate_03(settings: dict) -> dict:
    """Add filament-side customizations.

    The fingerprint layout is ``[process, filament_0, ..., filament_{N-1}, printer]``.
    Fixture 01 has slot 1 (filament_0) empty. Populate it with two
    filament keys and tweak their values so they actually deviate from
    the system filament profile defaults — the parity test will only
    flag a regression if those values flow through to the output gcode.
    """
    fp = list(settings.get("different_settings_to_system", []))
    while len(fp) < 3:
        fp.insert(-1 if fp else 0, "")
    fp[1] = "filament_max_volumetric_speed;filament_flow_ratio"
    settings["different_settings_to_system"] = fp
    # System default for Bambu PLA Basic A1M: max_vol_speed=21, flow_ratio=0.98.
    settings["filament_max_volumetric_speed"] = ["18"]
    settings["filament_flow_ratio"] = ["0.95"]
    return settings


def _mutate_05(settings: dict) -> dict:
    """Flip curr_bed_type to Cool Plate."""
    settings["curr_bed_type"] = "Cool Plate"
    return settings


VARIANTS = {
    "03": {
        "label": "single-filament with filament customizations",
        "input_name": "reference-benchy-with-filament-customizations.3mf",
        "expected_gui_output": "gui-reference-benchy-with-filament-customizations_sliced.3mf",
        "mutate": _mutate_03,
    },
    "05": {
        "label": "Cool Plate bed type",
        "input_name": "reference-benchy-cool-plate.3mf",
        "expected_gui_output": "gui-reference-benchy-cool-plate_sliced.3mf",
        "mutate": _mutate_05,
    },
}

MANUAL_VARIANTS = [
    (
        "04",
        "multi-filament (5 slots, mixed vendors / materials)",
        [
            "Author the project from scratch in OrcaSlicer with multiple filament slots and vendors of your choice.",
            "Save the project as `_fixture/04/reference-<name>.3mf` (File → Export → Project).",
            "Slice in the GUI and save the sliced output as `_fixture/04/gui-<name>_sliced.3mf` (File → Export → Sliced 3MF).",
        ],
    ),
    (
        "06",
        "X1C printer (different printer family)",
        [
            "Open `_fixture/01/reference-...3mf` in OrcaSlicer.",
            "Switch printer to `Bambu Lab X1 Carbon 0.4 nozzle` and an X1C-compatible process (e.g. `0.20mm Standard @BBL X1C`).",
            "Save as `_fixture/06/reference-benchy-x1c.3mf`.",
            "Slice and save the sliced output as `_fixture/06/gui-benchy-x1c_sliced.3mf`.",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def mutate_3mf(src: Path, dst: Path, mutate_fn) -> None:
    """Copy ``src`` to ``dst`` with ``Metadata/project_settings.config`` mutated.

    Other entries (geometry, model_settings, thumbnails, ...) pass through
    byte-for-byte.
    """
    with zipfile.ZipFile(src, "r") as zin:
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in zin.namelist():
                data = zin.read(name)
                if name == "Metadata/project_settings.config":
                    settings = json.loads(data.decode())
                    settings = mutate_fn(settings)
                    data = json.dumps(settings, indent=1).encode()
                zout.writestr(name, data)


def _print_gui_steps(variant_id: str, input_path: Path, output_name: str) -> None:
    print(f"  GUI steps to produce the sliced ground-truth for variant {variant_id}:")
    print(f"    1. Open {input_path.relative_to(FIXTURE_ROOT.parent)} in OrcaSlicer.app.")
    print(f"    2. Click Slice (do not change settings — keep what's embedded).")
    print(f"    3. File → Export → Export Sliced File…")
    print(f"       Save as: _fixture/{variant_id}/{output_name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def generate(variant_id: str, dry_run: bool) -> None:
    spec = VARIANTS[variant_id]
    target_dir = FIXTURE_ROOT / variant_id
    input_path = target_dir / spec["input_name"]

    print(f"\n=== variant {variant_id}: {spec['label']} ===")
    print(f"input → {input_path.relative_to(FIXTURE_ROOT.parent)}")

    if dry_run:
        print("(dry-run; no files written)")
        _print_gui_steps(variant_id, input_path, spec["expected_gui_output"])
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    mutate_3mf(SOURCE_INPUT, input_path, spec["mutate"])
    print(f"  ✓ wrote input ({input_path.stat().st_size:,} bytes)")
    _print_gui_steps(variant_id, input_path, spec["expected_gui_output"])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "variants",
        nargs="*",
        help="Specific variant ids to generate (default: all). Try '03' or '05'.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing files.",
    )
    args = p.parse_args()

    if not SOURCE_INPUT.exists():
        print(f"ERROR: source fixture missing: {SOURCE_INPUT}", file=sys.stderr)
        return 1

    targets = args.variants or list(VARIANTS.keys())
    unknown = [v for v in targets if v not in VARIANTS]
    if unknown:
        print(
            f"ERROR: unknown variant(s): {unknown}. "
            f"Valid: {list(VARIANTS.keys())}",
            file=sys.stderr,
        )
        return 1

    for variant_id in targets:
        try:
            generate(variant_id, dry_run=args.dry_run)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}", file=sys.stderr)
            return 2

    print("\n=== Variants requiring manual GUI authoring from scratch ===")
    for variant_id, label, steps in MANUAL_VARIANTS:
        print(f"\n--- variant {variant_id}: {label} ---")
        for i, step in enumerate(steps, 1):
            print(f"  {i}. {step}")

    print(
        "\nDone. After slicing in the GUI, extend "
        "tests/integration/test_slice_v2_fidelity.py to pin parity for the "
        "new fixtures."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
