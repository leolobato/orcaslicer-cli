"""Smoke test: orca-headless dump-options runs and emits sensible metadata.

Runs against the actual binary built into the container — guards against
regressions in the C++ extractor without faking the call.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import config as cfg


@pytest.mark.skipif(
    not Path(cfg.ORCA_HEADLESS_BINARY).exists(),
    reason="orca-headless binary not present (run inside container)",
)
def test_dump_options_emits_layer_height(tmp_path: Path) -> None:
    out = tmp_path / "opts.json"
    proc = subprocess.run(
        [cfg.ORCA_HEADLESS_BINARY, "dump-options"],
        input=json.dumps({"out_path": str(out)}).encode(),
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    envelope = json.loads(proc.stdout)
    assert envelope["status"] == "ok"
    catalogue = json.loads(out.read_text())
    by_key = {o["key"]: o for o in catalogue["options"]}

    assert "layer_height" in by_key
    lh = by_key["layer_height"]
    assert lh["category"] == "Quality"
    assert lh["type"] == "coFloat"
    assert lh["sidetext"]  # non-empty unit string
    assert lh["min"] is not None and lh["min"] >= 0.0
    # max is null for layer_height in libslic3r (no upper bound configured)
    assert lh["max"] is None or lh["max"] > lh["min"]
    assert lh["mode"] in {"simple", "advanced", "develop"}
    assert isinstance(lh["default"], str)

    # Filament-domain keys should be excluded.
    assert not any(k.startswith("filament_") for k in by_key), \
        "process dump leaked filament_* keys"
    assert not any(k.endswith("_filament") for k in by_key), \
        "process dump leaked *_filament keys"

    # An enum option should round-trip enum_values + enum_labels.
    assert "seam_position" in by_key
    sp = by_key["seam_position"]
    assert sp["type"] == "coEnum"
    assert sp["enum_values"], "seam_position should have enum_values"
