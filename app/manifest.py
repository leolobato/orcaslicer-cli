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


def populate_profile_cache(manifest: dict[str, list[dict[str, Any]]]) -> None:
    """Replace the module-level profile caches in ``app.profiles`` with manifest data.

    Caches populated: ``_raw_profiles``, ``_type_map``, ``_vendor_map``,
    ``_name_index``, ``_setting_id_index``. Each entry stores the manifest
    dict at ``raw["_manifest"]`` so listing functions can serve the API
    response directly without re-resolving anything.
    """
    profiles._raw_profiles.clear()
    profiles._type_map.clear()
    profiles._vendor_map.clear()
    profiles._name_index.clear()
    profiles._setting_id_index.clear()
    if hasattr(profiles, "_resolved_cache"):
        profiles._resolved_cache.clear()

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
            # Synthesize a minimal raw dict matching the legacy shape:
            # listing functions look at ``instantiation``/``setting_id``
            # directly. The full manifest entry lives under ``_manifest``
            # for direct serialization in the listing path.
            raw: dict[str, Any] = {
                "name":          name,
                "instantiation": "true",
                "setting_id":    entry.get("setting_id", ""),
                "_manifest":     entry,
            }
            if "filament_id" in entry:
                raw["filament_id"] = entry["filament_id"]
            profiles._index_profile(profile_key, raw, category, vendor)
