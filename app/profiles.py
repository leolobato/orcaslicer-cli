"""Load and resolve vendor profiles from OrcaSlicer resources at runtime."""

import json
import logging
import os
from typing import Any

from .config import PROFILES_DIR, USER_PROFILES_DIR

logger = logging.getLogger(__name__)

# Keys stripped from resolved profiles (inheritance metadata).
# NOTE: "from" must NOT be stripped — OrcaSlicer CLI requires it.
STRIP_KEYS = {"inherits", "instantiation"}

# Raw profiles loaded at startup: {name: raw_json_data}
_raw_profiles: dict[str, dict[str, Any]] = {}
# Category map: {name: "machine" | "process" | "filament"}
_type_map: dict[str, str] = {}
# Memoized resolved profiles
_resolved_cache: dict[str, dict[str, Any]] = {}
# Index: {vendor_slug (e.g. "BBL.GM014"): name}
_setting_id_index: dict[str, str] = {}
# Vendor map: {profile_name: vendor_name}
_vendor_map: dict[str, str] = {}


class ProfileNotFoundError(Exception):
    pass


def _detect_profile_type(data: dict[str, Any]) -> str:
    """Detect whether a profile is machine, process, or filament."""
    if "filament_type" in data or "filament_id" in data:
        return "filament"
    if "printer_model" in data or "machine_start_gcode" in data:
        return "machine"
    return "process"


