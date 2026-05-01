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


class UnresolvedChainError(ProfileNotFoundError):
    """Raised when a profile's inheritance chain cannot be resolved
    during a flatten/export operation. Subclasses ProfileNotFoundError
    so existing call sites that catch the parent class still work; new
    call sites can distinguish chain failures from "profile not found"
    via this subclass."""
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


def _filament_alias(name: str) -> str:
    """Alias of `_logical_filament_name`, used by the export path so the
    intent ('strip @<printer> for filename idempotency') reads at the call
    site. Kept as a separate symbol so a future change to one semantic
    doesn't silently affect the other.
    """
    return _logical_filament_name(name)


_SAFE_FILENAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def _safe_filename(name: str, *, fallback: str) -> str:
    """Convert a profile name into a safe `.json` filename.

    Rules:
    - Lowercase.
    - Replace any character not in [a-z0-9._-] with '_'.
    - Collapse runs of '_' to a single '_'.
    - Strip leading/trailing '_'.
    - If the result is empty, use `fallback` (also sanitized).
    - Append '.json'.
    """
    def _sanitize(value: str) -> str:
        out_chars = []
        for ch in value.lower():
            out_chars.append(ch if ch in _SAFE_FILENAME_ALLOWED else "_")
        # Collapse runs of '_' and trim.
        result_parts = "".join(out_chars).split("_")
        return "_".join(p for p in result_parts if p)

    base = _sanitize(name)
    if not base:
        base = _sanitize(fallback)
    if not base:
        base = "profile"
    return f"{base}.json"


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
    """Return (logical_filament_name, filament_id) pairs for resolvable filament profiles.

    Profiles whose inherits chain cannot be resolved are silently dropped
    from the returned list (and a warning is logged for each). This is
    appropriate for the only current caller (`_generate_custom_filament_id`,
    which uses these pairs to detect collisions when minting a new id —
    a profile that does not resolve cannot contribute an id and therefore
    cannot collide). New callers that need a complete enumeration must
    not use this function.
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
    """Lightly stamp an imported filament payload and return its raw form.

    The returned dict preserves `inherits` so the inheritance chain is
    resolved at slice / listing time. Stamping is limited to the fields
    needed for indexing and AMS identity:

    - `setting_id`: synthesized from `name` if missing.
    - `instantiation`: set to `"true"` if missing.
    - `filament_id`: kept verbatim if directly supplied; otherwise
      generated via `_generate_custom_filament_id`. Stamping the id at
      import time prevents it from inheriting from the parent (which
      would collide AMS identity for every clone of the parent).

    Validates that `inherits`, when set, points to a known filament
    profile, and that the resulting `filament_id` does not collide
    with another profile on overlapping `compatible_printers`.
    """
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' field.")
    name = name.strip()

    setting_id = data.get("setting_id", name)
    if not isinstance(setting_id, str) or not setting_id.strip():
        raise ValueError("Missing or invalid 'setting_id' field.")
    setting_id = setting_id.strip()

    inherits = data.get("inherits")
    if isinstance(inherits, str) and inherits.strip():
        parent_name = _resolve_filament_parent_ref(inherits.strip())
        if not parent_name:
            raise ProfileNotFoundError(
                f"Filament parent '{inherits.strip()}' not found"
            )

    result = dict(data)
    result["name"] = name
    result["setting_id"] = setting_id
    if "instantiation" not in result:
        result["instantiation"] = "true"

    # Stamp filament_id (AMS identity).
    if _has_direct_filament_id(data):
        # Caller's value wins; keep it verbatim.
        filament_id = _extract_filament_id(data)
        result["filament_id"] = filament_id
    else:
        logical_name = _logical_filament_name(name)
        filament_id = _generate_custom_filament_id(logical_name)
        result["filament_id"] = filament_id

    # Validate AMS-scope uniqueness against currently loaded profiles.
    # The importing profile is not yet indexed, so the chain walk here
    # resolves through the parent only — that's the right scope for
    # determining which printers this filament will end up applying to.
    compat = _compatible_printers_set_for_payload(result, category="filament")
    _check_filament_id_ams_scope(
        filament_id=filament_id,
        compatible_printers=compat,
        exclude_setting_id=setting_id,
    )

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


def _resolve_chain_for_payload(
    payload: dict[str, Any],
    *,
    category: str,
) -> dict[str, Any]:
    """Resolve a payload's inherits chain in memory without indexing it.

    Returns the fully merged dict (parent values overlaid with the
    payload's own keys). Raises `ProfileNotFoundError` if `inherits` is
    set but the parent or any link in its chain cannot be resolved.

    `category` must be `"filament"` or `"process"` (machine imports do
    not exist).

    The returned dict is a shallow copy: top-level keys are independent,
    but nested mutable values (lists, dicts) are shared with both the
    parent's resolved view and the input payload. Mutating nested values
    in place may affect the cached parent resolution; callers should
    treat the result as read-through.
    """
    inherits_raw = payload.get("inherits")
    if not isinstance(inherits_raw, str) or not inherits_raw.strip():
        return dict(payload)

    inherits = inherits_raw.strip()
    if category == "filament":
        parent_key = _resolve_filament_parent_ref(inherits)
    elif category == "process":
        parent_key = _resolve_process_parent_ref(inherits)
    else:
        raise ValueError(f"Unsupported category for chain resolution: {category!r}")

    if parent_key is None:
        raise ProfileNotFoundError(
            f"Parent {category} profile '{inherits}' not found"
        )

    parent_resolved = resolve_profile_by_name(parent_key)
    if parent_resolved is None:
        raise ProfileNotFoundError(
            f"Failed to resolve parent {category} profile '{inherits}'"
        )

    merged = dict(parent_resolved)
    merged.update(payload)
    return merged


def _compatible_printers_set_for_payload(
    payload: dict[str, Any],
    *,
    category: str,
) -> set[str]:
    """Return the resolved `compatible_printers` set for a payload.

    Walks the inherits chain (via `_resolve_chain_for_payload`) so that
    a payload that does not declare `compatible_printers` inherits it
    from the parent. Returns an empty set if no value is found anywhere
    in the chain.

    Used by the AMS-scope uniqueness check in `materialize_filament_import`
    — two filament profiles may share `filament_id` only when their
    resolved `compatible_printers` sets are disjoint.
    """
    merged = _resolve_chain_for_payload(payload, category=category)
    raw_value = merged.get("compatible_printers")
    if not isinstance(raw_value, list):
        return set()
    return {item for item in raw_value if isinstance(item, str)}


def _check_filament_id_ams_scope(
    *,
    filament_id: str,
    compatible_printers: set[str],
    exclude_setting_id: str | None,
) -> None:
    """Validate that `filament_id` does not collide on overlapping AMS scope.

    Two filament profiles may share `filament_id` iff their resolved
    `compatible_printers` sets are disjoint (different printers ⇒
    different AMS scopes). An empty set is treated as "all printers"
    and fails closed. A broken inherits chain on an existing profile
    also fails closed: we cannot verify the printer scope, so we treat
    the profile as conflicting rather than silently skipping it.

    `exclude_setting_id`, when provided, is the setting_id of the user
    profile being replaced — it is excluded from the comparison so a
    profile can be re-imported under itself.

    Vendor `@base` profiles that ship with `filament_id` but no
    `compatible_printers` will conflict with any user import re-using
    their id (the empty set is treated as "all printers" → fail
    closed). This is intentional AMS-identity protection: a user
    pasting a vendor id should receive a clear rejection rather than a
    silent collision.

    Raises `ValueError` (with a user-facing message) on conflict.
    """
    if not filament_id or filament_id == "null":
        return

    for other_key, raw in _raw_profiles.items():
        if _type_map.get(other_key) != "filament":
            continue
        if _extract_filament_id(raw) != filament_id:
            # Only check raw filament_id for performance; profiles that
            # inherit filament_id will be caught when the parent itself
            # is iterated (and at slice time inheriting the parent's id
            # is exactly what we are guarding against by stamping our
            # own id at import).
            continue
        other_setting_id = str(raw.get("setting_id", "")).strip()
        if exclude_setting_id and other_setting_id == exclude_setting_id:
            continue

        other_payload = dict(raw)
        try:
            other_printers = _compatible_printers_set_for_payload(
                other_payload, category="filament"
            )
        except ProfileNotFoundError:
            # Broken chain on the existing profile — we cannot verify
            # whether the printer sets overlap, so fail closed (treat
            # like "all printers" via the downstream empty-set branch).
            logger.warning(
                "Treating '%s' (filament_id=%s) as conflicting: "
                "broken inherits chain prevents AMS-scope verification",
                other_key, filament_id,
            )
            other_printers = set()

        existing_name = str(raw.get("name", _display_name(other_key)))

        # Empty on either side ⇒ "all printers" ⇒ assume conflict. Pick
        # the message variant that names the side with the missing
        # restriction so the operator knows which profile to fix. When
        # both sides are empty, surface the candidate side (the user is
        # importing it now and can act on it).
        if not compatible_printers:
            raise ValueError(
                f"filament_id '{filament_id}' is already used by profile "
                f"'{existing_name}', and the new profile has no "
                f"compatible_printers restriction (applies to all printers, "
                f"so it conflicts with every existing profile sharing this "
                f"id). Add a compatible_printers list or pick a different "
                f"filament_id."
            )
        if not other_printers:
            raise ValueError(
                f"filament_id '{filament_id}' is already used by profile "
                f"'{existing_name}', which has no compatible_printers "
                f"restriction (applies to all printers). Pick a different "
                f"filament_id."
            )

        overlap = compatible_printers & other_printers
        if not overlap:
            continue
        overlap_desc = ", ".join(sorted(overlap))

        raise ValueError(
            f"filament_id '{filament_id}' is already used by profile "
            f"'{existing_name}' on overlapping printers: {overlap_desc}."
        )


def materialize_process_import(data: dict[str, Any]) -> dict[str, Any]:
    """Lightly stamp an imported process payload and return its raw form.

    The returned dict preserves `inherits` so the inheritance chain is
    resolved at slice / listing time. Stamping is limited to:

    - `setting_id`: synthesized from `name` if missing.
    - `instantiation`: set to `"true"` if missing.

    Validates that `inherits`, when set, points to a known process
    profile.
    """
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' field.")
    name = name.strip()

    setting_id = data.get("setting_id", name)
    if not isinstance(setting_id, str) or not setting_id.strip():
        raise ValueError("Missing or invalid 'setting_id' field.")
    setting_id = setting_id.strip()

    inherits = data.get("inherits")
    if isinstance(inherits, str) and inherits.strip():
        parent_name = _resolve_process_parent_ref(inherits.strip())
        if not parent_name:
            raise ProfileNotFoundError(
                f"Process parent '{inherits.strip()}' not found"
            )

    result = dict(data)
    result["name"] = name
    result["setting_id"] = setting_id
    if "instantiation" not in result:
        result["instantiation"] = "true"
    return result


def _load_user_profiles() -> int:
    """Load user-provided profile JSONs from USER_PROFILES_DIR recursively.

    Files at the root of `USER_PROFILES_DIR` are loaded first (legacy
    flat layout), followed by files inside any subdirectory walked
    alphabetically. The typed subfolders `filament/`, `process/`, and
    `machine/` are the canonical write targets, but any nested layout
    is accepted. When the same profile `name` appears in multiple
    files, the file walked later wins — so a typed-subfolder copy
    overrides a legacy root file with the same name.
    """
    if not os.path.isdir(USER_PROFILES_DIR):
        return 0

    seen_names: set[str] = set()
    count = 0
    # topdown=True yields the root directory before its subdirs, so
    # legacy flat files load first and typed-subfolder files override.
    for dirpath, dirnames, filenames in os.walk(USER_PROFILES_DIR, topdown=True):
        # Skip hidden directories (e.g. macOS `.fseventsd`).
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fname in sorted(filenames):
            # Skip dotfiles, including macOS AppleDouble forks (`._*.json`)
            # that share the .json suffix but are AppleDouble-encoded binaries.
            if fname.startswith(".") or not fname.endswith(".json"):
                continue
            path = os.path.join(dirpath, fname)
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, USER_PROFILES_DIR)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                logger.warning("Skipping invalid user profile %s: %s", rel, e)
                continue

            name = data.get("name")
            if not name:
                logger.warning(
                    "Skipping user profile %s: missing 'name' field", rel
                )
                continue

            if "setting_id" not in data:
                data["setting_id"] = name

            category = _detect_profile_type(data)
            profile_key = _profile_key("User", str(name))
            if str(name) in seen_names:
                logger.warning(
                    "User profile %s overrides earlier profile with name '%s'",
                    rel, name,
                )
            _index_profile(profile_key, data, category, "User")
            seen_names.add(str(name))

            count += 1
            logger.info("Loaded user %s profile: %s (%s)", category, name, rel)

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


def _printer_variant_count(printer_name: str) -> int:
    """Return the number of extruder variants for a printer profile.

    Resolves the machine profile by name and reads its
    `printer_extruder_variant` list length. Returns 1 when the
    printer is unknown or the field is absent — degrades gracefully
    rather than failing the export.
    """
    profile_key = _select_profile_key_by_name(printer_name, category="machine")
    if profile_key is None:
        return 1
    try:
        resolved = resolve_profile_by_name(profile_key)
    except ProfileNotFoundError:
        return 1
    if not resolved:
        return 1
    variants = resolved.get("printer_extruder_variant")
    if isinstance(variants, list) and variants:
        return len(variants)
    return 1


# Filament keys whose value is a per-extruder-variant list in OrcaSlicer's
# GUI user filament shape. Derived from `app/normalize.py`'s _DEFAULTS set
# (the canonical per-filament-vector set the slicer normalizes) plus the
# additional per-variant keys observed in working GUI-native exports
# (temperatures, retraction, fan, scarf seam, cooling, plate temps).
#
# This list is intentionally explicit rather than dynamic: per-variant
# detection from the resolved profile alone is unreliable because some
# length-1 list keys are NOT per-variant (e.g. compatible_printers,
# filament_settings_id).
_FILAMENT_PER_VARIANT_KEYS: frozenset[str] = frozenset({
    # From normalize._DEFAULTS (the slicer's per-filament-vector set):
    "activate_chamber_temp_control",
    "adaptive_pressure_advance",
    "adaptive_pressure_advance_bridges",
    "adaptive_pressure_advance_model",
    "adaptive_pressure_advance_overhangs",
    "default_filament_colour",
    "dont_slow_down_outer_wall",
    "enable_overhang_bridge_fan",
    "enable_pressure_advance",
    "filament_colour",
    "filament_cooling_final_speed",
    "filament_cooling_initial_speed",
    "filament_cooling_moves",
    "filament_extruder_variant",
    "filament_ironing_flow",
    "filament_ironing_inset",
    "filament_ironing_spacing",
    "filament_ironing_speed",
    "filament_loading_speed",
    "filament_loading_speed_start",
    "filament_map",
    "filament_multitool_ramming",
    "filament_multitool_ramming_flow",
    "filament_multitool_ramming_volume",
    "filament_notes",
    "filament_ramming_parameters",
    "filament_shrinkage_compensation_z",
    "filament_stamping_distance",
    "filament_stamping_loading_speed",
    "filament_toolchange_delay",
    "filament_unloading_speed",
    "filament_unloading_speed_start",
    "idle_temperature",
    "internal_bridge_fan_speed",
    "ironing_fan_speed",
    "pressure_advance",
    "support_material_interface_fan_speed",
    "textured_cool_plate_temp",
    "textured_cool_plate_temp_initial_layer",
    # Additional per-variant keys observed in working GUI-native exports:
    "nozzle_temperature",
    "nozzle_temperature_initial_layer",
    "filament_max_volumetric_speed",
    "filament_flow_ratio",
    "filament_flush_temp",
    "filament_flush_volumetric_speed",
    "filament_long_retractions_when_cut",
    "filament_retract_before_wipe",
    "filament_retract_lift_above",
    "filament_retract_lift_below",
    "filament_retract_lift_enforce",
    "filament_retract_restart_extra",
    "filament_retract_when_changing_layer",
    "filament_retraction_distances_when_cut",
    "filament_retraction_length",
    "filament_retraction_minimum_travel",
    "filament_retraction_speed",
    "filament_deretraction_speed",
    "filament_wipe",
    "filament_wipe_distance",
    "filament_z_hop",
    "filament_z_hop_types",
    "long_retractions_when_ec",
    "retraction_distances_when_ec",
    "filament_adaptive_volumetric_speed",
    "volumetric_speed_coefficients",
})


def _pad_per_variant_keys(
    profile: dict[str, Any],
    *,
    variant_count: int,
    variant_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Pad every per-variant list key in `profile` to `variant_count`.

    For each key in `_FILAMENT_PER_VARIANT_KEYS` whose current value is
    a list shorter than `variant_count`, extend it by repeating the
    last value (or replicating the only value if length 1). If the
    list already meets or exceeds the target length, leave it.

    `filament_extruder_variant` is special-cased: its slot values are
    variant *labels* (e.g. "Direct Drive Standard"), so when
    `variant_labels` is provided and the key needs padding, the
    label list is substituted whole rather than replicated.

    Mutates `profile` in place and returns it.
    """
    if variant_count <= 1:
        return profile

    for key in _FILAMENT_PER_VARIANT_KEYS:
        value = profile.get(key)
        if not isinstance(value, list):
            continue
        if len(value) >= variant_count:
            continue
        if key == "filament_extruder_variant" and variant_labels is not None:
            profile[key] = list(variant_labels[:variant_count])
            continue
        # Pad by repeating the last value to reach variant_count.
        last = value[-1] if value else ""
        padded = list(value) + [last] * (variant_count - len(value))
        profile[key] = padded
    return profile


# Vendor-profile marker keys stripped on flatten — they identify the
# input as a vendor preset, which OrcaSlicer's GUI uses to gate user
# editing and AMS assignment. User filaments do not carry these.
_FLATTEN_STRIP_KEYS = frozenset({
    "inherits", "base_id", "type", "instantiation", "setting_id",
})


def _longest_word_prefix(strings: list[str]) -> str:
    """Return the longest whitespace-token prefix shared by all strings.

    Returns the empty string when fewer than 2 strings are given, or
    when no tokens are shared at the start. Identical strings return
    their full value (the caller decides how to handle that case).

    Examples::

        >>> _longest_word_prefix(["Bambu Lab A1 mini 0.4 nozzle",
        ...                       "Bambu Lab A1 mini 0.6 nozzle"])
        'Bambu Lab A1 mini'
        >>> _longest_word_prefix(["Bambu Lab A1 mini 0.4 nozzle",
        ...                       "Bambu Lab P1P 0.4 nozzle"])
        'Bambu Lab'
        >>> _longest_word_prefix(["Foo", "Bar"])
        ''
        >>> _longest_word_prefix(["Same", "Same"])
        'Same'
    """
    if len(strings) < 2:
        return ""
    split = [s.split() for s in strings]
    first = split[0]
    prefix_tokens: list[str] = []
    for i, token in enumerate(first):
        if all(len(s) > i and s[i] == token for s in split[1:]):
            prefix_tokens.append(token)
        else:
            break
    return " ".join(prefix_tokens)


def _flatten_user_filament_for_printers(
    resolved: dict[str, Any],
    *,
    printers: list[str],
    name_label: str,
    variant_count: int,
    variant_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Reshape a resolved user filament dict for one or more target printers
    sharing the same extruder variant count.

    `printers` populates `compatible_printers`. Must be non-empty.
    `name_label` is the @<label> suffix of the rewritten name (e.g.
    "Bambu Lab A1 mini" or "Bambu Lab P1P 0.4 nozzle").
    `variant_count` is the count to pad per-variant keys to.
    `variant_labels` (optional) is the printer's variant-label list, used to
    substitute `filament_extruder_variant` rather than replicate.

    Returns a new dict (does not mutate `resolved`).
    """
    out = {k: v for k, v in resolved.items() if k not in _FLATTEN_STRIP_KEYS}
    out["inherits"] = ""

    alias = _filament_alias(str(resolved.get("name", "")))
    new_name = f"{alias} @{name_label}"
    out["name"] = new_name
    out["filament_settings_id"] = [new_name]

    out["compatible_printers"] = list(printers)
    out.setdefault("compatible_printers_condition", "")
    out.setdefault("compatible_prints", [])
    out.setdefault("compatible_prints_condition", "")

    out = _pad_per_variant_keys(
        out, variant_count=variant_count, variant_labels=variant_labels,
    )
    return out


def export_user_filament(
    setting_id: str,
    *,
    shape: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Export a user filament as one or more (filename, dict) entries.

    `shape` must be 'thin' or 'flattened'.

    `thin` → one entry containing the saved file as-is (with `inherits`
    preserved). Does not resolve the chain.

    `flattened` → one entry per compatible printer of the resolved
    filament. Each entry is the OrcaSlicer-GUI-shaped flattened JSON
    for that printer, suitable for AMS assignment.

    Raises:
    - `ProfileNotFoundError` if `setting_id` is unknown or does not
      identify a user-vendor filament.
    - `UnresolvedChainError` if `shape='flattened'` and the inheritance
      chain cannot be resolved.
    - `ValueError` if `shape='flattened'` and the resolved filament
      has empty `compatible_printers`.
    """
    if shape not in ("thin", "flattened"):
        raise ValueError(f"Invalid shape: {shape!r} (expected 'thin' or 'flattened')")

    keys = _setting_id_index.get(setting_id)
    if not keys:
        raise ProfileNotFoundError(
            f"filament profile with id '{setting_id}' not found"
        )

    profile_key = None
    for k in keys:
        if _type_map.get(k) == "filament" and _vendor_map.get(k) == "User":
            profile_key = k
            break
    if profile_key is None:
        raise ProfileNotFoundError(
            f"filament '{setting_id}' is not a user filament"
        )

    raw = _raw_profiles.get(profile_key, {})
    name = str(raw.get("name", setting_id))

    if shape == "thin":
        filename = _safe_filename(name, fallback=setting_id)
        return [(filename, dict(raw))]

    # shape == "flattened"
    try:
        resolved = resolve_profile_by_name(profile_key)
    except ProfileNotFoundError as e:
        raise UnresolvedChainError(str(e)) from e
    if resolved is None:
        raise UnresolvedChainError(
            f"filament '{setting_id}' resolved to None"
        )

    printers = resolved.get("compatible_printers")
    if not isinstance(printers, list) or not printers:
        raise ValueError(
            f"filament '{setting_id}' has empty compatible_printers; "
            f"cannot flatten for GUI export"
        )

    # Group compatible_printers by variant count.
    groups: dict[int, list[str]] = {}
    for p in printers:
        vc = _printer_variant_count(p)
        groups.setdefault(vc, []).append(p)

    alias = _filament_alias(name)
    entries: list[tuple[str, dict[str, Any]]] = []
    for variant_count in sorted(groups.keys()):
        group_printers = groups[variant_count]

        # Look up variant labels via the FIRST printer in the group (all
        # printers in the group share variant_count by construction; we use
        # the first as representative for label lookup).
        #
        # Assumption: all printers in the same variant-count group also
        # share the same `printer_extruder_variant` *labels*. True for the
        # current Bambu Lab fleet (label conventions are vendor-wide). If
        # we ever consolidate printers from different vendors that happen
        # to share a count but not labels (e.g. ["Standard", "High Flow"]
        # vs ["Direct Drive Standard", "Direct Drive High Flow"]), the
        # consolidated file's `filament_extruder_variant` would only match
        # the first printer. Sub-group by `tuple(variant_labels)` here if
        # that case ever surfaces.
        variant_labels = None
        if variant_count > 1:
            machine_key = _select_profile_key_by_name(
                group_printers[0], category="machine"
            )
            if machine_key is not None:
                try:
                    machine_resolved = resolve_profile_by_name(machine_key)
                except ProfileNotFoundError:
                    machine_resolved = None
                if machine_resolved:
                    labels = machine_resolved.get("printer_extruder_variant")
                    if isinstance(labels, list):
                        variant_labels = list(labels)

        # Decide consolidation: if multiple printers share a non-empty
        # word-prefix, emit one file. Otherwise fall back to per-printer.
        if len(group_printers) >= 2:
            prefix = _longest_word_prefix(group_printers)
            if prefix:
                flat = _flatten_user_filament_for_printers(
                    resolved,
                    printers=group_printers,
                    name_label=prefix,
                    variant_count=variant_count,
                    variant_labels=variant_labels,
                )
                filename = _safe_filename(flat["name"], fallback=alias or setting_id)
                entries.append((filename, flat))
                continue

        # Per-printer fallback (single printer in group OR no common prefix).
        for printer_name in group_printers:
            flat = _flatten_user_filament_for_printers(
                resolved,
                printers=[printer_name],
                name_label=printer_name,
                variant_count=variant_count,
                variant_labels=variant_labels,
            )
            filename = _safe_filename(flat["name"], fallback=alias or setting_id)
            entries.append((filename, flat))

    return entries


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
        try:
            resolved = resolve_profile_by_name(profile_key)
        except ProfileNotFoundError as exc:
            logger.warning(
                "Skipping %s '%s' from listing: %s",
                "machine", profile_key, exc,
            )
            continue
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
        try:
            resolved = resolve_profile_by_name(profile_key)
        except ProfileNotFoundError as exc:
            logger.warning(
                "Skipping %s '%s' from listing: %s",
                "process", profile_key, exc,
            )
            continue
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
        try:
            resolved = resolve_profile_by_name(profile_key)
        except ProfileNotFoundError as exc:
            logger.warning(
                "Skipping %s '%s' from listing: %s",
                "filament", profile_key, exc,
            )
            continue
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
