"""Drive ``orca-headless dump-profiles`` and populate the in-memory cache.

The legacy path in ``app/profiles.py::load_all_profiles`` walked all vendor
JSONs and resolved ``inherits`` chains in Python. Sub-phase B replaces that
with a single subprocess call: the binary stands up a real
``Slic3r::PresetBundle`` (the same one the slicer uses), iterates over the
loaded printers / prints / filaments, and writes a JSON manifest. We parse
the manifest into the existing module-level indexes so the listing
endpoints (``/profiles/{machines,processes,filaments}``) keep working
without knowing the source.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from . import config as cfg
from . import profiles

logger = logging.getLogger(__name__)


async def run_dump_profiles() -> dict[str, list[dict[str, Any]]]:
    """Invoke the binary's ``dump-profiles`` subcommand and return the manifest.

    Manifest shape is ``{"machines": [...], "processes": [...], "filaments": [...]}``.
    Raises ``RuntimeError`` on subprocess failure or non-OK envelope.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out_path = Path(tf.name)
    try:
        request = json.dumps({
            "profiles_dir": cfg.PROFILES_DIR,
            "user_dir":     cfg.USER_PROFILES_DIR,
            "out_path":     str(out_path),
        }).encode()
        proc = await asyncio.create_subprocess_exec(
            cfg.ORCA_HEADLESS_BINARY, "dump-profiles",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=request), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("dump-profiles timed out after 120s")
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(
                f"dump-profiles exited {proc.returncode}: {tail}")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as e:
            tail = stdout.decode(errors="replace")[-2000:] if isinstance(stdout, bytes) else stdout[-2000:]
            raise RuntimeError(
                f"dump-profiles stdout not JSON: {e}; stdout tail: {tail}")
        if envelope.get("status") != "ok":
            raise RuntimeError(f"dump-profiles error envelope: {envelope}")
        manifest = json.loads(out_path.read_text())
        logger.info(
            "dump-profiles loaded: %d machines, %d processes, %d filaments",
            len(manifest["machines"]),
            len(manifest["processes"]),
            len(manifest["filaments"]),
        )
        return manifest
    finally:
        out_path.unlink(missing_ok=True)


def annotate_profile_cache(manifest: dict[str, list[dict[str, Any]]]) -> None:
    """Stamp each manifest entry onto the matching ``_raw_profiles`` entry.

    The legacy disk-walking loader populates ``_raw_profiles`` with full
    JSON content (config keys, ``inherits``, etc.) — slicing depends on
    that for inheritance resolution. The bundle's manifest carries the
    *resolved* listing-API shape. Stamp it as ``raw["_manifest"]`` so
    the listing endpoints serve the bundle's data without re-walking
    Python's chain, while the slicer continues to read ``raw`` directly.

    Repair pass for user-imported profiles: libslic3r's JSON preset
    loader (Bambu's GUI behavior, ``Preset.cpp::load_presets``) does
    NOT read ``setting_id`` from user JSON files (it treats that field
    as a cloud-sync identifier), and ``Preset.cpp:1314`` overwrites a
    user filament's ``filament_id`` with the inherited parent's value
    whenever an ``inherits`` chain exists. Our import flow stamps both
    fields onto the on-disk JSON as an orcaslicer-cli convention. The
    binary stays GUI-aligned (correct); we reconcile here by preferring
    the on-disk values from ``_raw_profiles`` when they are non-empty.
    ``ams_assignable`` is recomputed from the repaired entry.

    Sub-phase C will collapse the two by having the binary emit full
    resolved presets too, dropping the legacy walk.
    """
    for category, entries in (
        ("machine",  manifest["machines"]),
        ("process",  manifest["processes"]),
        ("filament", manifest["filaments"]),
    ):
        for entry in entries:
            vendor = entry.get("vendor", "")
            name   = entry.get("name", "")
            if not name:
                continue
            profile_key = profiles._profile_key(vendor, name)
            raw = profiles._raw_profiles.get(profile_key)
            if raw is None:
                # Fallback by name across vendors. libslic3r leaves
                # ``preset.vendor`` null for user-imported profiles
                # (manifest vendor=""), but the legacy walker indexes
                # those under ``_profile_key("User", name)``. Match by
                # name alone so the on-disk setting_id / filament_id
                # repair below actually finds the raw entry. Prefer a
                # legacy-walker key of the same category to avoid
                # cross-category collisions on duplicate names.
                for candidate_key in profiles._name_index.get(name, []):
                    if profiles._type_map.get(candidate_key) != category:
                        continue
                    candidate = profiles._raw_profiles.get(candidate_key)
                    if candidate is not None:
                        raw = candidate
                        profile_key = candidate_key
                        break
            if raw is None:
                # Bundle saw a preset the legacy walk didn't (e.g. an
                # OrcaFilamentLibrary variant). Synthesize a minimal raw
                # so the listing path still surfaces it; slicing won't
                # be able to resolve it but listing endpoints will.
                raw = {
                    "name":          name,
                    "instantiation": "true",
                    "setting_id":    entry.get("setting_id", ""),
                }
                if "filament_id" in entry:
                    raw["filament_id"] = entry["filament_id"]
                profiles._index_profile(profile_key, raw, category, vendor)

            raw_setting_id = str(raw.get("setting_id", "")).strip()
            if raw_setting_id:
                entry["setting_id"] = raw_setting_id

            if category == "filament":
                raw_filament_id = str(raw.get("filament_id", "")).strip()
                if raw_filament_id:
                    entry["filament_id"] = raw_filament_id
                entry["ams_assignable"] = profiles._is_ams_assignable_filament(
                    raw, entry, setting_id=str(entry.get("setting_id", "")),
                )

            raw["_manifest"] = entry
