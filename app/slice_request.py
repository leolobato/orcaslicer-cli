"""Helpers for parsing slice endpoint filament selection payloads."""

from __future__ import annotations

import io
import json
import zipfile


def extract_project_filament_profile_ids(file_bytes: bytes) -> list[str]:
    """Read filament_settings_id from the input 3MF project settings."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
            raw = zf.read("Metadata/project_settings.config").decode()
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return []

    try:
        settings = json.loads(raw)
    except json.JSONDecodeError:
        return []

    raw_ids = settings.get("filament_settings_id", [])
    if not isinstance(raw_ids, list):
        return []
    return [str(item) for item in raw_ids]


def parse_filament_profile_ids(
    filament_profiles: str,
    file_bytes: bytes,
) -> tuple[list[str] | None, str | None]:
    """Parse legacy list or tray-aware project filament assignments."""
    try:
        payload = json.loads(filament_profiles)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, list):
        if not all(isinstance(item, str) for item in payload):
            return None, "filament_profiles list values must be strings"
        return payload, None

    if not isinstance(payload, dict):
        return None, (
            "filament_profiles must be either a JSON list of setting_id strings "
            "or a JSON object mapping project filament indexes to strings or "
            "{profile_setting_id, tray_slot} objects"
        )

    filament_ids = extract_project_filament_profile_ids(file_bytes)
    if not filament_ids:
        return None, (
            "filament_profiles object format requires input 3MF project "
            "filament_settings_id entries"
        )

    for slot_str, selection in payload.items():
        try:
            idx = int(slot_str)
        except (TypeError, ValueError):
            return None, f"Invalid project filament index: {slot_str!r}"

        if idx < 0 or idx >= len(filament_ids):
            return None, (
                f"Project filament index {idx} out of range for "
                f"{len(filament_ids)} project filament(s)"
            )

        if isinstance(selection, str):
            profile_setting_id = selection.strip()
        elif isinstance(selection, dict):
            profile_setting_id = str(selection.get("profile_setting_id", "")).strip()
            tray_slot = selection.get("tray_slot")
            if tray_slot is not None and not isinstance(tray_slot, int):
                return None, f"tray_slot for project filament {idx} must be an integer"
        else:
            return None, (
                f"Project filament {idx} selection must be a setting_id string "
                "or an object with profile_setting_id"
            )

        if not profile_setting_id:
            return None, f"Missing profile_setting_id for project filament {idx}"
        filament_ids[idx] = profile_setting_id

    return filament_ids, None
