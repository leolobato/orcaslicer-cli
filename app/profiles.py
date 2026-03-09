"""Load and resolve vendor profiles from OrcaSlicer resources at runtime."""

import hashlib
import json
import logging
import os
from datetime import datetime
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
# Index: {setting_id (e.g. "GM014"): name}
_setting_id_index: dict[str, str] = {}


class ProfileNotFoundError(Exception):
    pass


def _detect_profile_type(data: dict[str, Any]) -> str:
    """Detect whether a profile is machine, process, or filament."""
    if "filament_type" in data or "filament_id" in data:
        return "filament"
    if "printer_model" in data or "machine_start_gcode" in data:
        return "machine"
    return "process"


def _logical_filament_name(name: str) -> str:
    """Normalize a preset name to its logical filament name (before ' @')."""
    base, _, _ = name.partition(" @")
    return base.strip() if base else name.strip()


def _as_scalar_string(value: Any) -> str:
    """Return a scalar string from a profile field that may be a list."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    if value is None:
        return ""
    return str(value)


def _extract_filament_id(profile: dict[str, Any]) -> str:
    """Extract a normalized filament_id value from a profile dict."""
    return _as_scalar_string(profile.get("filament_id")).strip()


def _has_direct_filament_id(raw_profile: dict[str, Any]) -> bool:
    """Return True if a raw profile declares a usable filament_id directly."""
    filament_id = _extract_filament_id(raw_profile)
    return bool(filament_id and filament_id != "null")


def _is_ams_assignable_raw_filament(raw_profile: dict[str, Any]) -> bool:
    """AMS-assignable filament must be a root, instantiated profile with direct id."""
    inherits = raw_profile.get("inherits")
    has_inherits = isinstance(inherits, str) and bool(inherits.strip())
    return (
        raw_profile.get("instantiation") == "true"
        and not has_inherits
        and _has_direct_filament_id(raw_profile)
    )


def _iter_known_filament_names_and_ids() -> list[tuple[str, str]]:
    """Return known (logical_filament_name, filament_id) pairs from loaded profiles."""
    pairs: list[tuple[str, str]] = []
    for name, raw in _raw_profiles.items():
        if _type_map.get(name) != "filament":
            continue

        profile_name = str(raw.get("name", name))
        logical_name = _logical_filament_name(profile_name)

        # Prefer the raw profile's id; fallback to the resolved chain id.
        filament_id = _extract_filament_id(raw)
        if not filament_id:
            resolved = resolve_profile_by_name(name)
            if resolved:
                filament_id = _extract_filament_id(resolved)

        if not filament_id or filament_id == "null":
            continue
        pairs.append((logical_name, filament_id))
    return pairs


def _generate_custom_filament_id(logical_name: str) -> str:
    """Generate a custom filament id using Bambu/Orca-style P + md5 prefix."""
    known_pairs = _iter_known_filament_names_and_ids()
    id_to_names: dict[str, set[str]] = {}
    for existing_name, existing_id in known_pairs:
        id_to_names.setdefault(existing_id, set()).add(existing_name)

    base = "P" + hashlib.md5(logical_name.encode("utf-8")).hexdigest()[:7]
    if base not in id_to_names or logical_name in id_to_names[base]:
        return base

    # Collision with a different logical name: add a timestamp seed.
    for attempt in range(100):
        ts = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        seed = f"{logical_name}{ts}_{attempt}"
        candidate = "P" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:7]
        if candidate not in id_to_names or logical_name in id_to_names[candidate]:
            return candidate

    # Extremely unlikely fallback; keeps behavior deterministic enough.
    return base


def _find_reusable_filament_id(logical_name: str) -> str | None:
    """Return an existing filament_id for the same logical name, if present."""
    for existing_name, existing_id in _iter_known_filament_names_and_ids():
        if existing_name == logical_name:
            return existing_id
    return None


def _resolve_filament_parent_ref(parent_ref: str) -> str | None:
    """Resolve a filament inherits reference by name first, then by setting_id."""
    if parent_ref in _raw_profiles and _type_map.get(parent_ref) == "filament":
        return parent_ref

    by_setting_id = _name_for_slug(parent_ref)
    if by_setting_id and _type_map.get(by_setting_id) == "filament":
        return by_setting_id
    return None


def materialize_filament_import(data: dict[str, Any]) -> dict[str, Any]:
    """Create a clone-style root filament profile from imported JSON.

    Behavior mirrors Orca/Bambu GUI clone flow for custom filaments:
    - Resolve inheritance first (if present).
    - Produce a root profile (clear inherits/base_id).
    - Ensure explicit filament_id exists (reuse existing by logical name,
      otherwise generate Pxxxxxxx).
    """
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' field.")
    name = name.strip()

    setting_id = data.get("setting_id", name)
    if not isinstance(setting_id, str) or not setting_id.strip():
        raise ValueError("Missing or invalid 'setting_id' field.")
    setting_id = setting_id.strip()

    merged: dict[str, Any]
    inherits = data.get("inherits")
    if isinstance(inherits, str) and inherits.strip():
        parent_name = _resolve_filament_parent_ref(inherits.strip())
        if not parent_name:
            raise ProfileNotFoundError(
                f"Filament parent '{inherits.strip()}' not found"
            )
        parent = resolve_profile_by_name(parent_name)
        if parent is None:
            raise ProfileNotFoundError(
                f"Failed to resolve filament parent '{inherits.strip()}'"
            )
        merged = dict(parent)
        merged.update(data)
    else:
        merged = dict(data)

    if "filament_type" not in merged and "filament_id" not in merged:
        raise ValueError(
            "Profile must contain 'filament_type' or 'filament_id' (directly or via inherits)."
        )

    result = dict(merged)
    result["name"] = name
    result["setting_id"] = setting_id
    result["from"] = "User"
    result["instantiation"] = "true"
    result["filament_settings_id"] = [name]
    result.pop("inherits", None)
    result.pop("base_id", None)

    filament_id = _extract_filament_id(result)
    if not filament_id or filament_id == "null":
        logical_name = _logical_filament_name(name)
        reusable = _find_reusable_filament_id(logical_name)
        result["filament_id"] = reusable or _generate_custom_filament_id(logical_name)

    return result


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

        # Use setting_id if present, otherwise use name as identifier
        if "setting_id" not in data:
            data["setting_id"] = name
        _setting_id_index[data["setting_id"]] = name

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


def load_all_profiles() -> dict[str, int]:
    """Read all vendor profile JSONs into memory.

    Returns a summary dict with profile counts by category plus user count.
    """
    _raw_profiles.clear()
    _type_map.clear()
    _resolved_cache.clear()
    _setting_id_index.clear()

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
        vendor_dirs.append((vendor_name, vendor_dir))

    # Build setting_id index
    for name, data in _raw_profiles.items():
        if "setting_id" in data:
            _setting_id_index[data["setting_id"]] = name

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
                    if "setting_id" in data:
                        _setting_id_index[data["setting_id"]] = actual_name
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
    return {
        "machines": counts.get("machine", 0),
        "processes": counts.get("process", 0),
        "filaments": counts.get("filament", 0),
        "user": user_count,
    }


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
    """Return the setting_id for a profile name."""
    data = _raw_profiles.get(name, {})
    return data.get("setting_id", "")


def _name_for_slug(setting_id: str) -> str | None:
    """Look up a profile name by its setting_id (e.g. 'GM014')."""
    return _setting_id_index.get(setting_id)


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
    ams_assignable_only: bool = False,
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
        ams_assignable = _is_ams_assignable_raw_filament(raw)
        if ams_assignable_only and not ams_assignable:
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
            "ams_assignable": ams_assignable,
        })
    results.sort(key=lambda x: x["name"])
    return results
