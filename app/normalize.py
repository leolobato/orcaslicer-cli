"""Per-filament vector-length normalization for process profiles.

Background
----------
OrcaSlicer's GUI runs `Preset::normalize` on the combined config after loading
presets, which resizes per-filament vector options (e.g. `pressure_advance`)
to `len(filament_diameter)`. The `orca-slicer` CLI does not re-run this step
after combining `--load-settings` and `--load-filaments`, so keys that are
absent from vendor filament JSONs — populated only by `FullPrintConfig::defaults()`
at length 1 — stay at length 1 in the output.

For single-filament prints that's harmless. For multi-filament prints it's a
silent bug: slot 1's value falls back to slot 0's or to the hard-coded default.

Fix
---
Before handing the process profile JSON to `orca-slicer`, inject each target key
at length `n_filaments`. If the profile already carries the key at length 1,
repeat its value; if it carries length `n`, leave it alone; if it's missing,
inject the `FullPrintConfig::defaults()` value from `_DEFAULTS`.

The keys + defaults were extracted from OrcaSlicer v2.3.2's `PrintConfig.cpp`
and validated against an empirical GUI-vs-ours diff on a multi-filament 3MF.
See `docs/normalization-research.md` for the full derivation.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Per-filament vector keys we've observed to diverge between the GUI and CLI.
# All live in `Preset::filament_options()`; values come from `FullPrintConfig::defaults()`.
# Stored as strings — matches OrcaSlicer's JSON serialization for vector options.
# "nil" entries are nullable-type keys whose default is literally the nil sentinel.
_DEFAULTS: dict[str, str] = {
    "activate_chamber_temp_control": "0",
    "adaptive_pressure_advance": "0",
    "adaptive_pressure_advance_bridges": "0",
    "adaptive_pressure_advance_model": "0,0,0\n0,0,0",
    "adaptive_pressure_advance_overhangs": "0",
    "default_filament_colour": "",
    "dont_slow_down_outer_wall": "0",
    "enable_overhang_bridge_fan": "1",
    "enable_pressure_advance": "0",
    "filament_colour": "#F2754E",
    "filament_cooling_final_speed": "3.4",
    "filament_cooling_initial_speed": "2.2",
    "filament_cooling_moves": "4",
    "filament_ironing_flow": "nil",
    "filament_ironing_inset": "nil",
    "filament_ironing_spacing": "nil",
    "filament_ironing_speed": "nil",
    "filament_loading_speed": "28",
    "filament_loading_speed_start": "3",
    "filament_map": "1",
    "filament_multitool_ramming": "0",
    "filament_multitool_ramming_flow": "10",
    "filament_multitool_ramming_volume": "10",
    "filament_notes": "",
    "filament_ramming_parameters": (
        "120 100 6.6 6.8 7.2 7.6 7.9 8.2 8.7 9.4 9.9 10.0"
        "| 0.05 6.6 0.45 6.8 0.95 7.8 1.45 8.3 1.95 9.7 2.45 10"
        " 2.95 7.6 3.45 7.6 3.95 7.6 4.45 7.6 4.95 7.6"
    ),
    "filament_shrinkage_compensation_z": "100%",
    "filament_stamping_distance": "0",
    "filament_stamping_loading_speed": "0",
    "filament_toolchange_delay": "0",
    "filament_unloading_speed": "90",
    "filament_unloading_speed_start": "100",
    "idle_temperature": "0",
    "internal_bridge_fan_speed": "-1",
    "ironing_fan_speed": "-1",
    "pressure_advance": "0.02",
    "support_material_interface_fan_speed": "-1",
    "textured_cool_plate_temp": "40",
    "textured_cool_plate_temp_initial_layer": "40",
}


def normalize_process_profile(
    process_profile: dict[str, Any],
    n_filaments: int,
) -> dict[str, Any]:
    """Resize per-filament vector keys to length `n_filaments`.

    For each key in `_DEFAULTS`:
    - length 0 / missing → inject at length n with default value
    - length 1 → pad to n by repeating the single value
    - length == n → leave alone
    - length between 1 and n → pad to n by repeating the last value
    - length > n → leave alone (more filaments declared than loaded)

    Returns a new dict; does not mutate the input.
    """
    if n_filaments <= 1:
        return process_profile

    updates: dict[str, list[str]] = {}
    padded = 0
    injected = 0

    for key, default in _DEFAULTS.items():
        current = process_profile.get(key)

        if current is None:
            updates[key] = [default] * n_filaments
            injected += 1
            continue

        if not isinstance(current, list):
            # Scalar in a vector slot — treat as length 1 and pad.
            updates[key] = [str(current)] * n_filaments
            padded += 1
            continue

        if len(current) >= n_filaments:
            continue

        if len(current) == 0:
            updates[key] = [default] * n_filaments
            injected += 1
            continue

        pad_value = current[-1]
        updates[key] = list(current) + [pad_value] * (n_filaments - len(current))
        padded += 1

    if not updates:
        return process_profile

    logger.info(
        "Normalized process profile: %d key(s) padded, %d injected (target length=%d)",
        padded, injected, n_filaments,
    )
    return {**process_profile, **updates}
