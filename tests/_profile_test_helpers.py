"""Shared helpers for tests that exercise app.profiles directly."""

from app import profiles


def reset_profiles_state() -> None:
    """Clear all module-level dicts in app.profiles to a known empty state."""
    profiles._raw_profiles.clear()
    profiles._type_map.clear()
    profiles._vendor_map.clear()
    profiles._name_index.clear()
    profiles._resolved_cache.clear()
    profiles._setting_id_index.clear()
