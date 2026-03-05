"""Load and resolve BBL profiles from OrcaSlicer resources at runtime."""

import json
import os
from typing import Any

from .config import PROFILES_DIR

# Keys stripped from resolved profiles (inheritance metadata).
# NOTE: "from" must NOT be stripped — OrcaSlicer CLI requires it.
STRIP_KEYS = {"inherits", "instantiation"}

# Raw profiles loaded at startup: {name: raw_json_data}
_raw_profiles: dict[str, dict[str, Any]] = {}
# Category map: {name: "machine" | "process" | "filament"}
_type_map: dict[str, str] = {}
# Memoized resolved profiles
_resolved_cache: dict[str, dict[str, Any]] = {}
# Index: {setting_id: name}
_setting_id_index: dict[str, str] = {}


class ProfileNotFoundError(Exception):
    pass


def load_all_profiles() -> None:
    """Read all BBL profile JSONs into memory at startup."""
    _raw_profiles.clear()
    _type_map.clear()
    _resolved_cache.clear()
    _setting_id_index.clear()

    bbl_dir = PROFILES_DIR
    index_path = os.path.join(os.path.dirname(bbl_dir), "BBL.json")

    with open(index_path) as f:
        index = json.load(f)

    # Machine profiles — read every JSON in machine/
    machine_dir = os.path.join(bbl_dir, "machine")
    for fname in os.listdir(machine_dir):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(machine_dir, fname)) as f:
            data = json.load(f)
        name = data.get("name", fname[:-5])
        _raw_profiles[name] = data
        _type_map[name] = "machine"
        if "setting_id" in data:
            _setting_id_index[data["setting_id"]] = name

    # Process profiles — from index
    for entry in index.get("process_list", []):
        sub_path = entry.get("sub_path", "")
        if not sub_path:
            continue
        path = os.path.join(bbl_dir, sub_path)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            data = json.load(f)
        name = data.get("name", entry["name"])
        _raw_profiles[name] = data
        _type_map[name] = "process"
        if "setting_id" in data:
            _setting_id_index[data["setting_id"]] = name

    # Filament profiles — from index
    for entry in index.get("filament_list", []):
        sub_path = entry.get("sub_path", "")
        if not sub_path:
            continue
        path = os.path.join(bbl_dir, sub_path)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            data = json.load(f)
        name = data.get("name", entry["name"])
        _raw_profiles[name] = data
        _type_map[name] = "filament"
        if "setting_id" in data:
            _setting_id_index[data["setting_id"]] = name

    # Follow inherits chains to discover unlisted base profiles
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
            path = os.path.join(bbl_dir, ptype, f"{parent_name}.json")
            if not os.path.isfile(path):
                continue
            with open(path) as f:
                data = json.load(f)
            actual_name = data.get("name", parent_name)
            _raw_profiles[actual_name] = data
            _type_map[actual_name] = ptype
            if "setting_id" in data:
                _setting_id_index[data["setting_id"]] = actual_name
            next_batch.append(actual_name)
        pending = next_batch


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


def _name_for_setting_id(setting_id: str) -> str | None:
    """Look up a profile name by its setting_id."""
    return _setting_id_index.get(setting_id)


def _machine_names_for_setting_id(machine_setting_id: str) -> set[str]:
    """Return all machine profile names matching a machine setting_id.

    A single setting_id (e.g. GM030) maps to one machine name like
    "Bambu Lab A1 0.4 nozzle". But for filtering compatible_printers we
    return all machine names that share the same printer_model+nozzle combo.
    """
    name = _name_for_setting_id(machine_setting_id)
    if not name:
        return set()
    return {name}


def get_profile(category: str, setting_id: str) -> dict[str, Any]:
    """Return a fully-resolved profile by its setting_id."""
    name = _name_for_setting_id(setting_id)
    if not name:
        raise ProfileNotFoundError(
            f"{category} profile with setting_id '{setting_id}' not found"
        )
    if _type_map.get(name) != category:
        raise ProfileNotFoundError(
            f"'{setting_id}' is not a {category} profile"
        )
    resolved = resolve_profile_by_name(name)
    if resolved is None:
        raise ProfileNotFoundError(
            f"Failed to resolve {category} profile '{setting_id}'"
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
            "setting_id": resolved.get("setting_id", ""),
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
        machine_names = _machine_names_for_setting_id(machine_id)
        if not machine_names:
            raise ProfileNotFoundError(
                f"Machine with setting_id '{machine_id}' not found"
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

        # Map compatible_printers names to setting_ids
        compat_ids = []
        for cp_name in resolved.get("compatible_printers", []):
            cp_raw = _raw_profiles.get(cp_name)
            if cp_raw and "setting_id" in cp_raw:
                compat_ids.append(cp_raw["setting_id"])

        layer_height = resolved.get("layer_height", "")
        if isinstance(layer_height, list):
            layer_height = layer_height[0] if layer_height else ""

        results.append({
            "setting_id": resolved.get("setting_id", ""),
            "name": resolved.get("name", name),
            "compatible_printers": compat_ids,
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
        machine_names = _machine_names_for_setting_id(machine_id)
        if not machine_names:
            raise ProfileNotFoundError(
                f"Machine with setting_id '{machine_id}' not found"
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

        # Map compatible_printers names to setting_ids
        compat_ids = []
        for cp_name in resolved.get("compatible_printers", []):
            cp_raw = _raw_profiles.get(cp_name)
            if cp_raw and "setting_id" in cp_raw:
                compat_ids.append(cp_raw["setting_id"])

        filament_type = resolved.get("filament_type", [""])[0] if isinstance(
            resolved.get("filament_type"), list
        ) else resolved.get("filament_type", "")

        results.append({
            "setting_id": resolved.get("setting_id", ""),
            "name": resolved.get("name", name),
            "compatible_printers": compat_ids,
            "filament_type": filament_type,
        })
    results.sort(key=lambda x: x["name"])
    return results
