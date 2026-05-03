#!/usr/bin/env python3
"""Generate test fixtures for the /slice/v2 fidelity suite.

Run this on a Mac that has OrcaSlicer.app installed in /Applications. It
produces ``_fixture/<NN>/reference-...3mf`` (input) and
``_fixture/<NN>/gui-...3mf`` (GUI-equivalent sliced output) pairs that
``tests/integration/test_slice_v2_fidelity.py`` (and any future variants)
can pin parity against.

Strategy:

1. Start from ``_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf``
   — a single-filament A1 mini benchy with process+printer customizations.
2. For each desired variant, mutate the embedded ``project_settings.config``
   in-zip (no geometry changes — variants that need painted geometry are
   listed in ``MANUAL_VARIANTS`` for the user to author by hand in the GUI).
3. Slice each mutated input through ``OrcaSlicer.app/Contents/MacOS/OrcaSlicer``
   in CLI mode to produce the reference output. The CLI uses the same
   libslic3r the GUI does, so its output is what the GUI would emit for
   the same input — close enough for parity-test ground truth.

Variants generated:

- ``03/`` — single-filament with FILAMENT-side customizations in
  ``different_settings_to_system[1]``. Exercises Task 5's ``applied`` path
  (filament name matches, customizations applied) which fixture 01 only
  hits as ``no_customizations``.
- ``05/`` — same as fixture 01 but with ``curr_bed_type = "Cool Plate"``
  instead of Textured PEI. Exercises bed-type carry-through across
  different bed types and confirms the bed-temperature lookups follow.

Variants requiring manual GUI authoring (printed at the end as TODO):

- ``04/`` — multi-filament 2-color benchy. Needs painted-mesh data
  (model_settings.config part-color-IDs + paint XML in 3dmodel.model)
  that is not safely produced by config mutation.
- ``06/`` — different printer family (X1C). Needs printer profile swap
  and may need a different model/process selection.

Usage::

    python3 scripts/generate_fixtures.py            # generate all auto variants
    python3 scripts/generate_fixtures.py --dry-run  # show what would be done
    python3 scripts/generate_fixtures.py 03         # generate only variant 03
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT.parent / "_fixture"
SOURCE_INPUT = (
    FIXTURE_ROOT
    / "01"
    / "reference-benchy-orca-no-filament-custom-settings.3mf"
)
ORCA_BINARY = Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")


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
    # Ensure layout is [process, filament_0, printer].
    while len(fp) < 3:
        fp.insert(-1 if fp else 0, "")
    # Inject filament_max_volumetric_speed + filament_flow_ratio into slot 1.
    fp[1] = "filament_max_volumetric_speed;filament_flow_ratio"
    settings["different_settings_to_system"] = fp
    # Set non-default values so the customization has visible effect.
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
        "output_name": "gui-benchy-with-filament-customizations_sliced.3mf",
        "mutate": _mutate_03,
    },
    "05": {
        "label": "Cool Plate bed type",
        "input_name": "reference-benchy-cool-plate.3mf",
        "output_name": "gui-benchy-cool-plate_sliced.3mf",
        "mutate": _mutate_05,
    },
}

MANUAL_VARIANTS = [
    (
        "04",
        "multi-filament 2-color benchy",
        [
            "Open `_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf` in OrcaSlicer.",
            "Add a second filament slot (e.g. another colour of Bambu PLA Basic).",
            "Use the paint tool (or a multi-part STL) so different parts of the benchy print with the two filaments.",
            "Save the project as `_fixture/04/reference-benchy-2color.3mf` (File → Export → Project).",
            "Click Slice. Save the sliced output as `_fixture/04/gui-benchy-2color_sliced.3mf` (File → Export → Sliced 3MF).",
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

    Other entries (geometry, model_settings, thumbnails, ...) are passed
    through byte-for-byte.
    """
    with zipfile.ZipFile(src, "r") as zin:
        names = zin.namelist()
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = zin.read(name)
                if name == "Metadata/project_settings.config":
                    settings = json.loads(data.decode())
                    settings = mutate_fn(settings)
                    data = json.dumps(settings, indent=1).encode()
                zout.writestr(name, data)


def slice_with_orca_cli(input_3mf: Path, output_3mf: Path) -> None:
    """Drive OrcaSlicer.app's CLI mode to slice ``input_3mf`` into ``output_3mf``.

    ``--min-save`` skips embedding the input mesh in the output (matches
    the GUI's slice-export behaviour). Profiles are taken from the 3MF's
    embedded project — no external ``--load-settings`` needed.
    """
    if not ORCA_BINARY.exists():
        raise RuntimeError(
            f"OrcaSlicer not found at {ORCA_BINARY}. Install OrcaSlicer.app "
            "from https://github.com/SoftFever/OrcaSlicer/releases or adjust "
            "ORCA_BINARY in this script."
        )

    with tempfile.TemporaryDirectory(prefix="orca-fixture-") as tmpdir:
        tmp_input = Path(tmpdir) / "input.3mf"
        tmp_output = Path(tmpdir) / "result.3mf"
        shutil.copy2(input_3mf, tmp_input)

        cmd = [
            str(ORCA_BINARY),
            "--slice", "1",
            "--allow-newer-file",
            "--min-save",
            "--export-3mf", str(tmp_output.name),
            str(tmp_input.name),
        ]
        result = subprocess.run(
            cmd,
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0 or not tmp_output.exists():
            raise RuntimeError(
                f"OrcaSlicer CLI failed (rc={result.returncode}).\n"
                f"stdout:\n{result.stdout[-2000:]}\n"
                f"stderr:\n{result.stderr[-2000:]}"
            )

        output_3mf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_output, output_3mf)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def generate(variant_id: str, dry_run: bool) -> None:
    spec = VARIANTS[variant_id]
    target_dir = FIXTURE_ROOT / variant_id
    input_path = target_dir / spec["input_name"]
    output_path = target_dir / spec["output_name"]

    print(f"\n=== variant {variant_id}: {spec['label']} ===")
    print(f"input  → {input_path.relative_to(FIXTURE_ROOT.parent)}")
    print(f"output → {output_path.relative_to(FIXTURE_ROOT.parent)}")

    if dry_run:
        print("(dry-run; no files written)")
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    mutate_3mf(SOURCE_INPUT, input_path, spec["mutate"])
    print(f"  ✓ wrote input ({input_path.stat().st_size:,} bytes)")

    print(f"  · slicing with OrcaSlicer CLI ...")
    slice_with_orca_cli(input_path, output_path)
    print(f"  ✓ wrote output ({output_path.stat().st_size:,} bytes)")


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
        help="Print what would be done without writing files or invoking OrcaSlicer.",
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

    print("\n=== Manual GUI authoring still needed for these ===")
    for variant_id, label, steps in MANUAL_VARIANTS:
        print(f"\n--- variant {variant_id}: {label} ---")
        for i, step in enumerate(steps, 1):
            print(f"  {i}. {step}")

    print(
        "\nDone. After running, regenerate or extend "
        "tests/integration/test_slice_v2_fidelity.py to pin parity for the "
        "new fixtures."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
