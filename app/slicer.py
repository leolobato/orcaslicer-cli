"""Slicing logic adapted from bambu-poc/print_3mf.py."""

import asyncio
import io
import json
import logging
import os
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

# Keys that are profile metadata, not slicer settings
_PROFILE_META_KEYS = {"name", "from", "inherits", "version", "type", "setting_id"}

# Parameters that must be clamped to a minimum value
_CLAMP_RULES = {
    "raft_first_layer_expansion": 0,
    "solid_infill_filament": 1,
    "sparse_infill_filament": 1,
    "tree_support_wall_count": 0,
    "wall_filament": 1,
}


@dataclass
class SettingsTransferResult:
    status: str  # "applied", "no_original_profile", "no_customizations", "no_3mf_settings"
    transferred: list[dict[str, str]] = field(default_factory=list)


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
        if key in _PROFILE_META_KEYS:
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
        if k not in threemf_settings or k not in process_profile or k in _PROFILE_META_KEYS:
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
        if k not in threemf_settings or k in _PROFILE_META_KEYS:
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
    process_profile: dict[str, Any], threemf_settings: dict[str, Any],
) -> tuple[dict[str, Any], SettingsTransferResult]:
    """Transfer only user-customized settings from 3MF onto the process profile.

    Falls back to full overlay when we can't determine the original profile.
    """
    if not threemf_settings:
        logger.debug("No 3MF settings to transfer")
        return process_profile, SettingsTransferResult(status="no_3mf_settings")

    print_settings_id = threemf_settings.get("print_settings_id")
    if not print_settings_id:
        logger.debug("No print_settings_id in 3MF, falling back to full overlay")
        return (
            _overlay_3mf_settings(process_profile, threemf_settings),
            SettingsTransferResult(status="no_3mf_settings"),
        )

    original_profile = resolve_profile_by_name(print_settings_id)
    if original_profile is None:
        logger.debug(
            "Original profile %r not found, falling back to full overlay",
            print_settings_id,
        )
        return (
            _overlay_3mf_settings(process_profile, threemf_settings),
            SettingsTransferResult(status="no_original_profile"),
        )

    diffs = _diff_3mf_settings(threemf_settings, original_profile)
    if not diffs:
        logger.info("No customizations detected in 3MF vs original profile %r", print_settings_id)
        return process_profile, SettingsTransferResult(status="no_customizations")

    updated, applied_keys = _apply_customizations(process_profile, threemf_settings, set(diffs.keys()))
    if not applied_keys:
        logger.info(
            "Detected %d customization(s) in 3MF vs %r but none apply to the target profile",
            len(diffs), print_settings_id,
        )
        return process_profile, SettingsTransferResult(status="no_customizations")

    logger.info(
        "Applied %d customization(s) from 3MF (of %d detected) vs original profile %r: %s",
        len(applied_keys), len(diffs), print_settings_id, list(applied_keys),
    )
    transferred = [
        {"key": k, "value": json.dumps(tv), "original": json.dumps(ov)}
        for k, (tv, ov) in diffs.items()
        if k in applied_keys
    ]
    return updated, SettingsTransferResult(status="applied", transferred=transferred)


async def slice_3mf(
    file_bytes: bytes,
    machine_profile_id: str,
    process_profile_id: str,
    filament_profile_ids: list[str],
) -> tuple[bytes, SettingsTransferResult]:
    """Slice a 3MF file and return the sliced result as bytes + transfer info."""
    logger.info(
        "Slice request: machine=%s process=%s filaments=%s file_size=%d",
        machine_profile_id, process_profile_id, filament_profile_ids, len(file_bytes),
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

    # G92 E0 workaround
    lcg = machine_profile.get("layer_change_gcode", "")
    if "G92 E0" not in lcg:
        logger.debug("Injecting G92 E0 into layer_change_gcode")
        machine_profile = {**machine_profile, "layer_change_gcode": "G92 E0\n" + lcg}

    async with _slice_semaphore:
        return await _do_slice(
            file_bytes, machine_profile, process_profile, filament_profiles,
        )


async def _do_slice(
    file_bytes: bytes,
    machine_profile: dict[str, Any],
    process_profile: dict[str, Any],
    filament_profiles: list[dict[str, Any]],
) -> tuple[bytes, SettingsTransferResult]:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Read project settings and thumbnails BEFORE multi-plate extraction
        # (which creates a fresh 3MF without these files)
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
                # Extract plate 1 thumbnails for later injection into output
                for name in zf.namelist():
                    if name.endswith(".png") and (
                        "plate_1" in name or "top_1." in name or "pick_1." in name
                    ):
                        original_thumbnails[name] = zf.read(name)
        except (zipfile.BadZipFile,) as exc:
            logger.debug("Could not read 3MF: %s", exc)
        if original_thumbnails:
            logger.debug("Extracted %d thumbnail(s) from original 3MF", len(original_thumbnails))

        # For multi-plate 3MFs, extract plate 1 into a fresh simple 3MF.
        # OrcaSlicer CLI crashes on multi-plate files due to geometry processing bugs.
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

        # Validate model fits the build volume (after multi-plate extraction)
        fit_check = validate_model_fits(file_bytes, machine_profile)
        if not fit_check.fits:
            raise ModelTooBigError(fit_check.error)

        # Write input 3MF
        input_path = os.path.join(tmpdir, "input.3mf")
        with open(input_path, "wb") as f:
            f.write(file_bytes)

        # Strip machine keys from 3MF settings before transfer/diff
        threemf_settings = {
            k: v for k, v in threemf_settings.items()
            if k not in machine_profile or k in _PROFILE_META_KEYS
        }

        # Smart settings transfer: only apply user customizations
        transfer_result = _smart_settings_transfer(process_profile, threemf_settings)
        process_profile = transfer_result[0]
        settings_transfer = transfer_result[1]

        # Clamp transferred values that must meet minimums
        for key, min_val in _CLAMP_RULES.items():
            if key in process_profile:
                val = process_profile[key]
                try:
                    num = float(val) if isinstance(val, str) else val
                    if num < min_val:
                        process_profile[key] = str(min_val) if isinstance(val, str) else min_val
                except (ValueError, TypeError):
                    pass

        # Sanitize 3MF (clamps invalid values)
        slice_filepath = _sanitize_3mf(input_path, tmpdir)
        if slice_filepath != input_path:
            logger.debug("3MF was sanitized")

        # Write profile temp files
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

        # Build CLI command
        settings_arg = f"{machine_path};{process_path}"
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

        # Run OrcaSlicer
        logger.info("Running: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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

        # Read result
        result_path = os.path.join(tmpdir, "result.3mf")
        if not os.path.isfile(result_path):
            logger.error("Output file not found at %s", result_path)
            raise SlicingError(
                "OrcaSlicer did not produce output file",
                orca_output=orca_output,
            )

        # Inject original thumbnails if missing from output
        if original_thumbnails:
            with zipfile.ZipFile(result_path, "r") as zf:
                existing = set(zf.namelist())
            missing = {k: v for k, v in original_thumbnails.items() if k not in existing}
            if missing:
                with zipfile.ZipFile(result_path, "a") as zf:
                    for name, data in missing.items():
                        zf.writestr(name, data)
                logger.info("Injected %d thumbnail(s) into output 3MF", len(missing))

        result_size = os.path.getsize(result_path)
        logger.info("Sliced output: %s (%d bytes)", result_path, result_size)
        with open(result_path, "rb") as f:
            return f.read(), settings_transfer
