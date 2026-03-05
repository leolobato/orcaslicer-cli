"""Slicing logic adapted from bambu-poc/print_3mf.py."""

import asyncio
import json
import os
import shutil
import tempfile
import zipfile
from typing import Any

from .config import ORCA_BINARY
from .profiles import ProfileNotFoundError, get_profile

# Serialize slicing requests — CPU-heavy
_slice_semaphore = asyncio.Semaphore(1)

# Keys that are profile metadata, not slicer settings
_PROFILE_META_KEYS = {"name", "from", "inherits", "version", "type", "setting_id"}

# Parameters that must be clamped to a minimum value
_CLAMP_RULES = {
    "raft_first_layer_expansion": 0,
    "solid_infill_filament": 1,
    "sparse_infill_filament": 1,
    "wall_filament": 1,
}


class SlicingError(Exception):
    def __init__(self, message: str, orca_output: str | None = None):
        super().__init__(message)
        self.orca_output = orca_output


def _sanitize_3mf(filepath: str, tmpdir: str) -> str:
    """Fix invalid parameter values in a 3MF's project_settings.config."""
    settings_file = "Metadata/project_settings.config"
    with zipfile.ZipFile(filepath, "r") as zf:
        if settings_file not in zf.namelist():
            return filepath

        raw = zf.read(settings_file).decode()
        settings = json.loads(raw)

        changed = False
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
                        zf_out.writestr(item, json.dumps(settings, indent=2))
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


async def slice_3mf(
    file_bytes: bytes,
    machine_profile_id: str,
    process_profile_id: str,
    filament_profile_ids: list[str],
) -> bytes:
    """Slice a 3MF file and return the sliced result as bytes."""
    # Resolve profiles
    machine_profile = get_profile("machine", machine_profile_id)
    process_profile = get_profile("process", process_profile_id)
    filament_profiles = [
        get_profile("filament", fid) for fid in filament_profile_ids
    ]

    # G92 E0 workaround
    lcg = machine_profile.get("layer_change_gcode", "")
    if "G92 E0" not in lcg:
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
) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write input 3MF
        input_path = os.path.join(tmpdir, "input.3mf")
        with open(input_path, "wb") as f:
            f.write(file_bytes)

        # Read 3MF project settings for overlay
        threemf_settings = {}
        try:
            with zipfile.ZipFile(input_path, "r") as zf:
                raw = zf.read("Metadata/project_settings.config").decode()
                threemf_settings = json.loads(raw)
        except (KeyError, json.JSONDecodeError, zipfile.BadZipFile):
            pass

        # Overlay 3MF settings onto process profile
        process_profile = _overlay_3mf_settings(process_profile, threemf_settings)

        # Sanitize 3MF
        slice_filepath = _sanitize_3mf(input_path, tmpdir)

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
        cmd += [
            "--slice", "1",
            "--export-3mf", "result.3mf",
            "--allow-newer-file",
            "--outputdir", tmpdir,
            os.path.abspath(slice_filepath),
        ]

        # Run OrcaSlicer
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        orca_output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()

        if proc.returncode != 0:
            raise SlicingError(
                f"OrcaSlicer exited with code {proc.returncode}",
                orca_output=orca_output,
            )

        # Read result
        result_path = os.path.join(tmpdir, "result.3mf")
        if not os.path.isfile(result_path):
            raise SlicingError(
                "OrcaSlicer did not produce output file",
                orca_output=orca_output,
            )

        with open(result_path, "rb") as f:
            return f.read()
