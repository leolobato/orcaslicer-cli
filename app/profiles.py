"""Load and resolve vendor profiles from OrcaSlicer resources at runtime."""

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

from .config import PROFILES_DIR, USER_PROFILES_DIR

logger = logging.getLogger(__name__)

ORCA_FILAMENT_LIBRARY = "OrcaFilamentLibrary"

# Keys stripped from resolved profiles (inheritance metadata).
# NOTE: "from" must NOT be stripped — OrcaSlicer CLI requires it.
STRIP_KEYS = {"inherits", "instantiation"}

# Raw profiles loaded at startup: {internal_key: raw_json_data}
_raw_profiles: dict[str, dict[str, Any]] = {}
# Category map: {internal_key: "machine" | "process" | "filament"}
_type_map: dict[str, str] = {}
# Vendor map: {internal_key: vendor_name}
_vendor_map: dict[str, str] = {}
# Display-name index: {name: [internal_key, ...]}
_name_index: dict[str, list[str]] = {}
# Memoized resolved profiles. Keyed by profile_key. Cleared by
# load_all_profiles(); callers that catch ProfileNotFoundError and
# repair the index must reload before re-resolving, or stale results
# from previously-cached siblings will leak through.
_resolved_cache: dict[str, dict[str, Any]] = {}
# Index: {setting_id (e.g. "GM014"): [internal_key, ...]}
_setting_id_index: dict[str, list[str]] = {}


class ProfileNotFoundError(Exception):
    pass


def _profile_key(vendor_name: str, name: str) -> str:
    """Return a stable internal key for a loaded profile."""
    return f"{vendor_name}::{name}"


def _display_name(profile_key: str) -> str:
    """Return the human-readable name for an internal profile key."""
    raw = _raw_profiles.get(profile_key, {})
    return str(raw.get("name", profile_key))


def _index_profile(profile_key: str, data: dict[str, Any], category: str, vendor_name: str) -> None:
    """Register a loaded profile in the in-memory indexes."""
    _raw_profiles[profile_key] = data
    _type_map[profile_key] = category
    _vendor_map[profile_key] = vendor_name
    name = str(data.get("name", profile_key))
    _name_index.setdefault(name, []).append(profile_key)

    setting_id = str(data.get("setting_id", "")).strip()
    if setting_id:
        _setting_id_index.setdefault(setting_id, []).append(profile_key)


def _candidate_keys_for_name(name: str, *, category: str | None = None) -> list[str]:
    """Return loaded profile keys that share the exact display name."""
    keys = _name_index.get(name, [])
    if category is None:
        return list(keys)
    return [key for key in keys if _type_map.get(key) == category]


def _prefer_same_vendor(
    keys: list[str],
    *,
    preferred_vendor: str | None,
) -> list[str]:
    """Sort candidate keys so the matching vendor is preferred first."""
    if not preferred_vendor:
        return list(keys)
    preferred = [key for key in keys if _vendor_map.get(key) == preferred_vendor]
    fallback = [key for key in keys if _vendor_map.get(key) != preferred_vendor]
    return preferred + fallback


def _select_profile_key_by_name(
    name: str,
    *,
    category: str | None = None,
    preferred_vendor: str | None = None,
) -> str | None:
    """Select one loaded profile key for a display name."""
    keys = _candidate_keys_for_name(name, category=category)
    if not keys:
        return None
    ordered = _prefer_same_vendor(keys, preferred_vendor=preferred_vendor)
    return ordered[0] if ordered else None


def _resolve_parent_key(
    parent_name: str,
    *,
    category: str,
    preferred_vendor: str,
) -> str | None:
    """Resolve an `inherits` reference using OrcaSlicer GUI semantics.

    Same-vendor wins; the only cross-vendor fallback is OrcaFilamentLibrary.
    User profiles additionally fall back to any system vendor, since user
    customizations commonly clone vendor presets.
    """
    keys = _candidate_keys_for_name(parent_name, category=category)
    if not keys:
        return None

    same = [k for k in keys if _vendor_map.get(k) == preferred_vendor]
    if same:
        return same[0]

    library = [k for k in keys if _vendor_map.get(k) == ORCA_FILAMENT_LIBRARY]
    if library:
        return library[0]

    if preferred_vendor == "User":
        other = [
            k for k in keys
            if _vendor_map.get(k) not in (preferred_vendor, ORCA_FILAMENT_LIBRARY)
        ]
        if other:
            return other[0]

    return None


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


