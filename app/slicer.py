"""Slicing logic adapted from bambu-poc/print_3mf.py."""

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import ORCA_BINARY
from .normalize import normalize_process_profile
from .profiles import ProfileNotFoundError, get_profile, get_profile_by_id_or_name
from .threemf import (
    extract_plate,
    get_build_volume,
    get_plate_count,
    get_used_filament_slots,
    validate_model_fits,
)

logger = logging.getLogger(__name__)

# Serialize slicing requests — CPU-heavy
_slice_semaphore = asyncio.Semaphore(1)

# API-facing plate type values mapped to OrcaSlicer curr_bed_type labels.
PLATE_TYPE_API_TO_ORCA = {
    "cool_plate": "Cool Plate",
    "engineering_plate": "Engineering Plate",
    "high_temp_plate": "High Temp Plate",
    "textured_pei_plate": "Textured PEI Plate",
    "textured_cool_plate": "Textured Cool Plate",
    "supertack_plate": "Supertack Plate",
}
SUPPORTED_PLATE_TYPES = tuple(PLATE_TYPE_API_TO_ORCA.keys())

# Keys that are profile metadata, not slicer settings
_PROFILE_META_KEYS = {"name", "from", "inherits", "version", "type", "setting_id"}

# Valid values for parameter overrides
VALID_INFILL_PATTERNS = frozenset({
    "grid", "line", "cubic", "cubicsubdiv", "gyroid", "lightning",
    "honeycomb", "3dhoneycomb", "rectilinear", "monotonic", "monotoniclines",
    "alignedrectilinear", "hilbertcurve", "archimedeanchords",
    "octagramspiral", "supportcubic", "adaptivecubic",
})
VALID_SUPPORT_TYPES = frozenset({"normal", "tree", "none"})
VALID_BRIM_TYPES = frozenset({
    "auto_brim", "outer_only", "inner_only", "outer_and_inner", "no_brim",
})

# Parameters that must be clamped to a minimum value
_CLAMP_RULES = {
    "raft_first_layer_expansion": 0,
    "solid_infill_filament": 1,
    "sparse_infill_filament": 1,
    "tree_support_wall_count": 0,
    "wall_filament": 1,
}

# Filament slot/material identity comes from the explicit filament profiles
# loaded for the slice, not from embedded process settings in the source 3MF.
_NON_TRANSFERABLE_PROCESS_KEYS = {
    "default_filament_profile",
}


@dataclass
class FilamentTransferEntry:
    slot: int  # 0-indexed filament slot
    original_filament: str  # `filament_settings_id[slot]` from the source 3MF
    selected_filament: str  # `name` of the filament profile supplied for this slice
    status: str  # "applied", "filament_changed", "no_customizations"
    transferred: list[dict[str, Any]] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)


@dataclass
class SettingsTransferResult:
    status: str  # "applied", "no_customizations", "no_3mf_settings"
    transferred: list[dict[str, str]] = field(default_factory=list)
    customized_keys: set[str] = field(default_factory=set)
    filaments: list[FilamentTransferEntry] = field(default_factory=list)
    filament_customized_keys: dict[int, set[str]] = field(default_factory=dict)
    machine_customized_keys: set[str] = field(default_factory=set)
    machine_transferred: list[dict[str, Any]] = field(default_factory=list)




class ModelTooBigError(Exception):
    pass


class SlicingError(Exception):
    def __init__(
        self,
        message: str,
        orca_output: str | None = None,
        critical_warnings: list[str] | None = None,
    ):
        super().__init__(message)
        self.orca_output = orca_output
        self.critical_warnings = critical_warnings or []


# OrcaSlicer status-callback lines carry a `message_type` field where
# `2` = Critical (GUI would show a dismissable warning, CLI treats as fatal).
# Example line:
#   default_status_callback: percent=-1, warning_step=6, message=It seems object
#   X.stl has floating regions. ..., message_type=2
_CRITICAL_WARNING_RE = re.compile(
    r"default_status_callback:[^\n]*?message=(.+?), message_type=2\b"
)


def _extract_critical_warnings(orca_output: str) -> list[str]:
    """Return the deduplicated list of critical (message_type=2) warnings."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _CRITICAL_WARNING_RE.finditer(orca_output):
        msg = match.group(1).strip()
        if msg and msg not in seen:
            seen.add(msg)
            result.append(msg)
    return result


def _read_result_json(tmpdir: str) -> str:
    """Read the `result.json` file OrcaSlicer writes on exit, if present."""
    path = os.path.join(tmpdir, "result.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _format_exit_reason(returncode: int) -> str:
    """Render a non-zero subprocess returncode as a human-readable message.

    Negative returncodes mean the process was killed by signal `-returncode`
    (POSIX semantics surfaced by `asyncio.subprocess`). Surface that as the
    signal name plus a hint, since the binary's stderr is typically empty on
    a crash and the message would otherwise just be "exited with code -11".
    """
    if returncode >= 0:
        return f"OrcaSlicer exited with code {returncode}"
    sig = -returncode
    try:
        name = signal.Signals(sig).name
    except ValueError:
        name = f"signal {sig}"
    hint = (
        " — likely a malformed mesh; try opening and re-exporting the file "
        "in OrcaSlicer or Bambu Studio"
        if sig in (signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGFPE)
        else ""
    )
    return f"OrcaSlicer crashed ({name}){hint}"


def _build_failure(
    returncode: int,
    tmpdir: str,
    orca_output: str,
) -> SlicingError:
    """Assemble a SlicingError with the most informative message we can extract."""
    critical = _extract_critical_warnings(orca_output)
    exit_reason = _read_result_json(tmpdir)
    full_output = orca_output
    if exit_reason:
        full_output = f"{orca_output}\n\n=== result.json ===\n{exit_reason}"
    if critical:
        message = "; ".join(critical)
    else:
        message = _format_exit_reason(returncode)
    return SlicingError(message, orca_output=full_output, critical_warnings=critical)


def _is_transferable_process_key(key: str) -> bool:
    """Return True when a 3MF setting is safe to copy onto a target process profile."""
    if key in _NON_TRANSFERABLE_PROCESS_KEYS:
        return False
    if key.startswith("filament_"):
        return False
    if key.endswith("_filament"):
        return False
    return True


def _extract_declared_customizations(threemf_settings: dict[str, Any]) -> set[str]:
    """Extract process-level keys explicitly marked as customized via different_settings_to_system.

    OrcaSlicer stores this as a list where the first element is a semicolon-separated
    string of process setting keys that differ from the system profile.
    """
    diff_to_system = threemf_settings.get("different_settings_to_system")
    if not isinstance(diff_to_system, list) or not diff_to_system:
        return set()
    process_diffs = diff_to_system[0]
    if not isinstance(process_diffs, str) or not process_diffs.strip():
        return set()
    return {k.strip() for k in process_diffs.split(";") if k.strip()}


def _extract_declared_filament_customizations(
    threemf_settings: dict[str, Any],
) -> list[set[str]]:
    """Per-filament customization allowlists from `different_settings_to_system`.

    OrcaSlicer stores this field as ``[process, filament_0, filament_1, ...,
    filament_N-1, printer]`` (see ``PresetBundle::load_3mf_*`` in
    ``OrcaSlicer/src/libslic3r/PresetBundle.cpp:3585,3595`` — printer is loaded
    from index ``num_filaments + 1``). So filament slot ``i`` lives at
    ``different_settings_to_system[i + 1]``, and the LAST slot is always the
    printer (handled separately by ``_extract_declared_machine_customizations``).

    Returns an empty list when the fingerprint is missing or has no filament
    slots (length < 3).
    """
    diff_to_system = threemf_settings.get("different_settings_to_system")
    if not isinstance(diff_to_system, list) or len(diff_to_system) < 3:
        return []
    # Filament slots are everything between the process slot (index 0) and
    # the printer slot (last index).
    result: list[set[str]] = []
    for entry in diff_to_system[1:-1]:
        if isinstance(entry, str) and entry.strip():
            result.append({k.strip() for k in entry.split(";") if k.strip()})
        else:
            result.append(set())
    return result


def _extract_declared_machine_customizations(
    threemf_settings: dict[str, Any],
) -> set[str]:
    """Machine/printer customization allowlist from the LAST entry of
    ``different_settings_to_system``.

    OrcaSlicer's layout for a 3MF with N filaments is
    ``[process, filament_0, ..., filament_N-1, printer]`` — so the printer
    slot is always the trailing entry. Returns an empty set when the
    fingerprint is missing, too short to contain a printer slot (length < 2),
    or that slot is blank.
    """
    diff_to_system = threemf_settings.get("different_settings_to_system")
    if not isinstance(diff_to_system, list) or len(diff_to_system) < 2:
        return set()
    entry = diff_to_system[-1]
    if not isinstance(entry, str) or not entry.strip():
        return set()
    return {k.strip() for k in entry.split(";") if k.strip()}


def _overlay_3mf_filament_settings(
    filament_profile: dict[str, Any],
    threemf_settings: dict[str, Any],
    slot_idx: int,
    allowed_keys: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Overlay per-filament 3MF customizations onto a loaded filament profile.

    The 3MF's `project_settings.config` stores per-filament values as combined
    multi-element vectors (e.g. `nozzle_temperature: ["220", "225"]` where
    index `i` is filament slot `i`). A loaded filament profile keeps its own
    value as a 1-element vector. For each key in `allowed_keys`, this extracts
    `threemf_settings[key][slot_idx]` and writes it as `[value]` into the
    filament profile.

    Returns `(updated_profile, entries)` where `entries` is a list of
    `{key, value, original}` dicts for display in response headers.
    """
    overrides: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    for key in allowed_keys:
        if key not in threemf_settings:
            continue
        threemf_val = threemf_settings[key]
        if isinstance(threemf_val, list):
            if slot_idx >= len(threemf_val):
                continue
            slot_val = threemf_val[slot_idx]
        else:
            slot_val = threemf_val

        profile_val = filament_profile.get(key)
        if isinstance(profile_val, list):
            new_val: Any = [slot_val]
            original = profile_val[0] if profile_val else None
        else:
            new_val = slot_val
            original = profile_val

        if profile_val == new_val:
            continue
        overrides[key] = new_val
        entries.append({"key": key, "value": slot_val, "original": original})

    if overrides:
        return {**filament_profile, **overrides}, entries
    return filament_profile, []


