"""Slicing logic adapted from bambu-poc/print_3mf.py."""

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import ORCA_BINARY
from .normalize import normalize_process_profile
from .profiles import ProfileNotFoundError, get_profile
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
        message = f"OrcaSlicer exited with code {returncode}"
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
    """Per-filament customization allowlists from `different_settings_to_system[2+]`.

    Slot `i` in the returned list corresponds to filament slot `i`
    (= `different_settings_to_system[i + 2]`). Returns an empty list when the
    fingerprint is missing or has no filament slots.
    """
    diff_to_system = threemf_settings.get("different_settings_to_system")
    if not isinstance(diff_to_system, list) or len(diff_to_system) <= 2:
        return []
    result: list[set[str]] = []
    for entry in diff_to_system[2:]:
        if isinstance(entry, str) and entry.strip():
            result.append({k.strip() for k in entry.split(";") if k.strip()})
        else:
            result.append(set())
    return result


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


def _sanitize_3mf(
    filepath: str,
    tmpdir: str,
    machine_profile: dict[str, Any] | None = None,
) -> str:
    """Fix invalid parameter values and rebrand printer identity in a 3MF.

    When the 3MF was authored for a different printer than the target
    `machine_profile`, OrcaSlicer CLI takes a "foreign vendor" path
    (`_load_model_from_file: found 3mf from other vendor, split as instance`)
    that auto-arranges the model and can spuriously flag repositioned objects
    as having floating regions — blocking the slice even when the same model
    + target profile combination succeeds in the GUI. The GUI avoids this by
    rewriting `printer_model` / `printer_settings_id` in memory when the user
    changes printer. We mirror that behavior here so CLI slices match GUI
    behavior for cross-printer 3MFs.
    """
    settings_file = "Metadata/project_settings.config"
    with zipfile.ZipFile(filepath, "r") as zf:
        if settings_file not in zf.namelist():
            return filepath

        raw = zf.read(settings_file).decode()
        settings = json.loads(raw)

        changed = False

        # Clamp values that must meet minimums
        for key, min_val in _CLAMP_RULES.items():
            if key in settings:
                val = settings[key]
                try:
                    num = float(val) if isinstance(val, str) else val
                    if num < min_val:
                        settings[key] = str(min_val) if isinstance(val, str) else min_val
                        changed = True
                except (ValueError, TypeError):
                    pass

        # Rebrand printer identity to match target, to avoid OrcaSlicer's
        # foreign-vendor path. The values come from the machine profile's own
        # `printer_model` and `name` (the profile name doubles as the
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
                changed = True
            if target_settings_id and settings.get("printer_settings_id") != target_settings_id:
                logger.info(
                    "Rebranding 3MF printer_settings_id: %r -> %r",
                    settings.get("printer_settings_id"), target_settings_id,
                )
                settings["printer_settings_id"] = target_settings_id
                changed = True

        if not changed:
            return filepath

        sanitized = os.path.join(tmpdir, "sanitized.3mf")
        with zipfile.ZipFile(sanitized, "w") as zf_out:
            with zipfile.ZipFile(filepath, "r") as zf_in:
                for item in zf_in.infolist():
                    if item.filename == settings_file:
                        # Use filename instead of ZipInfo to avoid stale size metadata
                        zf_out.writestr(item.filename, json.dumps(settings, indent=2))
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

    Returns (updated_profile, set_of_overlaid_keys).
    """
    overrides = {}
    for k in allowed_keys:
        if (
            k not in process_profile
            or k not in threemf_settings
            or k in _PROFILE_META_KEYS
            or not _is_transferable_process_key(k)
        ):
            continue
        pv = process_profile[k]
        tv = threemf_settings[k]
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
    raw_bed_type = threemf_settings.get("curr_bed_type")

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

    slice_filepath = _sanitize_3mf(input_path, tmpdir, machine_profile)
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
        "--outputdir", tmpdir,
        os.path.abspath(slice_filepath),
    ]

    return SliceContext(
        tmpdir=tmpdir,
        cmd=cmd,
        settings_transfer=settings_transfer,
        original_thumbnails=original_thumbnails,
        result_path=result_path,
    )


def _patch_output_settings(
    result_path: str,
    customized_keys: set[str],
    filament_customized_keys: dict[int, set[str]] | None = None,
) -> None:
    """Ensure transferred keys appear in the output 3MF's different_settings_to_system.

    OrcaSlicer CLI computes different_settings_to_system against the loaded profile
    (which already has customizations baked in), so transferred keys are missing.
    The GUI then falls back to system-profile defaults for those keys.  Patching
    the field after slicing ensures the user's customizations are visible when
    reopening the file in OrcaSlicer.

    `customized_keys` patches slot 0 (process). `filament_customized_keys` maps
    filament slot index to per-filament customization keys; each mapped slot is
    patched into `different_settings_to_system[slot + 2]`.
    """
    filament_customized_keys = filament_customized_keys or {}
    if not customized_keys and not filament_customized_keys:
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

    required_len = max(
        [1]
        + ([2 + s for s in filament_customized_keys] if filament_customized_keys else []),
    )
    while len(diff_to_system) < required_len:
        diff_to_system.append("")

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
        target_idx = slot_idx + 2
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
        get_profile("filament", fid) for fid in filament_profile_ids
    ]
    logger.info(
        "Resolved profiles: machine=%s process=%s filaments=%s",
        machine_profile.get("name"), process_profile.get("name"),
        [fp.get("name") for fp in filament_profiles],
    )

    # OrcaSlicer enforces that G92 E0 exists in layer_change_gcode for printers
    # using relative extrusion (all Bambu Lab printers). Without it, the CLI
    # refuses to slice. The bundled profiles have it in the GUI, but when loaded
    # via --load-settings the resolved JSON may not include it due to inheritance
    # chain issues. Safe to inject unconditionally — it only resets the extruder
    # position to prevent float precision drift on long prints.
    lcg = machine_profile.get("layer_change_gcode", "")
    if "G92 E0" not in lcg:
        logger.debug("Injecting G92 E0 into layer_change_gcode")
        machine_profile = {**machine_profile, "layer_change_gcode": "G92 E0\n" + lcg}

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
        get_profile("filament", fid) for fid in filament_profile_ids
    ]

    lcg = machine_profile.get("layer_change_gcode", "")
    if "G92 E0" not in lcg:
        machine_profile = {**machine_profile, "layer_change_gcode": "G92 E0\n" + lcg}

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
