"""Detect drift between process_pages.json, process_allowlist.json, and dump-options.

Three drift modes detected:

1. Allowlist key not in dump-options output (typo, removed upstream, or
   filament/machine-domain leak).
2. Allowlist key in dump-options but not in process_pages.json (key
   exists in libslic3r but isn't surfaced in the GUI's process Tab —
   nowhere to render it).
3. process_pages.json references a key not in dump-options (Tab.cpp
   regex caught a stale reference, or upstream renamed the key).

Usage:
    python scripts/check_allowlist.py --check
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAYOUT = REPO_ROOT / "cpp" / "src" / "generated" / "process_pages.json"
DEFAULT_ALLOWLIST = REPO_ROOT / "app" / "process_allowlist.json"
DEFAULT_BINARY = os.environ.get(
    "ORCA_HEADLESS_BINARY", "/opt/orca-headless/bin/orca-headless")


def _layout_keys(layout_path: Path) -> set[str]:
    doc = json.loads(layout_path.read_text())
    return {
        k
        for p in doc.get("pages", [])
        for og in p.get("optgroups", [])
        for k in og.get("options", [])
    }


def _allowlist_keys(allowlist_path: Path) -> set[str]:
    return set(json.loads(allowlist_path.read_text()).get("options", []))


def _catalogue_keys(catalogue_path: Path) -> set[str]:
    doc = json.loads(catalogue_path.read_text())
    return {o["key"] for o in doc.get("options", [])}


def _generate_catalogue(binary_path: str, dest: Path) -> None:
    """Shell out to orca-headless dump-options, write the catalogue to dest."""
    proc = subprocess.run(
        [binary_path, "dump-options"],
        input=json.dumps({"out_path": str(dest)}).encode(),
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"orca-headless dump-options failed: rc={proc.returncode} "
            f"stderr={proc.stderr.decode(errors='replace')[-500:]}"
        )
    envelope = json.loads(proc.stdout)
    if envelope.get("status") != "ok":
        raise RuntimeError(f"dump-options error envelope: {envelope}")


def check_drift(
    layout_path: Path, allowlist_path: Path, catalogue_path: Path,
) -> list[str]:
    """Return a list of human-readable error messages; empty list = clean."""
    layout = _layout_keys(layout_path)
    allow  = _allowlist_keys(allowlist_path)
    cat    = _catalogue_keys(catalogue_path)
    errors: list[str] = []

    # Mode 1: allowlist key not in dump-options.
    for k in sorted(allow - cat):
        errors.append(
            f"allowlist key {k!r} is not in dump-options (typo, "
            f"removed upstream, or filament/machine-domain leak)"
        )

    # Mode 2: allowlist key in dump-options but not in pages.json.
    for k in sorted(allow & cat - layout):
        errors.append(
            f"allowlist key {k!r} is not surfaced in process_pages.json "
            f"(key exists in libslic3r but Tab.cpp doesn't expose it)"
        )

    # Mode 3: process_pages.json references a key not in dump-options.
    for k in sorted(layout - cat):
        errors.append(
            f"process_pages.json (Tab.cpp references) {k!r} but it is "
            f"not in dump-options — Tab.cpp may carry a stale reference"
        )

    return errors


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT)
    p.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    p.add_argument("--binary", default=DEFAULT_BINARY,
                   help="path to orca-headless (used to regenerate catalogue)")
    p.add_argument("--catalogue", type=Path,
                   help="reuse an existing catalogue JSON instead of running the binary")
    p.add_argument("--check", action="store_true",
                   help="exit non-zero on any drift (alias of default behaviour)")
    args = p.parse_args()

    if args.catalogue:
        catalogue_path = args.catalogue
    else:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            catalogue_path = Path(tf.name)
        try:
            _generate_catalogue(args.binary, catalogue_path)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    errors = check_drift(args.layout, args.allowlist, catalogue_path)
    if errors:
        for e in errors:
            print(f"drift: {e}", file=sys.stderr)
        return 1
    print("allowlist drift check: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