def _overlay_3mf_machine_settings(
    machine_profile: dict[str, Any],
    threemf_settings: dict[str, Any],
    allowed_keys: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Overlay 3MF-declared machine/printer customizations onto a loaded machine profile.

    Unlike the per-filament overlay, the machine overlay has no name guard:
    the printer profile is fixed by the slice request, so any customization
    declared in the 3MF's printer slot of ``different_settings_to_system``
    applies straight onto the resolved machine profile.

    Per-extruder list values (e.g. ``machine_max_jerk_x: ["20", "9"]`` where
    each element is one motion profile) are copied wholesale; scalar values
    are written as-is.

    Returns ``(updated_profile, entries)`` where ``entries`` is a list of
    ``{key, value, original}`` dicts for surfacing in response headers.
    """
    overrides: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    for key in allowed_keys:
        if key not in threemf_settings:
            continue
        new_val = threemf_settings[key]
        original = machine_profile.get(key)
        if original == new_val:
            continue
        overrides[key] = new_val
        entries.append({"key": key, "value": new_val, "original": original})

    if overrides:
        return {**machine_profile, **overrides}, entries
    return machine_profile, []


_PLATER_NAME_METADATA_RE = re.compile(
    r'[ \t]*<metadata\s+key="plater_name"\s+value="[^"]*"\s*/>[ \t]*(?:\r?\n)?',
)


def _strip_plater_name_metadata(xml: str) -> tuple[str, int]:
    """Remove `<metadata key="plater_name" .../>` entries from model_settings XML.

    Works around an OrcaSlicer 2.3.2 CLI bug: `PartPlateList::load_from_3mf_structure`
    forwards each plate's `plater_name` to `PartPlate::set_plate_name`, which
    unconditionally calls `generate_plate_name_texture()`. That function does
    `m_partplate_list->m_plater->get_view3D_canvas3D()` — but `m_plater` is null
    in CLI mode, so any 3MF whose plate has a non-empty name segfaults the
    slicer. Stripping the entry leaves orca to call its empty-name path, which
    is purely cosmetic for slicing output.
    """
    new_xml, n = _PLATER_NAME_METADATA_RE.subn("", xml)
    return new_xml, n


# Authoritative per-filament-slot keys from OrcaSlicer's
# `s_Preset_filament_options` (`OrcaSlicer/src/libslic3r/Preset.cpp:960-998`),
# plus the three canonical filament-identity keys filtered out of that list
# (`filament_colour`, `filament_settings_id`, `filament_ids`).
#
# A naive "every list whose length matches the filament count" heuristic is
# unsafe: many project keys coincidentally carry length-N lists for unrelated
# reasons — `bed_exclude_area` / `printable_area` (polygon vertices),
# `print_compatible_printers` / `compatible_printers` (string lists),
# `chamber_temperatures` (per-print-condition values), etc. Truncating those
# corrupts the project (a 4-vertex polygon becomes a 1-vertex polygon and
# Orca segfaults on area computation). Only keys explicitly per-filament in
# OrcaSlicer's data model belong here.
#
# Profile metadata keys from `s_Preset_filament_options` that are NOT
# per-slot in `project_settings.config` are intentionally excluded:
# `compatible_prints*`, `compatible_printers*`, `inherits`, `filament_vendor`.
_PER_FILAMENT_KEYS: frozenset[str] = frozenset({
    # Filament identity — filtered out of s_Preset_filament_options but
    # always one entry per authored slot in project_settings.config.
    "filament_colour", "filament_settings_id", "filament_ids",
    # From s_Preset_filament_options (Preset.cpp:960-998).
    "default_filament_colour", "required_nozzle_HRC", "filament_diameter",
    "pellet_flow_coefficient", "volumetric_speed_coefficients", "filament_type",
    "filament_soluble", "filament_is_support", "filament_printable",
    "filament_max_volumetric_speed", "filament_adaptive_volumetric_speed",
    "filament_flow_ratio", "filament_density", "filament_adhesiveness_category",
    "filament_cost", "filament_minimal_purge_on_wipe_tower",
    "filament_tower_interface_pre_extrusion_dist",
    "filament_tower_interface_pre_extrusion_length",
    "filament_tower_ironing_area", "filament_tower_interface_purge_volume",
    "filament_tower_interface_print_temp",
    "nozzle_temperature", "nozzle_temperature_initial_layer",
    "cool_plate_temp", "textured_cool_plate_temp", "eng_plate_temp",
    "hot_plate_temp", "textured_plate_temp",
    "cool_plate_temp_initial_layer", "textured_cool_plate_temp_initial_layer",
    "eng_plate_temp_initial_layer", "hot_plate_temp_initial_layer",
    "textured_plate_temp_initial_layer", "supertack_plate_temp_initial_layer",
    "supertack_plate_temp",
    "temperature_vitrification", "reduce_fan_stop_start_freq",
    "dont_slow_down_outer_wall", "slow_down_for_layer_cooling",
    "fan_min_speed", "fan_max_speed",
    "enable_overhang_bridge_fan", "overhang_fan_speed", "overhang_fan_threshold",
    "close_fan_the_first_x_layers", "full_fan_speed_layer",
    "fan_cooling_layer_time", "slow_down_layer_time", "slow_down_min_speed",
    "filament_start_gcode", "filament_end_gcode",
    "activate_air_filtration", "during_print_exhaust_fan_speed",
    "complete_print_exhaust_fan_speed",
    "filament_retraction_length", "filament_z_hop", "filament_z_hop_types",
    "filament_retract_lift_above", "filament_retract_lift_below",
    "filament_retract_lift_enforce", "filament_retraction_speed",
    "filament_deretraction_speed", "filament_retract_restart_extra",
    "filament_retraction_minimum_travel", "filament_retract_when_changing_layer",
    "filament_wipe", "filament_retract_before_wipe",
    "filament_wipe_distance", "additional_cooling_fan_speed",
    "nozzle_temperature_range_low", "nozzle_temperature_range_high",
    "filament_extruder_variant",
    "enable_pressure_advance", "pressure_advance",
    "adaptive_pressure_advance", "adaptive_pressure_advance_model",
    "adaptive_pressure_advance_overhangs", "adaptive_pressure_advance_bridges",
    "chamber_temperature",
    "filament_shrink", "filament_shrinkage_compensation_z",
    "support_material_interface_fan_speed", "internal_bridge_fan_speed",
    "filament_notes", "ironing_fan_speed",
    "filament_ironing_flow", "filament_ironing_spacing",
    "filament_ironing_inset", "filament_ironing_speed",
    "filament_loading_speed", "filament_loading_speed_start",
    "filament_unloading_speed", "filament_unloading_speed_start",
    "filament_toolchange_delay", "filament_cooling_moves",
    "filament_stamping_loading_speed", "filament_stamping_distance",
    "filament_cooling_initial_speed", "filament_cooling_final_speed",
    "filament_ramming_parameters", "filament_multitool_ramming",
    "filament_multitool_ramming_volume", "filament_multitool_ramming_flow",
    "activate_chamber_temp_control",
    "filament_long_retractions_when_cut",
    "filament_retraction_distances_when_cut", "idle_temperature",
    "filament_change_length", "filament_flush_volumetric_speed",
    "filament_flush_temp",
    "long_retractions_when_ec", "retraction_distances_when_ec",
})


def _truncate_per_filament_lists(
    settings: dict[str, Any], target_n: int
) -> dict[str, int]:
    """Truncate per-filament list-valued keys in project_settings to target_n.

    OrcaSlicer's G-code export validates `flush_volumes_matrix.size()` against
    `filament_colour.size()` (see `Slic3r::GCode::_post_process` /
    `OrcaSlicer/src/libslic3r/GCode.cpp:5394-5411`):

        size_t filament_count_tmp = temp_filament_color.size();
        if (filament_count_tmp * filament_count_tmp * heads_count_tmp
                == temp_flush_volumes_matrix.size()) { ... }
        else if (filament_count_tmp == 1) { ... }
        else throw "Flush volumes matrix do not match to the correct size!";

    `_resize_flush_volumes` rewrites the matrix to N×N, but the 3MF's
    `project_settings.config` carries dozens of other per-filament list-valued
    keys (`filament_colour`, `filament_settings_id`, `nozzle_temperature`,
    `*_plate_temp*`, `filament_max_volumetric_speed`, …) sized to the
    originally-authored filament count. After `_trim_unused_filament_ids`
    drops trailing slots, those lists still hold the original count and
    `filament_colour.size()` no longer agrees with the resized matrix —
    Orca then aborts at G-code export with the size-mismatch above.

    Truncates exactly the keys OrcaSlicer documents as per-filament (see
    `_PER_FILAMENT_KEYS`). Any list-valued entry longer than `target_n` is
    shrunk; entries that are already short enough or aren't lists are left
    alone, so an upstream Orca change that flips a key from list to scalar
    won't fight us.

    Returns {key: original_length} for every key touched.
    """
    if target_n <= 0:
        return {}

    truncated: dict[str, int] = {}
    for key in _PER_FILAMENT_KEYS:
        value = settings.get(key)
        if not isinstance(value, list):
            continue
        if len(value) <= target_n:
            continue
        truncated[key] = len(value)
        settings[key] = value[:target_n]
    return truncated


# Project-level structural arrays whose shape is
# `[process, fil_1, ..., fil_N, printer]` (length N+2). Confirmed in
# `OrcaSlicer/src/OrcaSlicer.cpp:2710-2713`:
#
#     inherits_group.resize(filament_count + 2, std::string());
#     different_settings.resize(filament_count + 2, std::string());
#
# These are NOT per-filament — `[0]` is the process slot and `[N+1]` is the
# printer slot — so they don't belong in `_PER_FILAMENT_KEYS`. Truncation
# rule: keep `[0]`, keep `[1..target_n]`, keep `[N+1]` (last entry).
_STRUCTURAL_FILAMENT_ARRAYS: frozenset[str] = frozenset({
    "inherits_group",
    "different_settings_to_system",
})


def _truncate_structural_arrays(
    settings: dict[str, Any], target_n: int
) -> dict[str, int]:
    """Truncate `[process, fil_*, printer]`-shaped arrays to `target_n + 2`.

    OrcaSlicer's CLI segfaults on file load when `inherits_group.size()`
    disagrees with `filament_settings_id.size()`. The crash site is
    `OrcaSlicer.cpp:1647-1655`, which reads `current_filaments_name[index-1]`
    (sourced from `filament_settings_id`) while iterating `index` up to
    `inherits_group.size() - 1`. If the project carries the original
    N-filament `inherits_group` but `filament_settings_id` was truncated to
    M < N, the loop reads past the end of `current_filaments_name`. OrcaSlicer
    later resizes both arrays itself at line 2712, but the crashing loop
    runs first.

    Anchors on the original filament count via `len(filament_settings_id)`,
    so this MUST run before `_truncate_per_filament_lists` truncates that
    key. Only touches arrays whose length is exactly `original_n + 2`, so
    a malformed project that ships with a different shape is left alone.

    Returns {key: original_length} for every key touched.
    """
    if target_n <= 0:
        return {}

    anchor = settings.get("filament_settings_id")
    if not isinstance(anchor, list) or not anchor:
        return {}
    original_n = len(anchor)
    if original_n <= target_n:
        return {}

    expected_len = original_n + 2
    truncated: dict[str, int] = {}
    for key in _STRUCTURAL_FILAMENT_ARRAYS:
        value = settings.get(key)
        if not isinstance(value, list) or len(value) != expected_len:
            continue
        # [process] + [fil_1..fil_target_n] + [printer]
        settings[key] = [value[0]] + list(value[1 : target_n + 1]) + [value[-1]]
        truncated[key] = expected_len
    return truncated


def _resize_flush_volumes(
    settings: dict[str, Any], target_n: int, nozzle_count: int = 1,
) -> bool:
    """Resize `flush_volumes_matrix` to N×N×H, `flush_volumes_vector` to 2N,
    and `flush_multiplier` to H — where N is the loaded filament count and
    H is the target machine's nozzle count.

    OrcaSlicer's G-code writer aborts with `Flush volumes matrix do not match
    to the correct size!` when
    `filament_colour.size()² × flush_multiplier.size() != flush_volumes_matrix.size()`
    (`GCode.cpp:5394-5411`). The 3MF carries all three sized for the printer
    it was authored with; if it was authored on a multi-nozzle printer (e.g.
    H2D) and we're slicing onto a single-nozzle one (or vice versa),
    `flush_multiplier` stays at the source size and breaks the check even
    after the matrix is rewritten.

    Preserves matrix entries `[i][j]` where both indices remain valid; fills
    new cells with 140 mm³ off-diagonal and 0 on-diagonal (OrcaSlicer's
    defaults). Replicates the preserved N×N block across all H nozzles.
    Returns True if any field was modified.

    NOTE: Resizing the matrix alone is not sufficient — `filament_colour` and
    other per-filament list keys must also be truncated. See
    `_truncate_per_filament_lists`.
    """
    if target_n <= 0:
        return False
    if nozzle_count <= 0:
        nozzle_count = 1
    changed = False

    matrix = settings.get("flush_volumes_matrix")
    if isinstance(matrix, list):
        target_len = target_n * target_n * nozzle_count
        if len(matrix) != target_len:
            old_len = len(matrix)
            # Detect old layout. The 3MF stores matrix as
            # ``H × (N × N)`` flattened. Try the layout that matches
            # ``flush_multiplier.size()`` first, then fall back to single-head.
            old_multiplier = settings.get("flush_multiplier")
            old_h = (
                len(old_multiplier)
                if isinstance(old_multiplier, list) and old_multiplier
                else 1
            )
            old_n = 0
            if old_h > 0 and old_len % old_h == 0:
                per_head = old_len // old_h
                candidate = int(per_head ** 0.5)
                if candidate * candidate == per_head:
                    old_n = candidate
            if old_n == 0:
                # Layout didn't match; treat as single-head square.
                candidate = int(old_len ** 0.5)
                if candidate * candidate == old_len:
                    old_n = candidate
                    old_h = 1
            new_matrix: list[Any] = []
            for h in range(nozzle_count):
                for i in range(target_n):
                    for j in range(target_n):
                        if i < old_n and j < old_n and h < old_h:
                            new_matrix.append(
                                matrix[h * old_n * old_n + i * old_n + j]
                            )
                        else:
                            new_matrix.append("0" if i == j else "140")
            logger.info(
                "Resized flush_volumes_matrix: %d -> %d entries (N=%d, H=%d)",
                old_len, target_len, target_n, nozzle_count,
            )
            settings["flush_volumes_matrix"] = new_matrix
            changed = True

    vector = settings.get("flush_volumes_vector")
    if isinstance(vector, list):
        target_vec_len = target_n * 2
        if len(vector) != target_vec_len:
            old_len = len(vector)
            new_vector = list(vector[:target_vec_len])
            while len(new_vector) < target_vec_len:
                new_vector.append("140")
            logger.info(
                "Resized flush_volumes_vector: %d -> %d entries",
                old_len, target_vec_len,
            )
            settings["flush_volumes_vector"] = new_vector
            changed = True

    multiplier = settings.get("flush_multiplier")
    if isinstance(multiplier, list):
        if len(multiplier) != nozzle_count:
            old_len = len(multiplier)
            fill = multiplier[0] if multiplier else "1"
            new_multiplier = list(multiplier[:nozzle_count])
            while len(new_multiplier) < nozzle_count:
                new_multiplier.append(fill)
            logger.info(
                "Resized flush_multiplier: %d -> %d entries (H=%d)",
                old_len, nozzle_count, nozzle_count,
            )
            settings["flush_multiplier"] = new_multiplier
            changed = True

    return changed


def _sanitize_3mf(
    filepath: str,
    tmpdir: str,
    machine_profile: dict[str, Any] | None = None,
    target_filament_count: int = 0,
) -> str:
    """Fix invalid parameter values, rebrand printer identity, and strip
    GUI-only metadata that crashes OrcaSlicer's CLI.

    When the 3MF was authored for a different printer than the target
    `machine_profile`, OrcaSlicer CLI takes a "foreign vendor" path
    (`_load_model_from_file: found 3mf from other vendor, split as instance`)
    that auto-arranges the model and can spuriously flag repositioned objects
    as having floating regions — blocking the slice even when the same model
    + target profile combination succeeds in the GUI. The GUI avoids this by
    rewriting `printer_model` / `printer_settings_id` in memory when the user
    changes printer. We mirror that behavior here so CLI slices match GUI
    behavior for cross-printer 3MFs.

    Also strips `plater_name` metadata from `model_settings.config` to dodge
    OrcaSlicer 2.3.2's null-`m_plater` deref in `generate_plate_name_texture()`.

    When `target_filament_count` is positive, resizes `flush_volumes_matrix`
    and `flush_volumes_vector` to match and truncates every per-filament list
    key (`filament_colour`, `nozzle_temperature`, `*_plate_temp*`, …) to the
    same length — OrcaSlicer's G-code writer cross-checks all of these and
    aborts on size mismatch when the slice runs with fewer filaments than
    the 3MF was authored with.
    """
    settings_file = "Metadata/project_settings.config"
    model_settings_file = "Metadata/model_settings.config"
    with zipfile.ZipFile(filepath, "r") as zf:
        names = set(zf.namelist())
        has_settings = settings_file in names
        has_model_settings = model_settings_file in names

        if not has_settings and not has_model_settings:
            return filepath

        settings: dict[str, Any] | None = None
        settings_changed = False
        if has_settings:
            settings = json.loads(zf.read(settings_file).decode())

            # Clamp values that must meet minimums
            for key, min_val in _CLAMP_RULES.items():
                if key in settings:
                    val = settings[key]
                    try:
                        num = float(val) if isinstance(val, str) else val
                        if num < min_val:
                            settings[key] = str(min_val) if isinstance(val, str) else min_val
                            settings_changed = True
                    except (ValueError, TypeError):
                        pass

            # Rebrand printer identity to match target, to avoid OrcaSlicer's
            # foreign-vendor path. The values come from the machine profile's
            # own `printer_model` and `name` (the profile name doubles as the
            # printer_settings_id OrcaSlicer embeds on GUI save).
            if machine_profile:
                target_model = machine_profile.get("printer_model")
                target_settings_id = machine_profile.get("name")
                if target_model and settings.get("printer_model") != target_model:
                    logger.info(
                        "Rebranding 3MF printer_model: %r -> %r",
                        settings.get("printer_model"), target_model,
                    )
                    settings["printer_model"] = target_model
                    settings_changed = True
                if target_settings_id and settings.get("printer_settings_id") != target_settings_id:
                    logger.info(
                        "Rebranding 3MF printer_settings_id: %r -> %r",
                        settings.get("printer_settings_id"), target_settings_id,
                    )
                    settings["printer_settings_id"] = target_settings_id
                    settings_changed = True

            target_nozzle_count = 1
            if machine_profile:
                nozzles = machine_profile.get("nozzle_diameter")
                if isinstance(nozzles, list) and nozzles:
                    target_nozzle_count = len(nozzles)
            if _resize_flush_volumes(
                settings, target_filament_count, target_nozzle_count,
            ):
                settings_changed = True

            # Order matters: structural arrays anchor on the *original*
            # `filament_settings_id` length, which `_truncate_per_filament_lists`
            # rewrites in place.
            truncated_structural = _truncate_structural_arrays(
                settings, target_filament_count,
            )
            if truncated_structural:
                logger.info(
                    "Truncated %d structural array(s) to %d+2 entries: %s",
                    len(truncated_structural), target_filament_count,
                    ", ".join(sorted(truncated_structural.keys())),
                )
                settings_changed = True

            truncated = _truncate_per_filament_lists(settings, target_filament_count)
            if truncated:
                sample = sorted(truncated.keys())[:8]
                more = "" if len(truncated) <= len(sample) else f" (+{len(truncated) - len(sample)} more)"
                logger.info(
                    "Truncated %d per-filament list key(s) from %d -> %d entries: %s%s",
                    len(truncated), next(iter(truncated.values())),
                    target_filament_count, ", ".join(sample), more,
                )
                settings_changed = True

            # Log the final post-sanitize sizes for the keys OrcaSlicer
            # cross-checks at G-code export. Lets us diagnose mismatches
            # straight from the slicer log next time the size check trips.
            def _len_or_none(key: str) -> str:
                value = settings.get(key)
                return str(len(value)) if isinstance(value, list) else "—"
            logger.info(
                "Post-sanitize sizes: filament_colour=%s, "
                "filament_settings_id=%s, flush_volumes_matrix=%s, "
                "flush_multiplier=%s (target_n=%d, nozzle_count=%d)",
                _len_or_none("filament_colour"),
                _len_or_none("filament_settings_id"),
                _len_or_none("flush_volumes_matrix"),
                _len_or_none("flush_multiplier"),
                target_filament_count, target_nozzle_count,
            )

        # Strip `plater_name` metadata to work around the CLI null-deref in
        # `PartPlate::generate_plate_name_texture()` (OrcaSlicer 2.3.2).
        model_settings_xml: str | None = None
        if has_model_settings:
            xml_raw = zf.read(model_settings_file).decode()
            stripped_xml, n_stripped = _strip_plater_name_metadata(xml_raw)
            if n_stripped > 0:
                logger.info(
                    "Stripped %d plater_name metadata entries from %s "
                    "(workaround for OrcaSlicer 2.3.2 CLI null-deref)",
                    n_stripped, model_settings_file,
                )
                model_settings_xml = stripped_xml

        if not settings_changed and model_settings_xml is None:
            return filepath

        sanitized = os.path.join(tmpdir, "sanitized.3mf")
        with zipfile.ZipFile(sanitized, "w") as zf_out:
            with zipfile.ZipFile(filepath, "r") as zf_in:
                for item in zf_in.infolist():
                    if item.filename == settings_file and settings_changed:
                        zf_out.writestr(item.filename, json.dumps(settings, indent=2))
                    elif item.filename == model_settings_file and model_settings_xml is not None:
                        zf_out.writestr(item.filename, model_settings_xml)
                    else:
                        zf_out.writestr(item, zf_in.read(item.filename))
        return sanitized


def _overlay_3mf_settings(
    process_profile: dict[str, Any],
    threemf_settings: dict[str, Any],
    allowed_keys: set[str],
) -> tuple[dict[str, Any], set[str]]:
    """Overlay 3MF project settings onto process profile to preserve user choices.

    Only keys in `allowed_keys` (derived from the 3MF's
    `different_settings_to_system[0]` fingerprint) are considered. Passing an
    empty set transfers nothing.

    A key is transferred even when absent from the resolved process profile —
    `different_settings_to_system` lists keys that diverge from system defaults,
    and OrcaSlicer often realizes those defaults via compile-time constants
    rather than profile JSON (e.g. `brim_type` defaults to `auto_brim` and is
    not written into any BBL profile). Requiring presence would let the slicer
    silently fall back to the default and discard the user's choice.

    Returns (updated_profile, set_of_overlaid_keys).
    """
    overrides = {}
    for k in allowed_keys:
        if (
            k not in threemf_settings
            or k in _PROFILE_META_KEYS
            or not _is_transferable_process_key(k)
        ):
            continue
        tv = threemf_settings[k]
        if k not in process_profile:
            overrides[k] = tv
            continue
        pv = process_profile[k]
        if type(pv) == type(tv):
            overrides[k] = tv
        elif isinstance(pv, list) and isinstance(tv, str):
            overrides[k] = [tv] * len(pv) if pv else [tv]
        elif isinstance(pv, str) and isinstance(tv, list):
            overrides[k] = tv[0] if tv else pv
        else:
            overrides[k] = tv
    if overrides:
        return {**process_profile, **overrides}, set(overrides.keys())
    return process_profile, set()




def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _parse_progress_percent(line: str) -> int | None:
    """Extract a percentage (0-100) from a line of OrcaSlicer output."""
    match = re.search(r'(\d{1,3})%', line)
    if match:
        val = int(match.group(1))
        if 0 <= val <= 100:
            return val
    return None


# Patterns in OrcaSlicer output that indicate a phase change.
# Order matters — first match wins.
_ORCA_PHASE_PATTERNS = [
    (re.compile(r'Initializing StaticPrintConfigs', re.IGNORECASE), "initializing", "Initializing slicer"),
    (re.compile(r'arrange.*object|auto_arrange|arranging', re.IGNORECASE), "arranging", "Arranging objects on plate"),
    (re.compile(r'Slicing object|slice region|slice_region|slice objects', re.IGNORECASE), "slicing_objects", "Slicing objects"),
    (re.compile(r'Generating (perimeters|infill|support|skirt|brim|raft)', re.IGNORECASE), "generating", "Generating toolpaths"),
    (re.compile(r'Exporting G-code|export_gcode|gcode_export', re.IGNORECASE), "exporting_gcode", "Exporting G-code"),
]


def _detect_orca_phase(line: str) -> tuple[str, str] | None:
    """Detect a slicer phase change from an OrcaSlicer output line."""
    for pattern, phase, message in _ORCA_PHASE_PATTERNS:
        if pattern.search(line):
            return phase, message
    return None


def _apply_parameter_overrides(
    process_profile: dict[str, Any],
    overrides: dict[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    """Apply explicit API parameter overrides onto the process profile.

    Returns (updated_profile, set_of_applied_keys).
    All values are formatted to match OrcaSlicer's internal conventions.
    """
    applied: dict[str, Any] = {}

    if "layer_height" in overrides:
        val = str(overrides["layer_height"])
        applied["layer_height"] = val
        # initial_layer_print_height often matches layer_height
        if "initial_layer_print_height" in process_profile:
            cur = float(process_profile["initial_layer_print_height"])
            new = float(val)
            if new > cur:
                applied["initial_layer_print_height"] = val

    if "sparse_infill_density" in overrides:
        applied["sparse_infill_density"] = f"{int(overrides['sparse_infill_density'])}%"

    if "sparse_infill_pattern" in overrides:
        applied["sparse_infill_pattern"] = overrides["sparse_infill_pattern"]

    if "wall_loops" in overrides:
        applied["wall_loops"] = str(overrides["wall_loops"])

    if "top_shell_layers" in overrides:
        applied["top_shell_layers"] = str(overrides["top_shell_layers"])

    if "bottom_shell_layers" in overrides:
        applied["bottom_shell_layers"] = str(overrides["bottom_shell_layers"])

    if "brim_type" in overrides:
        applied["brim_type"] = overrides["brim_type"]

    if "support_type" in overrides:
        st = overrides["support_type"]
        if st == "none":
            applied["enable_support"] = "0"
        elif st == "normal":
            applied["enable_support"] = "1"
            applied["support_type"] = "normal(auto)"
        elif st == "tree":
            applied["enable_support"] = "1"
            applied["support_type"] = "tree(auto)"

    if not applied:
        return process_profile, set()

    logger.info("Applying %d parameter override(s): %s", len(applied), list(applied.keys()))
    return {**process_profile, **applied}, set(applied.keys())


def _trim_unused_filament_ids(
    filament_profile_ids: list[str],
    file_bytes: bytes,
    plate: int,
    file_type: str,
) -> list[str]:
    """Drop trailing filament slot ids the 3MF's plate doesn't reference.

    Multi-filament projects often carry `filament_settings_id` entries for slots
    the active plate never uses. When the client (or a middleware like
    bambu-gateway) backfills those slots with the 3MF's originals, they may
    reference filament profiles that don't exist in the target printer's
    profile set — resolution would then 400 even though the slice doesn't
    need them. Trimming to `max(used) + 1` keeps interior slot indices intact
    (the model may bind objects to specific slot indices) while dropping
    unused trailing entries that would only cause spurious failures.
    """
    if file_type != "3mf" or not filament_profile_ids:
        return filament_profile_ids
    used = get_used_filament_slots(file_bytes, plate=plate)
    if not used:
        return filament_profile_ids
    required_len = max(used) + 1
    if required_len >= len(filament_profile_ids):
        return filament_profile_ids
    dropped = filament_profile_ids[required_len:]
    logger.info(
        "Trimmed %d trailing filament slot id(s) not used by plate %d "
        "(kept %d; used slots %s; dropped ids %s)",
        len(dropped), plate, required_len, sorted(used), dropped,
    )
    return filament_profile_ids[:required_len]


@dataclass
class SliceContext:
    tmpdir: str
    cmd: list[str]
    settings_transfer: SettingsTransferResult
    original_thumbnails: dict[str, bytes]
    result_path: str
    num_filaments: int = 1


def _prepare_slice(
    file_bytes: bytes,
    machine_profile: dict[str, Any],
    process_profile: dict[str, Any],
    filament_profiles: list[dict[str, Any]],
    plate_type: str | None,
    tmpdir: str,
    process_overrides: dict[str, Any] | None = None,
    file_type: str = "3mf",
    plate: int = 1,
) -> SliceContext:
    """Prepare all inputs for slicing: extract settings, write temp files, build CLI command."""
    # Convert STL to 3MF if needed
    if file_type == "stl":
        from .stl_to_3mf import stl_to_3mf

        volume = get_build_volume(machine_profile)
        if volume:
            bed_cx, bed_cy = volume[0] / 2, volume[1] / 2
        else:
            bed_cx, bed_cy = 128.0, 128.0
        logger.info("Converting STL to 3MF (bed center: %.1f, %.1f)", bed_cx, bed_cy)
        file_bytes = stl_to_3mf(file_bytes, bed_cx, bed_cy)

    threemf_settings = {}
    original_thumbnails: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
            try:
                raw = zf.read("Metadata/project_settings.config").decode()
                threemf_settings = json.loads(raw)
                logger.debug("Loaded %d project settings from 3MF", len(threemf_settings))
            except (KeyError, json.JSONDecodeError):
                pass
            plate_str = str(plate)
            for name in zf.namelist():
                if name.endswith(".png") and (
                    f"plate_{plate_str}" in name
                    or f"top_{plate_str}." in name
                    or f"pick_{plate_str}." in name
                ):
                    original_thumbnails[name] = zf.read(name)
    except (zipfile.BadZipFile,) as exc:
        logger.debug("Could not read 3MF: %s", exc)
    if original_thumbnails:
        logger.debug("Extracted %d thumbnail(s) from original 3MF", len(original_thumbnails))

    plate_count = get_plate_count(file_bytes)
    if plate > plate_count:
        raise ModelTooBigError(
            f"Requested plate {plate} but file only has {plate_count} plate(s)"
        )
    if plate_count > 1:
        volume = get_build_volume(machine_profile)
        if volume:
            bed_cx, bed_cy = volume[0] / 2, volume[1] / 2
        else:
            bed_cx, bed_cy = 90.0, 90.0
        rebuilt = extract_plate(file_bytes, bed_cx, bed_cy, plate_id=str(plate))
        if rebuilt is not None:
            logger.info(
                "Rebuilt multi-plate 3MF (%d plates) into single-plate for plate %d",
                plate_count, plate,
            )
            file_bytes = rebuilt

    fit_check = validate_model_fits(file_bytes, machine_profile)
    if not fit_check.fits:
        raise ModelTooBigError(fit_check.error)

    input_path = os.path.join(tmpdir, "input.3mf")
    with open(input_path, "wb") as f:
        f.write(file_bytes)

    # Extract values before machine-key filter (these may be present in both
    # machine and process contexts and would otherwise be incorrectly excluded).
    declared_customizations = _extract_declared_customizations(threemf_settings)
    declared_machine_customizations = _extract_declared_machine_customizations(threemf_settings)
    raw_bed_type = threemf_settings.get("curr_bed_type")

    # Apply printer customizations from the 3MF before stripping machine keys
    # below — they're sourced from ``threemf_settings`` and disappear after the
    # filter. The machine profile for this slice is fixed by the request, so
    # there's no equivalent of the filament name guard.
    machine_transferred: list[dict[str, Any]] = []
    if declared_machine_customizations:
        machine_profile, machine_transferred = _overlay_3mf_machine_settings(
            machine_profile, threemf_settings, declared_machine_customizations,
        )
        if machine_transferred:
            logger.info(
                "Overlaid %d machine setting(s) from 3MF onto printer profile",
                len(machine_transferred),
            )

    threemf_settings = {
        k: v for k, v in threemf_settings.items()
        if k not in machine_profile or k in _PROFILE_META_KEYS
    }

    if threemf_settings:
        process_profile, overlaid_keys = _overlay_3mf_settings(
            process_profile, threemf_settings, declared_customizations,
        )
        if overlaid_keys:
            logger.info("Overlaid %d setting(s) from 3MF onto process profile", len(overlaid_keys))
            settings_transfer = SettingsTransferResult(
                status="applied",
                customized_keys=overlaid_keys,
            )
        else:
            settings_transfer = SettingsTransferResult(status="no_customizations")
    else:
        settings_transfer = SettingsTransferResult(status="no_3mf_settings")

    if machine_transferred:
        settings_transfer.machine_transferred = machine_transferred
        settings_transfer.machine_customized_keys = {
            e["key"] for e in machine_transferred
        }

    # `wipe_tower_x` / `wipe_tower_y` are stored in ``project_settings.config``
    # as plate-indexed vectors (one entry per plate) and read by OrcaSlicer
    # via ``get_at(plate_index)`` — see ``Print.cpp:984/2282/2439/2740``,
    # ``PartPlate.cpp:2129``. They're NOT listed in the smart-overlay
    # fingerprint (``different_settings_to_system``), so the overlay above
    # misses them. After ``extract_plate`` collapses the project to one plate,
    # the slicer reads index 0 and falls back to OrcaSlicer's C++ defaults
    # (``{15., 220.}`` from ``PrintConfig.cpp``) — which lands the prime
    # tower 40 mm above the back edge on a 180×180 A1 mini bed and trips
    # ``CLI_GCODE_PATH_IN_UNPRINTABLE_AREA`` on any multi-filament slice.
    plate_idx = max(0, plate - 1)
    plate_indexed_overlaid: list[dict[str, Any]] = []
    for key in ("wipe_tower_x", "wipe_tower_y"):
        src = threemf_settings.get(key)
        if not isinstance(src, list) or not src:
            continue
        idx = plate_idx if plate_idx < len(src) else 0
        new_val = [src[idx]]
        original = process_profile.get(key)
        if original == new_val:
            continue
        process_profile[key] = new_val
        plate_indexed_overlaid.append(
            {"key": key, "value": new_val, "original": original},
        )
    if plate_indexed_overlaid:
        logger.info(
            "Transferred %d plate-indexed setting(s) from 3MF (plate %d): %s",
            len(plate_indexed_overlaid), plate,
            ", ".join(e["key"] for e in plate_indexed_overlaid),
        )
        settings_transfer.customized_keys |= {
            e["key"] for e in plate_indexed_overlaid
        }

    # Project-level flush settings (``s_project_options`` in
    # ``PresetBundle.cpp:37-52``) live in ``project_settings.config`` but are
    # NOT listed in ``different_settings_to_system`` either, so the smart
    # overlay above misses them too. The CLI defaults (``flush_multiplier =
    # 0.3``, uniform 108-mm³ matrix) under-flush badly compared to the
    # filament-pair-specific values the GUI authored — for a real
    # multi-color print this means the next color bleeds the previous one.
    # Lift the source's values straight onto the process profile.
    project_overlaid: list[dict[str, Any]] = []
    for key in (
        "flush_multiplier", "flush_volumes_vector", "flush_volumes_matrix",
    ):
        if key not in threemf_settings:
            continue
        new_val = threemf_settings[key]
        original = process_profile.get(key)
        if original == new_val:
            continue
        process_profile[key] = new_val
        project_overlaid.append(
            {"key": key, "value": new_val, "original": original},
        )
    if project_overlaid:
        logger.info(
            "Transferred %d project-level setting(s) from 3MF: %s",
            len(project_overlaid),
            ", ".join(e["key"] for e in project_overlaid),
        )
        settings_transfer.customized_keys |= {
            e["key"] for e in project_overlaid
        }

    # Request value takes priority; otherwise preserve 3MF-selected bed type.
    effective_plate_type = plate_type
    if not effective_plate_type:
        if isinstance(raw_bed_type, str) and raw_bed_type:
            effective_plate_type = raw_bed_type
    if effective_plate_type:
        process_profile["curr_bed_type"] = effective_plate_type

    for key, min_val in _CLAMP_RULES.items():
        if key in process_profile:
            val = process_profile[key]
            try:
                num = float(val) if isinstance(val, str) else val
                if num < min_val:
                    process_profile[key] = str(min_val) if isinstance(val, str) else min_val
            except (ValueError, TypeError):
                pass

    # Apply explicit API parameter overrides (highest priority — after settings
    # transfer and clamp rules so they always win).
    if process_overrides:
        process_profile, override_keys = _apply_parameter_overrides(process_profile, process_overrides)
        settings_transfer.customized_keys |= override_keys

    slice_filepath = _sanitize_3mf(
        input_path, tmpdir, machine_profile, len(filament_profiles),
    )
    if slice_filepath != input_path:
        logger.debug("3MF was sanitized")

    # Per-filament customization transfer: apply the 3MF's declared per-slot
    # customizations onto the loaded filament profile when the selected filament
    # matches the 3MF's original for that slot; otherwise report discarded keys.
    filament_allowlists = _extract_declared_filament_customizations(threemf_settings)
    original_filament_ids = threemf_settings.get("filament_settings_id") or []
    if not isinstance(original_filament_ids, list):
        original_filament_ids = []

    for slot_idx, fp in enumerate(filament_profiles):
        allowed_keys = (
            filament_allowlists[slot_idx]
            if slot_idx < len(filament_allowlists)
            else set()
        )
        original_name = ""
        if slot_idx < len(original_filament_ids):
            raw = original_filament_ids[slot_idx]
            if isinstance(raw, str):
                original_name = raw.strip()
        selected_name = str(fp.get("name", "")).strip()

        if not allowed_keys:
            continue

        same_filament = bool(original_name) and original_name == selected_name
        if same_filament:
            updated_fp, transferred = _overlay_3mf_filament_settings(
                fp, threemf_settings, slot_idx, allowed_keys,
            )
            if transferred:
                filament_profiles[slot_idx] = updated_fp
                settings_transfer.filaments.append(FilamentTransferEntry(
                    slot=slot_idx,
                    original_filament=original_name,
                    selected_filament=selected_name,
                    status="applied",
                    transferred=transferred,
                ))
                settings_transfer.filament_customized_keys[slot_idx] = {
                    e["key"] for e in transferred
                }
                logger.info(
                    "Filament slot %d: applied %d customization(s) from 3MF",
                    slot_idx, len(transferred),
                )
        else:
            settings_transfer.filaments.append(FilamentTransferEntry(
                slot=slot_idx,
                original_filament=original_name,
                selected_filament=selected_name,
                status="filament_changed",
                discarded=sorted(allowed_keys),
            ))
            logger.info(
                "Filament slot %d: discarded %d customization(s) — filament "
                "changed from %r to %r",
                slot_idx, len(allowed_keys), original_name, selected_name,
            )

    # Resize per-filament vector keys to match the number of loaded filaments.
    # Replicates `Preset::normalize` — the orca-slicer CLI does not run it after
    # combining --load-settings with --load-filaments, so keys absent from vendor
    # filament JSONs stay at length 1 and silently fall back to slot-0 values
    # for slot 1+ on multi-filament prints.
    process_profile = normalize_process_profile(process_profile, len(filament_profiles))

    machine_path = os.path.join(tmpdir, "machine.json")
    process_path = os.path.join(tmpdir, "process.json")
    with open(machine_path, "w") as f:
        json.dump(machine_profile, f, indent=2)
    with open(process_path, "w") as f:
        json.dump(process_profile, f, indent=2)

    filament_paths = []
    for i, fp in enumerate(filament_profiles):
        path = os.path.join(tmpdir, f"filament_{i}.json")
        with open(path, "w") as f:
            json.dump(fp, f, indent=2)
        filament_paths.append(path)

    settings_arg = f"{machine_path};{process_path}"
    result_path = os.path.join(tmpdir, "result.3mf")
    cmd = [
        ORCA_BINARY,
        # `--debug 4` keeps the `default_status_callback` lines that carry
        # `message_type=2` (critical) warnings so we can surface them to the
        # caller on failure, without the per-layer trace noise of level 5.
        "--debug", "4",
        "--load-settings", settings_arg,
    ]
    if filament_paths:
        cmd += ["--load-filaments", ";".join(filament_paths)]
    if fit_check.needs_arrange:
        logger.info("Cross-printer detected: adding --arrange 1")
        cmd += ["--arrange", "1"]
    cmd += [
        "--slice", "1",
        "--export-3mf", "result.3mf",
        "--allow-newer-file",
        # Match the GUI's slice-export strategy. The GUI sets
        # ``SaveStrategy::SkipModel`` unconditionally on every slice export
        # (Plater.cpp 14624 / 15828 / 15856), which omits the input mesh
        # from the output 3MF — only gcode + metadata + thumbnails get
        # written. The CLI's default keeps the full mesh, bloating the
        # output by the original file size (~13 MB on a typical multi-part
        # project). ``--min-save`` flips the same bit (OrcaSlicer.cpp
        # 7242 → ``store_params.strategy | SaveStrategy::SkipModel``).
        # `min_save` is a `coBool` and the CLI parser at
        # ``Config.cpp:1656`` deliberately does NOT consume the next token
        # for bool options — passing ``"--min-save", "1"`` would leak the
        # ``"1"`` into ``m_input_files`` and abort with CLI_FILE_NOTFOUND
        # before the real input is even loaded.
        "--min-save",
        "--outputdir", tmpdir,
        os.path.abspath(slice_filepath),
    ]

    return SliceContext(
        tmpdir=tmpdir,
        cmd=cmd,
        settings_transfer=settings_transfer,
        original_thumbnails=original_thumbnails,
        result_path=result_path,
        num_filaments=len(filament_profiles),
    )


def _patch_output_settings(
    result_path: str,
    customized_keys: set[str],
    filament_customized_keys: dict[int, set[str]] | None = None,
    machine_customized_keys: set[str] | None = None,
    num_filaments: int = 1,
) -> None:
    """Ensure transferred keys appear in the output 3MF's different_settings_to_system.

    OrcaSlicer CLI computes different_settings_to_system against the loaded profile
    (which already has customizations baked in), so transferred keys are missing.
    The GUI then falls back to system-profile defaults for those keys.  Patching
    the field after slicing ensures the user's customizations are visible when
    reopening the file in OrcaSlicer.

    The field is laid out as ``[process, filament_0, ..., filament_{N-1}, printer]``
    (see ``PresetBundle::load_3mf_*`` in OrcaSlicer source). ``customized_keys``
    patches slot 0 (process); ``filament_customized_keys`` maps slot ``i`` →
    index ``i + 1``; ``machine_customized_keys`` patches the trailing
    printer slot at index ``num_filaments + 1``.
    """
    filament_customized_keys = filament_customized_keys or {}
    machine_customized_keys = machine_customized_keys or set()
    if not customized_keys and not filament_customized_keys and not machine_customized_keys:
        return

    settings_file = "Metadata/project_settings.config"

    with zipfile.ZipFile(result_path, "r") as zf:
        if settings_file not in zf.namelist():
            return
        raw = zf.read(settings_file).decode()

    settings = json.loads(raw)
    diff_to_system = settings.get("different_settings_to_system", [])
    if not isinstance(diff_to_system, list):
        diff_to_system = []

    # Pad to the canonical OrcaSlicer width so every slot has a stable index.
    required_len = max(
        num_filaments + 2,
        len(diff_to_system),
        max((1 + s for s in filament_customized_keys), default=0) + 1,
    )
    while len(diff_to_system) < required_len:
        diff_to_system.append("")
    machine_idx = len(diff_to_system) - 1

    changed = False

    if customized_keys:
        existing = (
            {k.strip() for k in diff_to_system[0].split(";") if k.strip()}
            if isinstance(diff_to_system[0], str)
            else set()
        )
        merged = existing | customized_keys
        if merged != existing:
            diff_to_system[0] = ";".join(sorted(merged))
            changed = True

    for slot_idx, keys in filament_customized_keys.items():
        if not keys:
            continue
        target_idx = slot_idx + 1
        if target_idx >= machine_idx:
            # Defensive: a stale slot index that would collide with the printer
            # slot would silently corrupt the layout. Skip it.
            continue
        existing_entry = diff_to_system[target_idx]
        existing = (
            {k.strip() for k in existing_entry.split(";") if k.strip()}
            if isinstance(existing_entry, str)
            else set()
        )
        merged = existing | keys
        if merged != existing:
            diff_to_system[target_idx] = ";".join(sorted(merged))
            changed = True

    if machine_customized_keys:
        existing_entry = diff_to_system[machine_idx]
        existing = (
            {k.strip() for k in existing_entry.split(";") if k.strip()}
            if isinstance(existing_entry, str)
            else set()
        )
        merged = existing | machine_customized_keys
        if merged != existing:
            diff_to_system[machine_idx] = ";".join(sorted(merged))
            changed = True

    if not changed:
        return

    settings["different_settings_to_system"] = diff_to_system

    tmp_path = result_path + ".tmp"
    with zipfile.ZipFile(result_path, "r") as zf_in:
        with zipfile.ZipFile(tmp_path, "w") as zf_out:
            for item in zf_in.infolist():
                if item.filename == settings_file:
                    zf_out.writestr(item.filename, json.dumps(settings, indent=2))
                else:
                    zf_out.writestr(item, zf_in.read(item.filename))
    os.replace(tmp_path, result_path)
    logger.debug("Patched different_settings_to_system")


def _post_process(ctx: SliceContext, orca_output: str | None = None) -> bytes:
    """Read the sliced result and inject thumbnails."""
    if not os.path.isfile(ctx.result_path):
        logger.error("Output file not found at %s", ctx.result_path)
        raise SlicingError(
            "OrcaSlicer did not produce output file",
            orca_output=orca_output,
        )

    try:
        _patch_output_settings(
            ctx.result_path,
            ctx.settings_transfer.customized_keys,
            ctx.settings_transfer.filament_customized_keys,
            machine_customized_keys=ctx.settings_transfer.machine_customized_keys,
            num_filaments=ctx.num_filaments,
        )
    except Exception:
        logger.warning("Failed to patch different_settings_to_system; continuing", exc_info=True)

    if ctx.original_thumbnails:
        with zipfile.ZipFile(ctx.result_path, "r") as zf:
            existing = set(zf.namelist())
        missing = {k: v for k, v in ctx.original_thumbnails.items() if k not in existing}
        if missing:
            with zipfile.ZipFile(ctx.result_path, "a") as zf:
                for name, data in missing.items():
                    zf.writestr(name, data)
            logger.info("Injected %d thumbnail(s) into output 3MF", len(missing))

    result_size = os.path.getsize(ctx.result_path)
    logger.info("Sliced output: %s (%d bytes)", ctx.result_path, result_size)
    with open(ctx.result_path, "rb") as f:
        return f.read()


async def slice_3mf(
    file_bytes: bytes,
    machine_profile_id: str,
    process_profile_id: str,
    filament_profile_ids: list[str],
    plate_type: str | None = None,
    process_overrides: dict[str, Any] | None = None,
    file_type: str = "3mf",
    plate: int = 1,
) -> tuple[bytes, SettingsTransferResult]:
    """Slice a 3MF or STL file and return the sliced result as bytes + transfer info."""
    logger.info(
        "Slice request: machine=%s process=%s filaments=%s file_size=%d overrides=%s file_type=%s plate=%d",
        machine_profile_id, process_profile_id, filament_profile_ids, len(file_bytes),
        list(process_overrides.keys()) if process_overrides else None, file_type, plate,
    )

    filament_profile_ids = _trim_unused_filament_ids(
        filament_profile_ids, file_bytes, plate, file_type
    )

    # Resolve profiles
    machine_profile = get_profile("machine", machine_profile_id)
    process_profile = get_profile("process", process_profile_id)
    filament_profiles = [
        get_profile_by_id_or_name("filament", fid) for fid in filament_profile_ids
    ]
    logger.info(
        "Resolved profiles: machine=%s process=%s filaments=%s",
        machine_profile.get("name"), process_profile.get("name"),
        [fp.get("name") for fp in filament_profiles],
    )

    async with _slice_semaphore:
        return await _do_slice(
            file_bytes, machine_profile, process_profile, filament_profiles,
            plate_type, process_overrides, file_type, plate,
        )


async def _do_slice(
    file_bytes: bytes,
    machine_profile: dict[str, Any],
    process_profile: dict[str, Any],
    filament_profiles: list[dict[str, Any]],
    plate_type: str | None,
    process_overrides: dict[str, Any] | None = None,
    file_type: str = "3mf",
    plate: int = 1,
) -> tuple[bytes, SettingsTransferResult]:
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _prepare_slice(
            file_bytes, machine_profile, process_profile, filament_profiles,
            plate_type, tmpdir, process_overrides, file_type, plate,
        )

        # Run OrcaSlicer
        logger.info("Running: %s", " ".join(ctx.cmd))
        proc = await asyncio.create_subprocess_exec(
            *ctx.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stdout_text = stdout.decode(errors="replace").strip()
        stderr_text = stderr.decode(errors="replace").strip()
        orca_output = (stdout_text + "\n" + stderr_text).strip()

        if proc.returncode != 0:
            err = _build_failure(proc.returncode, tmpdir, orca_output)
            logger.error(
                "OrcaSlicer failed (code %d): %s\nstdout: %s\nstderr: %s",
                proc.returncode, err, stdout_text, stderr_text,
            )
            raise err

        logger.info("OrcaSlicer finished successfully")
        logger.debug("OrcaSlicer output: %s", orca_output)

        result_bytes = _post_process(ctx, orca_output=orca_output)
        return result_bytes, ctx.settings_transfer


async def slice_3mf_streaming(
    file_bytes: bytes,
    machine_profile_id: str,
    process_profile_id: str,
    filament_profile_ids: list[str],
    plate_type: str | None = None,
    process_overrides: dict[str, Any] | None = None,
    file_type: str = "3mf",
    plate: int = 1,
):
    """Resolve profiles and return an SSE async generator for streaming slicing.

    Profile resolution happens before the generator is created, so
    ProfileNotFoundError is raised synchronously (caught by FastAPI → HTTP 400).
    """
    filament_profile_ids = _trim_unused_filament_ids(
        filament_profile_ids, file_bytes, plate, file_type
    )
    machine_profile = get_profile("machine", machine_profile_id)
    process_profile = get_profile("process", process_profile_id)
    filament_profiles = [
        get_profile_by_id_or_name("filament", fid) for fid in filament_profile_ids
    ]

    async def _generate():
        tmpdir = tempfile.mkdtemp()
        try:
            yield _sse_event("status", {"phase": "reading_3mf", "message": "Reading input file"})
            ctx = _prepare_slice(
                file_bytes, machine_profile, process_profile, filament_profiles,
                plate_type, tmpdir, process_overrides, file_type, plate,
            )

            yield _sse_event("status", {"phase": "slicing", "message": "Starting OrcaSlicer"})

            async with _slice_semaphore:
                logger.info("Running: %s", " ".join(ctx.cmd))
                proc = await asyncio.create_subprocess_exec(
                    *ctx.cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                output_lines = []
                current_phase = None
                async for raw_line in proc.stdout:
                    line = raw_line.decode(errors="replace").rstrip()
                    if line:
                        output_lines.append(line)
                        phase = _detect_orca_phase(line)
                        if phase and phase[0] != current_phase:
                            current_phase = phase[0]
                            yield _sse_event("status", {"phase": current_phase, "message": phase[1]})
                        percent = _parse_progress_percent(line)
                        yield _sse_event("progress", {"line": line, "percent": percent})

                await proc.wait()
                orca_output = "\n".join(output_lines)

                if proc.returncode != 0:
                    err = _build_failure(proc.returncode, tmpdir, orca_output)
                    logger.error("OrcaSlicer failed (code %d): %s\n%s", proc.returncode, err, orca_output)
                    yield _sse_event("error", {
                        "error": str(err),
                        "orca_output": err.orca_output,
                        "critical_warnings": err.critical_warnings,
                    })
                    return

                logger.info("OrcaSlicer finished successfully")

                yield _sse_event("status", {"phase": "packaging", "message": "Packaging result"})

                try:
                    result_bytes = _post_process(ctx, orca_output=orca_output)
                except SlicingError as e:
                    yield _sse_event("error", {
                        "error": str(e),
                        "orca_output": e.orca_output,
                        "critical_warnings": e.critical_warnings,
                    })
                    return

                result_b64 = base64.b64encode(result_bytes).decode()
                transfer_info: dict[str, Any] = {"status": ctx.settings_transfer.status}
                if ctx.settings_transfer.status == "applied" and ctx.settings_transfer.transferred:
                    transfer_info["transferred"] = ctx.settings_transfer.transferred
                if ctx.settings_transfer.filaments:
                    transfer_info["filaments"] = [
                        asdict(f) for f in ctx.settings_transfer.filaments
                    ]
                if ctx.settings_transfer.machine_transferred:
                    transfer_info["machine_transferred"] = (
                        ctx.settings_transfer.machine_transferred
                    )

                yield _sse_event("result", {
                    "file_base64": result_b64,
                    "file_size": len(result_bytes),
                    "settings_transfer": transfer_info,
                })
        except Exception as e:
            yield _sse_event("error", {"error": str(e), "orca_output": None})
        finally:
            try:
                yield _sse_event("done", {})
            except GeneratorExit:
                pass
            shutil.rmtree(tmpdir, ignore_errors=True)

    return _generate()