def _is_ams_assignable_filament(
    raw_profile: dict[str, Any],
    resolved_profile: dict[str, Any],
    *,
    setting_id: str,
) -> bool:
    """Return True when a filament profile can be assigned to AMS.

    A filament is considered assignable if it is instantiable, exposes a
    stable profile setting_id, and resolves to a non-empty filament_id.
    Inheritance does not disqualify assignability.
    """
    if raw_profile.get("instantiation") != "true":
        return False
    if not str(setting_id or "").strip():
        return False
    filament_id = _extract_filament_id(resolved_profile)
    return bool(filament_id and filament_id != "null")


def _iter_known_filament_names_and_ids() -> list[tuple[str, str]]:
    """Return known (logical_filament_name, filament_id) pairs from loaded profiles.

    Profiles with broken inherits chains are skipped with a warning, so a
    single bad vendor JSON cannot block id-collision detection during a
    user import.
    """
    pairs: list[tuple[str, str]] = []
    for profile_key, raw in _raw_profiles.items():
        if _type_map.get(profile_key) != "filament":
            continue

        profile_name = str(raw.get("name", _display_name(profile_key)))
        logical_name = _logical_filament_name(profile_name)

        # Prefer the raw profile's id; fallback to the resolved chain id.
        filament_id = _extract_filament_id(raw)
        if not filament_id:
            try:
                resolved = resolve_profile_by_name(profile_key)
            except ProfileNotFoundError as exc:
                logger.warning(
                    "Skipping filament '%s' during id-collision iteration: %s",
                    profile_key, exc,
                )
                continue
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


def _resolve_filament_parent_ref(parent_ref: str) -> str | None:
    """Resolve a filament inherits reference by name first, then by setting_id."""
    if parent_ref in _raw_profiles and _type_map.get(parent_ref) == "filament":
        return parent_ref

    by_setting_id = _name_for_slug(parent_ref)
    if by_setting_id and _type_map.get(by_setting_id) == "filament":
        return by_setting_id
    return _select_profile_key_by_name(parent_ref, category="filament")


def materialize_filament_import(data: dict[str, Any]) -> dict[str, Any]:
    """Create a clone-style root filament profile from imported JSON.

    Behavior mirrors Orca/Bambu GUI clone flow for custom filaments:
    - Resolve inheritance first (if present).
    - Produce a root profile (clear inherits/base_id).
    - Ensure explicit filament_id exists:
      - keep user-provided filament_id when present;
      - otherwise generate a custom Pxxxxxxx id (do not reuse inherited id).
    """
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' field.")
    name = name.strip()

    setting_id = data.get("setting_id", name)
    if not isinstance(setting_id, str) or not setting_id.strip():
        raise ValueError("Missing or invalid 'setting_id' field.")
    setting_id = setting_id.strip()
    has_direct_input_filament_id = _has_direct_filament_id(data)

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
    if has_direct_input_filament_id and filament_id and filament_id != "null":
        result["filament_id"] = filament_id
    else:
        logical_name = _logical_filament_name(name)
        result["filament_id"] = _generate_custom_filament_id(logical_name)

    return result


def _resolve_process_parent_ref(parent_ref: str) -> str | None:
    """Resolve a process inherits reference by name first, then by setting_id.

    Type-scoped: only returns a key where _type_map[key] == "process".
    """
    if parent_ref in _raw_profiles and _type_map.get(parent_ref) == "process":
        return parent_ref

    by_setting_id = _name_for_slug(parent_ref)
    if by_setting_id and _type_map.get(by_setting_id) == "process":
        return by_setting_id
    return _select_profile_key_by_name(parent_ref, category="process")