def _load_user_profiles() -> int:
    """Load user-provided profile JSONs from USER_PROFILES_DIR.

    Returns the number of profiles loaded.
    """
    if not os.path.isdir(USER_PROFILES_DIR):
        return 0

    count = 0
    for fname in sorted(os.listdir(USER_PROFILES_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(USER_PROFILES_DIR, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Skipping invalid user profile %s: %s", fname, e)
            continue

        name = data.get("name")
        if not name:
            logger.warning("Skipping user profile %s: missing 'name' field", fname)
            continue

        category = _detect_profile_type(data)
        _raw_profiles[name] = data
        _type_map[name] = category
        _vendor_map[name] = "user"

        # Use setting_id if present, otherwise use name as identifier
        if "setting_id" not in data:
            data["setting_id"] = name
        slug = _vendor_slug("user", data["setting_id"])
        _setting_id_index[slug] = name

        count += 1
        logger.info("Loaded user %s profile: %s", category, name)

    return count


def _load_vendor_profiles(vendor_dir: str, index: dict) -> tuple[
    dict[str, dict[str, Any]], dict[str, str]
]:
    """Load all profiles for a single vendor directory.

    Returns (profiles, type_map).
    """
    profiles: dict[str, dict[str, Any]] = {}
    type_map: dict[str, str] = {}

    # Machine profiles — read every JSON in machine/
    machine_dir = os.path.join(vendor_dir, "machine")
    if os.path.isdir(machine_dir):
        for fname in sorted(os.listdir(machine_dir)):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(machine_dir, fname)) as f:
                data = json.load(f)
            name = data.get("name", fname[:-5])
            profiles[name] = data
            type_map[name] = "machine"

    # Process profiles — from index
    for entry in index.get("process_list", []):
        sub_path = entry.get("sub_path", "")
        if not sub_path:
            continue
        path = os.path.join(vendor_dir, sub_path)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            data = json.load(f)
        name = data.get("name", entry.get("name", ""))
        if name:
            profiles[name] = data
            type_map[name] = "process"

    # Filament profiles — from index
    for entry in index.get("filament_list", []):
        sub_path = entry.get("sub_path", "")
        if not sub_path:
            continue
        path = os.path.join(vendor_dir, sub_path)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            data = json.load(f)
        name = data.get("name", entry.get("name", ""))
        if name:
            profiles[name] = data
            type_map[name] = "filament"

    return profiles, type_map


def _vendor_slug(vendor_name: str, setting_id: str) -> str:
    """Build a vendor-prefixed slug like 'BBL.GM014'."""
    return f"{vendor_name}.{setting_id}"


def load_all_profiles() -> None:
    """Read all vendor profile JSONs into memory at startup."""
    _raw_profiles.clear()
    _type_map.clear()
    _resolved_cache.clear()
    _setting_id_index.clear()
    _vendor_map.clear()

    vendor_dirs: list[tuple[str, str]] = []  # (vendor_name, vendor_dir)

    # Discover all vendor index files in PROFILES_DIR
    for fname in sorted(os.listdir(PROFILES_DIR)):
        if not fname.endswith(".json"):
            continue
        vendor_name = fname[:-5]  # e.g. "BBL", "Creality"
        vendor_dir = os.path.join(PROFILES_DIR, vendor_name)
        index_path = os.path.join(PROFILES_DIR, fname)

        if not os.path.isdir(vendor_dir):
            continue

        with open(index_path) as f:
            index = json.load(f)

        v_profiles, v_type_map = _load_vendor_profiles(vendor_dir, index)
        _raw_profiles.update(v_profiles)
        _type_map.update(v_type_map)
        for name in v_profiles:
            _vendor_map[name] = vendor_name
        vendor_dirs.append((vendor_name, vendor_dir))

    # Build setting_id index with vendor-prefixed slugs
    for name, data in _raw_profiles.items():
        if "setting_id" in data:
            vendor = _vendor_map.get(name, "unknown")
            slug = _vendor_slug(vendor, data["setting_id"])
            _setting_id_index[slug] = name

    # Follow inherits chains to discover unlisted base profiles
    # Search across ALL vendor dirs for parent profiles
    pending = list(_raw_profiles.keys())
    while pending:
        next_batch = []
        for name in pending:
            parent_name = _raw_profiles[name].get("inherits")
            if not parent_name or parent_name in _raw_profiles:
                continue
            ptype = _type_map.get(name, "")
            if not ptype:
                continue
            for vendor_name, vdir in vendor_dirs:
                path = os.path.join(vdir, ptype, f"{parent_name}.json")
                if os.path.isfile(path):
                    with open(path) as f:
                        data = json.load(f)
                    actual_name = data.get("name", parent_name)
                    _raw_profiles[actual_name] = data
                    _type_map[actual_name] = ptype
                    _vendor_map[actual_name] = vendor_name
                    if "setting_id" in data:
                        slug = _vendor_slug(vendor_name, data["setting_id"])
                        _setting_id_index[slug] = actual_name
                    next_batch.append(actual_name)
                    break
        pending = next_batch

    # Load user-provided profiles from USER_PROFILES_DIR
    user_count = _load_user_profiles()

    counts: dict[str, int] = {}
    for cat in _type_map.values():
        counts[cat] = counts.get(cat, 0) + 1
    logger.info(
        "Loaded %d machine, %d process, %d filament profiles from %d vendors"
        " (%d user profiles)",
        counts.get("machine", 0),
        counts.get("process", 0),
        counts.get("filament", 0),
        len(vendor_dirs),
        user_count,
    )


def resolve_profile_by_name(name: str) -> dict[str, Any] | None:
    """Resolve a single profile's inheritance chain, with memoization."""
    if name in _resolved_cache:
        return _resolved_cache[name]

    profile = _raw_profiles.get(name)
    if profile is None:
        return None

    parent_name = profile.get("inherits")
    if parent_name and parent_name in _raw_profiles:
        parent = resolve_profile_by_name(parent_name)
        if parent is not None:
            merged = dict(parent)
            merged.update(profile)
        else:
            merged = dict(profile)
    else:
        merged = dict(profile)

    _resolved_cache[name] = merged
    return merged


def _clean_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Remove inheritance metadata keys from a resolved profile."""
    return {k: v for k, v in profile.items() if k not in STRIP_KEYS}


def _slug_for_profile(name: str) -> str:
    """Return the vendor-prefixed slug for a profile name."""
    data = _raw_profiles.get(name, {})
    vendor = _vendor_map.get(name, "unknown")
    sid = data.get("setting_id", "")
    return _vendor_slug(vendor, sid)


def _name_for_slug(slug: str) -> str | None:
    """Look up a profile name by its vendor-prefixed slug (e.g. 'BBL.GM014')."""
    return _setting_id_index.get(slug)


def _machine_names_for_slug(machine_slug: str) -> set[str]:
    """Return all machine profile names matching a machine slug.

    A single slug (e.g. BBL.GM030) maps to one machine name like
    "Bambu Lab A1 0.4 nozzle". But for filtering compatible_printers we
    return all machine names that share the same printer_model+nozzle combo.
    """
    name = _name_for_slug(machine_slug)
    if not name:
        return set()
    return {name}


def get_profile(category: str, slug: str) -> dict[str, Any]:
    """Return a fully-resolved profile by its vendor-prefixed slug (e.g. 'BBL.GM014')."""
    name = _name_for_slug(slug)
    if not name:
        raise ProfileNotFoundError(
            f"{category} profile with id '{slug}' not found"
        )
    if _type_map.get(name) != category:
        raise ProfileNotFoundError(
            f"'{slug}' is not a {category} profile"
        )
    resolved = resolve_profile_by_name(name)
    if resolved is None:
        raise ProfileNotFoundError(
            f"Failed to resolve {category} profile '{slug}'"
        )
    return _clean_profile(resolved)


def get_machine_profiles() -> list[dict[str, Any]]:
    """Return resolved leaf machine profiles."""
    results = []
    for name, raw in _raw_profiles.items():
        if _type_map.get(name) != "machine":
            continue
        if raw.get("instantiation") != "true":
            continue
        resolved = resolve_profile_by_name(name)
        if resolved is None:
            continue
        nozzle = resolved.get("nozzle_diameter", ["0.4"])
        if isinstance(nozzle, list):
            nozzle = nozzle[0] if nozzle else "0.4"
        results.append({
            "setting_id": _slug_for_profile(name),
            "name": resolved.get("name", name),
            "nozzle_diameter": nozzle,
            "printer_model": resolved.get("printer_model", ""),
        })
    results.sort(key=lambda x: x["name"])
    return results


def get_process_profiles(
    machine_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return resolved leaf process profiles, optionally filtered by machine."""
    machine_names: set[str] | None = None
    if machine_id:
        machine_names = _machine_names_for_slug(machine_id)
        if not machine_names:
            raise ProfileNotFoundError(
                f"Machine with id '{machine_id}' not found"
            )

    results = []
    for name, raw in _raw_profiles.items():
        if _type_map.get(name) != "process":
            continue
        if raw.get("instantiation") != "true":
            continue
        resolved = resolve_profile_by_name(name)
        if resolved is None:
            continue

        if machine_names:
            compat = resolved.get("compatible_printers", [])
            if not any(m in compat for m in machine_names):
                continue

        # Map compatible_printers names to vendor-prefixed slugs
        compat_slugs = []
        for cp_name in resolved.get("compatible_printers", []):
            if cp_name in _raw_profiles:
                compat_slugs.append(_slug_for_profile(cp_name))

        layer_height = resolved.get("layer_height", "")
        if isinstance(layer_height, list):
            layer_height = layer_height[0] if layer_height else ""

        results.append({
            "setting_id": _slug_for_profile(name),
            "name": resolved.get("name", name),
            "compatible_printers": compat_slugs,
            "layer_height": layer_height,
        })
    results.sort(key=lambda x: x["name"])
    return results


def get_filament_profiles(
    machine_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return resolved leaf filament profiles, optionally filtered by machine."""
    machine_names: set[str] | None = None
    if machine_id:
        machine_names = _machine_names_for_slug(machine_id)
        if not machine_names:
            raise ProfileNotFoundError(
                f"Machine with id '{machine_id}' not found"
            )

    results = []
    for name, raw in _raw_profiles.items():
        if _type_map.get(name) != "filament":
            continue
        if raw.get("instantiation") != "true":
            continue
        resolved = resolve_profile_by_name(name)
        if resolved is None:
            continue

        if machine_names:
            compat = resolved.get("compatible_printers", [])
            if not any(m in compat for m in machine_names):
                continue

        # Map compatible_printers names to vendor-prefixed slugs
        compat_slugs = []
        for cp_name in resolved.get("compatible_printers", []):
            if cp_name in _raw_profiles:
                compat_slugs.append(_slug_for_profile(cp_name))

        filament_type = resolved.get("filament_type", [""])[0] if isinstance(
            resolved.get("filament_type"), list
        ) else resolved.get("filament_type", "")

        results.append({
            "setting_id": _slug_for_profile(name),
            "name": resolved.get("name", name),
            "compatible_printers": compat_slugs,
            "filament_type": filament_type,
        })
    results.sort(key=lambda x: x["name"])
    return results
