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
from dataclasses import dataclass, field
from typing import Any

from .config import ORCA_BINARY
from .profiles import ProfileNotFoundError, get_profile, resolve_profile_by_name
from .threemf import extract_first_plate, get_build_volume, get_plate_count, validate_model_fits

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
class SettingsTransferResult:
    status: str  # "applied", "no_original_profile", "no_customizations", "no_3mf_settings"
    transferred: list[dict[str, str]] = field(default_factory=list)
    customized_keys: set[str] = field(default_factory=set)


def _normalize_for_comparison(value: Any) -> str:
    """Normalize a value for comparison: float precision, type coercion."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        try:
            f = float(value)
            if f == int(f):
                return str(int(f))
            return f"{f:.10g}"
        except (ValueError, OverflowError):
            return str(value)
    if isinstance(value, str):
        try:
            f = float(value)
            if f == int(f):
                return str(int(f))
            return f"{f:.10g}"
        except ValueError:
            return value
    if isinstance(value, list):
        return json.dumps([_normalize_for_comparison(v) for v in value], separators=(",", ":"))
    return str(value)


def _diff_3mf_settings(
    threemf_settings: dict[str, Any], original_profile: dict[str, Any],
) -> dict[str, tuple[Any, Any]]:
    """Compare 3MF settings against the resolved original profile.

    Returns dict of {key: (threemf_val, original_val)} for settings that differ.
    """
    diffs: dict[str, tuple[Any, Any]] = {}
    for key, tv in threemf_settings.items():
        if key in _PROFILE_META_KEYS or not _is_transferable_process_key(key):
            continue
        if key not in original_profile:
            # Setting exists in 3MF but not in original profile — treat as customization
            diffs[key] = (tv, None)
            continue
        ov = original_profile[key]
        if _normalize_for_comparison(tv) != _normalize_for_comparison(ov):
            diffs[key] = (tv, ov)
    return diffs


def _apply_customizations(
    process_profile: dict[str, Any],
    threemf_settings: dict[str, Any],
    customized_keys: set[str],
) -> tuple[dict[str, Any], set[str]]:
    """Apply only the customized keys from 3MF settings onto the process profile.

    Returns (updated_profile, actually_applied_keys).
    """
    overrides = {}
    for k in customized_keys:
        if (
            k not in threemf_settings
            or k not in process_profile
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


class ModelTooBigError(Exception):
    pass


class SlicingError(Exception):
    def __init__(self, message: str, orca_output: str | None = None):
        super().__init__(message)
        self.orca_output = orca_output


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


def _sanitize_3mf(filepath: str, tmpdir: str) -> str:
    """Fix invalid parameter values in a 3MF's project_settings."""
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
    process_profile: dict[str, Any], threemf_settings: dict[str, Any],
) -> dict[str, Any]:
    """Overlay 3MF project settings onto process profile to preserve user choices."""
    overrides = {}
    for k in process_profile:
        if (
            k not in threemf_settings
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
        return {**process_profile, **overrides}
    return process_profile


def _smart_settings_transfer(
    process_profile: dict[str, Any],
    threemf_settings: dict[str, Any],
    declared_customized_keys: set[str] | None = None,
) -> tuple[dict[str, Any], SettingsTransferResult]:
    """Transfer only user-customized settings from 3MF onto the process profile.

    Falls back to full overlay when we can't determine the original profile.
    ``declared_customized_keys`` are process keys from the 3MF's
    ``different_settings_to_system`` — they supplement the diff-based detection.
    """
    if not threemf_settings:
        logger.debug("No 3MF settings to transfer")
        return process_profile, SettingsTransferResult(status="no_3mf_settings")

    # Filter declared keys to only those that are actually transferable and
    # present in both the 3MF settings and the target process profile.
    safe_declared = {
        k for k in (declared_customized_keys or set())
        if k in threemf_settings
        and k in process_profile
        and k not in _PROFILE_META_KEYS
        and _is_transferable_process_key(k)
    }

    print_settings_id = threemf_settings.get("print_settings_id")
    if not print_settings_id:
        logger.debug("No print_settings_id in 3MF, falling back to full overlay")
        return (
            _overlay_3mf_settings(process_profile, threemf_settings),
            SettingsTransferResult(
                status="no_3mf_settings",
                customized_keys=safe_declared,
            ),
        )

    original_profile = resolve_profile_by_name(print_settings_id)
    if original_profile is None:
        logger.debug(
            "Original profile %r not found, falling back to full overlay",
            print_settings_id,
        )
        return (
            _overlay_3mf_settings(process_profile, threemf_settings),
            SettingsTransferResult(
                status="no_original_profile",
                customized_keys=safe_declared,
            ),
        )

    diffs = _diff_3mf_settings(threemf_settings, original_profile)

    # Union diff-detected keys with explicitly declared customizations
    all_customized = set(diffs.keys()) | safe_declared

    if not all_customized:
        logger.info("No customizations detected in 3MF vs original profile %r", print_settings_id)
        return process_profile, SettingsTransferResult(status="no_customizations")

    updated, applied_keys = _apply_customizations(process_profile, threemf_settings, all_customized)
    if not applied_keys:
        logger.info(
            "Detected %d customization(s) in 3MF vs %r but none apply to the target profile",
            len(all_customized), print_settings_id,
        )
        return process_profile, SettingsTransferResult(status="no_customizations")

    diff_count = len(diffs)
    declared_count = len(declared_customized_keys or set())
    logger.info(
        "Applied %d customization(s) from 3MF (%d detected, %d declared) vs original profile %r: %s",
        len(applied_keys), diff_count, declared_count, print_settings_id, list(applied_keys),
    )
    transferred = [
        {"key": k, "value": json.dumps(tv), "original": json.dumps(ov)}
        for k, (tv, ov) in diffs.items()
        if k in applied_keys
    ]
    # Include declared-only keys (not caught by diff) that were successfully applied
    for k in applied_keys:
        if k not in diffs:
            tv = threemf_settings.get(k)
            ov = original_profile.get(k)
            transferred.append({
                "key": k, "value": json.dumps(tv), "original": json.dumps(ov),
            })
    return updated, SettingsTransferResult(
        status="applied", transferred=transferred, customized_keys=applied_keys,
    )


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
            for name in zf.namelist():
                if name.endswith(".png") and (
                    "plate_1" in name or "top_1." in name or "pick_1." in name
                ):
                    original_thumbnails[name] = zf.read(name)
    except (zipfile.BadZipFile,) as exc:
        logger.debug("Could not read 3MF: %s", exc)
    if original_thumbnails:
        logger.debug("Extracted %d thumbnail(s) from original 3MF", len(original_thumbnails))

    plate_count = get_plate_count(file_bytes)
    if plate_count > 1:
        volume = get_build_volume(machine_profile)
        if volume:
            bed_cx, bed_cy = volume[0] / 2, volume[1] / 2
        else:
            bed_cx, bed_cy = 90.0, 90.0
        rebuilt = extract_first_plate(file_bytes, bed_cx, bed_cy)
        if rebuilt is not None:
            logger.info(
                "Rebuilt multi-plate 3MF (%d plates) into single-plate for plate 1",
                plate_count,
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

    transfer_result = _smart_settings_transfer(
        process_profile, threemf_settings, declared_customizations,
    )
    process_profile = transfer_result[0]
    settings_transfer = transfer_result[1]

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

    slice_filepath = _sanitize_3mf(input_path, tmpdir)
    if slice_filepath != input_path:
        logger.debug("3MF was sanitized")

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


def _patch_output_settings(result_path: str, customized_keys: set[str]) -> None:
    """Ensure transferred keys appear in the output 3MF's different_settings_to_system.

    OrcaSlicer CLI computes different_settings_to_system against the loaded profile
    (which already has customizations baked in), so transferred keys are missing.
    The GUI then falls back to system-profile defaults for those keys.  Patching
    the field after slicing ensures the user's customizations are visible when
    reopening the file in OrcaSlicer.
    """
    if not customized_keys:
        return

    settings_file = "Metadata/project_settings.config"

    with zipfile.ZipFile(result_path, "r") as zf:
        if settings_file not in zf.namelist():
            return
        raw = zf.read(settings_file).decode()

    settings = json.loads(raw)
    diff_to_system = settings.get("different_settings_to_system", [])
    if not isinstance(diff_to_system, list) or not diff_to_system:
        diff_to_system = [""]

    existing = {k.strip() for k in diff_to_system[0].split(";") if k.strip()} \
        if isinstance(diff_to_system[0], str) else set()
    merged = existing | customized_keys
    if merged == existing:
        return  # nothing to add

    diff_to_system[0] = ";".join(sorted(merged))
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
    logger.debug(
        "Patched different_settings_to_system with %d key(s): %s",
        len(merged - existing), sorted(merged - existing),
    )


def _post_process(ctx: SliceContext, orca_output: str | None = None) -> bytes:
    """Read the sliced result and inject thumbnails."""
    if not os.path.isfile(ctx.result_path):
        logger.error("Output file not found at %s", ctx.result_path)
        raise SlicingError(
            "OrcaSlicer did not produce output file",
            orca_output=orca_output,
        )

    try:
        _patch_output_settings(ctx.result_path, ctx.settings_transfer.customized_keys)
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
) -> tuple[bytes, SettingsTransferResult]:
    """Slice a 3MF or STL file and return the sliced result as bytes + transfer info."""
    logger.info(
        "Slice request: machine=%s process=%s filaments=%s file_size=%d overrides=%s file_type=%s",
        machine_profile_id, process_profile_id, filament_profile_ids, len(file_bytes),
        list(process_overrides.keys()) if process_overrides else None, file_type,
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
            plate_type, process_overrides, file_type,
        )


async def _do_slice(
    file_bytes: bytes,
    machine_profile: dict[str, Any],
    process_profile: dict[str, Any],
    filament_profiles: list[dict[str, Any]],
    plate_type: str | None,
    process_overrides: dict[str, Any] | None = None,
    file_type: str = "3mf",
) -> tuple[bytes, SettingsTransferResult]:
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _prepare_slice(
            file_bytes, machine_profile, process_profile, filament_profiles,
            plate_type, tmpdir, process_overrides, file_type,
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
            logger.error(
                "OrcaSlicer failed (code %d)\nstdout: %s\nstderr: %s",
                proc.returncode, stdout_text, stderr_text,
            )
            raise SlicingError(
                f"OrcaSlicer exited with code {proc.returncode}",
                orca_output=orca_output,
            )

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
):
    """Resolve profiles and return an SSE async generator for streaming slicing.

    Profile resolution happens before the generator is created, so
    ProfileNotFoundError is raised synchronously (caught by FastAPI → HTTP 400).
    """
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
                plate_type, tmpdir, process_overrides, file_type,
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
                    logger.error("OrcaSlicer failed (code %d)\n%s", proc.returncode, orca_output)
                    yield _sse_event("error", {
                        "error": f"OrcaSlicer exited with code {proc.returncode}",
                        "orca_output": orca_output,
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
                    })
                    return

                result_b64 = base64.b64encode(result_bytes).decode()
                transfer_info = {"status": ctx.settings_transfer.status}
                if ctx.settings_transfer.status == "applied" and ctx.settings_transfer.transferred:
                    transfer_info["transferred"] = ctx.settings_transfer.transferred

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