def materialize_process_import(data: dict[str, Any]) -> dict[str, Any]:
    """Create a clone-style root process profile from imported JSON.

    Mirrors materialize_filament_import for the process category:
    - Resolve inheritance chain (required for thin JSON exports).
    - Produce a root profile (strip inherits / base_id).
    - Stamp from="User", instantiation="true", print_settings_id=name.
    - Default setting_id to name when caller doesn't supply one.
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
        parent_name = _resolve_process_parent_ref(inherits.strip())
        if not parent_name:
            raise ProfileNotFoundError(
                f"Process parent '{inherits.strip()}' not found"
            )
        parent = resolve_profile_by_name(parent_name)
        if parent is None:
            raise ProfileNotFoundError(
                f"Failed to resolve process parent '{inherits.strip()}'"
            )
        merged = dict(parent)
        merged.update(data)
    else:
        merged = dict(data)

    result = dict(merged)
    result["name"] = name
    result["setting_id"] = setting_id
    result["from"] = "User"
    result["instantiation"] = "true"
    result["print_settings_id"] = name
    result.pop("inherits", None)
    result.pop("base_id", None)
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

        # Use setting_id if present, otherwise use name as identifier
        if "setting_id" not in data:
            data["setting_id"] = name

        category = _detect_profile_type(data)
        profile_key = _profile_key("User", str(name))
        _index_profile(profile_key, data, category, "User")

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
    _vendor_map.clear()
    _name_index.clear()
    _resolved_cache.clear()
    _setting_id_index.clear()

    vendor_dirs: list[tuple[str, str]] = []  # (vendor_name, vendor_dir)

    # Discover all vendor index files in PROFILES_DIR
    index_files = sorted(fname for fname in os.listdir(PROFILES_DIR) if fname.endswith(".json"))
    if f"{ORCA_FILAMENT_LIBRARY}.json" in index_files:
        index_files.remove(f"{ORCA_FILAMENT_LIBRARY}.json")
        index_files.insert(0, f"{ORCA_FILAMENT_LIBRARY}.json")

    for fname in index_files:
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
        for profile_name, data in v_profiles.items():
            profile_key = _profile_key(vendor_name, profile_name)
            _index_profile(profile_key, data, v_type_map[profile_name], vendor_name)
        vendor_dirs.append((vendor_name, vendor_dir))

    # Follow inherits chains to discover unlisted base profiles.
    # Strict GUI semantics: only search the child's own vendor and
    # OrcaFilamentLibrary. Same-vendor wins.
    pending = list(_raw_profiles.keys())
    while pending:
        next_batch = []
        for profile_key in pending:
            parent_name = _raw_profiles[profile_key].get("inherits")
            if not parent_name:
                continue
            ptype = _type_map.get(profile_key, "")
            if not ptype:
                continue
            preferred_vendor = _vendor_map.get(profile_key, "")
            existing_parent_key = _resolve_parent_key(
                parent_name,
                category=ptype,
                preferred_vendor=preferred_vendor,
            )
            if existing_parent_key and _vendor_map.get(existing_parent_key) == preferred_vendor:
                continue
            allowed = {preferred_vendor, ORCA_FILAMENT_LIBRARY}
            candidate_dirs = [item for item in vendor_dirs if item[0] in allowed]
            candidate_dirs.sort(key=lambda item: (item[0] != preferred_vendor, item[0]))
            for vendor_name, vdir in candidate_dirs:
                path = os.path.join(vdir, ptype, f"{parent_name}.json")
                if os.path.isfile(path):
                    with open(path) as f:
                        data = json.load(f)
                    actual_name = data.get("name", parent_name)
                    parent_key = _profile_key(vendor_name, actual_name)
                    if parent_key in _raw_profiles:
                        break
                    _index_profile(parent_key, data, ptype, vendor_name)
                    next_batch.append(parent_key)
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
    """Resolve a single profile's inheritance chain, with memoization.

    Raises `ProfileNotFoundError` if any link in the chain cannot be
    resolved. Returns `None` only when the requested name itself does
    not exist in the index (the caller can decide whether that is a
    hard error). Once a profile is found, every parent reference must
    resolve.
    """
    profile_key = name if name in _raw_profiles else _select_profile_key_by_name(name)
    if profile_key is None:
        return None

    if profile_key in _resolved_cache:
        return _resolved_cache[profile_key]

    profile = _raw_profiles.get(profile_key)
    if profile is None:
        return None

    parent_name = profile.get("inherits")
    if parent_name:
        parent_key = _resolve_parent_key(
            parent_name,
            category=_type_map.get(profile_key, ""),
            preferred_vendor=_vendor_map.get(profile_key, ""),
        )
        if parent_key is None:
            raise ProfileNotFoundError(
                f"Profile '{_display_name(profile_key)}' inherits from "
                f"'{parent_name}', which is not loaded."
            )
        parent = resolve_profile_by_name(parent_key)
        if parent is None:
            raise ProfileNotFoundError(
                f"Profile '{_display_name(profile_key)}' inherits from "
                f"'{parent_name}', whose profile entry is missing from the index "
                f"(internal state may be inconsistent — try POST /profiles/reload)."
            )
        merged = dict(parent)
        merged.update(profile)
    else:
        merged = dict(profile)

    _resolved_cache[profile_key] = merged
    return merged


def _clean_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Remove inheritance metadata keys from a resolved profile."""
    return {k: v for k, v in profile.items() if k not in STRIP_KEYS}


def _slug_for_profile(name: str) -> str:
    """Return the setting_id for a profile name."""
    data = _raw_profiles.get(name, {})
    return data.get("setting_id", "")


def _name_for_slug(setting_id: str) -> str | None:
    """Look up a profile key by its setting_id (e.g. 'GM014').

    Returns the first registered profile key for the given setting_id.
    """
    keys = _setting_id_index.get(setting_id)
    if not keys:
        return None
    return keys[0]


def _machine_names_for_slug(machine_slug: str) -> set[str]:
    """Return all machine profile names matching a machine slug.

    Multiple vendors may reuse the same setting_id (e.g. GM014 is used by
    both BBL and Z-Bolt).  We return display names for every machine that
    shares the setting_id so that compatible_printers filtering works for all
    of them.
    """
    keys = _setting_id_index.get(machine_slug)
    if not keys:
        return set()
    return {
        _display_name(k)
        for k in keys
        if _type_map.get(k) == "machine"
    }


def _resolve_by_slug(category: str, slug: str) -> tuple[str, dict[str, Any]]:
    """Look up and resolve a profile by setting_id and category.

    Returns (profile_key, resolved_profile). Raises ProfileNotFoundError.
    """
    keys = _setting_id_index.get(slug)
    if not keys:
        raise ProfileNotFoundError(
            f"{category} profile with id '{slug}' not found"
        )
    profile_key = None
    for k in keys:
        if _type_map.get(k) == category:
            profile_key = k
            break
    if profile_key is None:
        raise ProfileNotFoundError(
            f"'{slug}' is not a {category} profile"
        )
    resolved = resolve_profile_by_name(profile_key)
    if resolved is None:
        raise ProfileNotFoundError(
            f"Failed to resolve {category} profile '{slug}'"
        )
    return profile_key, resolved


def get_profile(category: str, slug: str) -> dict[str, Any]:
    """Return a fully-resolved profile by its vendor-prefixed slug (e.g. 'BBL.GM014')."""
    _, resolved = _resolve_by_slug(category, slug)
    return _clean_profile(resolved)


def get_profile_detail(category: str, slug: str) -> dict[str, Any]:
    """Return a profile with its resolved data and full inheritance chain.

    Returns a dict with:
      - setting_id, name, vendor: top-level metadata
      - resolved: the fully-resolved profile (cleaned of inheritance keys)
      - inheritance_chain: list of dicts, one per level from leaf to root,
        each with {name, vendor, own_fields}
    """
    profile_key, resolved = _resolve_by_slug(category, slug)

    # Walk the inheritance chain from leaf to root
    chain: list[dict[str, Any]] = []
    current_key = profile_key
    visited: set[str] = set()
    while current_key and current_key not in visited:
        visited.add(current_key)
        raw = _raw_profiles.get(current_key)
        if raw is None:
            break
        own_fields = {k: v for k, v in raw.items() if k not in STRIP_KEYS}
        chain.append({
            "name": _display_name(current_key),
            "vendor": _vendor_map.get(current_key, ""),
            "own_fields": own_fields,
        })
        parent_name = raw.get("inherits")
        if not parent_name:
            break
        current_key = _resolve_parent_key(
            parent_name,
            category=_type_map.get(current_key, ""),
            preferred_vendor=_vendor_map.get(current_key, ""),
        )

    return {
        "setting_id": slug,
        "name": _display_name(profile_key),
        "vendor": _vendor_map.get(profile_key, ""),
        "resolved": _clean_profile(resolved),
        "inheritance_chain": chain,
    }


def get_machine_profiles() -> list[dict[str, Any]]:
    """Return resolved leaf machine profiles."""
    results = []
    for profile_key, raw in _raw_profiles.items():
        if _type_map.get(profile_key) != "machine":
            continue
        if raw.get("instantiation") != "true":
            continue
        resolved = resolve_profile_by_name(profile_key)
        if resolved is None:
            continue
        nozzle = resolved.get("nozzle_diameter", ["0.4"])
        if isinstance(nozzle, list):
            nozzle = nozzle[0] if nozzle else "0.4"
        results.append({
            "setting_id": _slug_for_profile(profile_key),
            "name": resolved.get("name", _display_name(profile_key)),
            "vendor": _vendor_map.get(profile_key, ""),
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
    for profile_key, raw in _raw_profiles.items():
        if _type_map.get(profile_key) != "process":
            continue
        if raw.get("instantiation") != "true":
            continue
        resolved = resolve_profile_by_name(profile_key)
        if resolved is None:
            continue

        if machine_names:
            compat = resolved.get("compatible_printers", [])
            if not any(m in compat for m in machine_names):
                continue

        # Map compatible_printers names to vendor-prefixed slugs
        compat_slugs = []
        for cp_name in resolved.get("compatible_printers", []):
            cp_key = _select_profile_key_by_name(cp_name, category="machine")
            if cp_key:
                compat_slugs.append(_slug_for_profile(cp_key))

        layer_height = resolved.get("layer_height", "")
        if isinstance(layer_height, list):
            layer_height = layer_height[0] if layer_height else ""

        results.append({
            "setting_id": _slug_for_profile(profile_key),
            "name": resolved.get("name", _display_name(profile_key)),
            "vendor": _vendor_map.get(profile_key, ""),
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
    for profile_key, raw in _raw_profiles.items():
        if _type_map.get(profile_key) != "filament":
            continue
        if raw.get("instantiation") != "true":
            continue
        resolved = resolve_profile_by_name(profile_key)
        if resolved is None:
            continue
        setting_id = _slug_for_profile(profile_key)
        ams_assignable = _is_ams_assignable_filament(
            raw,
            resolved,
            setting_id=setting_id,
        )
        if ams_assignable_only and not ams_assignable:
            continue

        if machine_names:
            compat = resolved.get("compatible_printers", [])
            if not any(m in compat for m in machine_names):
                continue

        # Map compatible_printers names to vendor-prefixed slugs
        compat_slugs = []
        for cp_name in resolved.get("compatible_printers", []):
            cp_key = _select_profile_key_by_name(cp_name, category="machine")
            if cp_key:
                compat_slugs.append(_slug_for_profile(cp_key))

        filament_type = resolved.get("filament_type", [""])[0] if isinstance(
            resolved.get("filament_type"), list
        ) else resolved.get("filament_type", "")
        filament_id = _extract_filament_id(resolved)
        if not filament_id:
            filament_id = _extract_filament_id(raw)

        results.append({
            "setting_id": setting_id,
            "filament_id": filament_id,
            "name": resolved.get("name", _display_name(profile_key)),
            "vendor": _vendor_map.get(profile_key, ""),
            "compatible_printers": compat_slugs,
            "filament_type": filament_type,
            "ams_assignable": ams_assignable,
        })
    results.sort(key=lambda x: x["name"])
    return results
