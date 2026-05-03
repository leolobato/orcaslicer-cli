"""Assemble the JSON payload for ``GET /3mf/{token}/inspect``.

This module owns the cheap-side reads (ZIP + XML/JSON parsing) only.
Any data that requires libslic3r (notably ``use_set_per_plate`` for
un-sliced 3MFs) is plumbed in by the endpoint layer, not here.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from collections import OrderedDict
from typing import Any

from .threemf import get_bounding_box, get_plate_count, get_used_filament_slots

logger = logging.getLogger(__name__)

# Bump when the response shape changes in a way that should invalidate
# any in-memory cache the endpoint layer keeps.
INSPECT_SCHEMA_VERSION = 1


def _read_project_settings(zf: zipfile.ZipFile) -> dict[str, Any]:
    if "Metadata/project_settings.config" not in zf.namelist():
        return {}
    try:
        return json.loads(zf.read("Metadata/project_settings.config").decode())
    except (json.JSONDecodeError, KeyError):
        return {}


def _read_slice_info(zf: zipfile.ZipFile) -> str | None:
    """Return the slice_info XML only when it contains real slice data.

    OrcaSlicer saves a stub ``slice_info.config`` (header only, no
    ``<plate>`` children) even for un-sliced project files.  We treat
    such stubs as absent so ``is_sliced`` stays ``False``.
    """
    if "Metadata/slice_info.config" not in zf.namelist():
        return None
    try:
        xml = zf.read("Metadata/slice_info.config").decode()
    except KeyError:
        return None
    # Only count as sliced if there's at least one <plate> element.
    if "<plate>" not in xml:
        return None
    return xml


def _parse_plates(zf: zipfile.ZipFile, file_bytes: bytes) -> list[dict[str, Any]]:
    plate_count = get_plate_count(file_bytes)
    plates: list[dict[str, Any]] = []
    for i in range(1, max(1, plate_count) + 1):
        used = get_used_filament_slots(file_bytes, plate=i)
        plates.append({
            "id": i,
            # `None` means "the slice metadata didn't say" — for un-sliced
            # 3MFs this gets filled in later by `orca-headless use-set`.
            "used_filament_indices": sorted(used) if used is not None else None,
        })
    return plates


_FILAMENT_TAG_RE = re.compile(
    r'<filament\s+id="(?P<id>\d+)"\s+'
    r'tray_info_idx="(?P<tray>[^"]*)"\s+'
    r'type="(?P<type>[^"]*)"\s+'
    r'color="(?P<color>[^"]*)"\s+'
    r'used_m="(?P<used_m>[^"]*)"\s+'
    r'used_g="(?P<used_g>[^"]*)"',
)


def _parse_estimate(slice_info_xml: str) -> dict[str, Any] | None:
    """Parse the global plate-1 estimate from `slice_info.config`."""
    pred_m = re.search(r'key="prediction"\s+value="([^"]+)"', slice_info_xml)
    weight_m = re.search(r'key="weight"\s+value="([^"]+)"', slice_info_xml)
    if not pred_m or not weight_m:
        return None
    filaments_used: list[float] = []
    for fm in _FILAMENT_TAG_RE.finditer(slice_info_xml):
        try:
            filaments_used.append(float(fm.group("used_m")))
        except ValueError:
            pass
    try:
        return {
            "time_seconds": float(pred_m.group(1)),
            "weight_g": float(weight_m.group(1)),
            "filament_used_m": filaments_used,
        }
    except ValueError:
        return None


def _parse_filaments(
    project_settings: dict[str, Any],
    slice_info_xml: str | None,
) -> list[dict[str, Any]]:
    """Per-slot filament records.

    Sources, in order of preference:
    - When the 3MF is sliced, `slice_info.config` carries the authoritative
      per-slot ``tray_info_idx`` / ``type`` / ``color`` (these are what
      the printer will actually print with).
    - Otherwise fall back to ``project_settings.config`` per-slot vectors
      (``filament_settings_id``, ``filament_colour``, ``filament_ids``,
      ``filament_type``).
    """
    if slice_info_xml is not None:
        sliced = []
        for m in _FILAMENT_TAG_RE.finditer(slice_info_xml):
            sliced.append({
                "slot": int(m.group("id")) - 1,
                "type": m.group("type"),
                "color": m.group("color"),
                "filament_id": m.group("tray"),
                "settings_id": "",  # not present in slice_info; settings_id only lives in project_settings.
            })
        if sliced:
            # Project-side settings_id may still be useful; merge by slot.
            settings_ids = project_settings.get("filament_settings_id") or []
            for entry in sliced:
                if entry["slot"] < len(settings_ids):
                    entry["settings_id"] = settings_ids[entry["slot"]]
            return sliced

    settings_ids = project_settings.get("filament_settings_id") or []
    colors = project_settings.get("filament_colour") or []
    ids = project_settings.get("filament_ids") or []
    types = project_settings.get("filament_type") or []
    n = max(len(settings_ids), len(colors), len(ids), len(types))
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append({
            "slot": i,
            "type": types[i] if i < len(types) else "",
            "color": colors[i] if i < len(colors) else "",
            "filament_id": ids[i] if i < len(ids) else "",
            "settings_id": settings_ids[i] if i < len(settings_ids) else "",
        })
    return out


def parse_inspect_data(file_bytes: bytes) -> dict[str, Any]:
    """Inspect a 3MF byte blob and return a structured summary.

    Pure read — never touches libslic3r. The endpoint layer fills in
    ``use_set_per_plate`` (via `orca-headless use-set`) for un-sliced
    3MFs and adds ``thumbnail_urls``.
    """
    out: dict[str, Any] = {
        "schema_version": INSPECT_SCHEMA_VERSION,
        "is_sliced": False,
        "plate_count": 0,
        "plates": [],
        "filaments": [],
        "estimate": None,
        "bbox": None,
        "printer_model": "",
        "printer_variant": "",
        "curr_bed_type": "",
    }

    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        logger.warning("inspect: not a zip file")
        return out

    with zf:
        slice_info_xml = _read_slice_info(zf)
        out["is_sliced"] = slice_info_xml is not None
        project_settings = _read_project_settings(zf)
        out["plate_count"] = get_plate_count(file_bytes)
        out["plates"] = _parse_plates(zf, file_bytes)
        out["filaments"] = _parse_filaments(project_settings, slice_info_xml)
        out["printer_model"] = project_settings.get("printer_model", "")
        out["printer_variant"] = project_settings.get("printer_variant", "")
        out["curr_bed_type"] = project_settings.get("curr_bed_type", "")

        if slice_info_xml is not None:
            out["estimate"] = _parse_estimate(slice_info_xml)

    bbox = get_bounding_box(file_bytes)
    if bbox is not None:
        out["bbox"] = {
            "min_x": bbox.min_x, "min_y": bbox.min_y, "min_z": bbox.min_z,
            "max_x": bbox.max_x, "max_y": bbox.max_y, "max_z": bbox.max_z,
        }

    return out


class InspectCache:
    """In-memory cache of assembled inspect responses.

    Keyed by ``(sha256, INSPECT_SCHEMA_VERSION)``. Bumping the schema
    version invalidates every cached entry without touching the token
    cache that stores the actual 3MF bytes.

    Capped at 256 entries (LRU eviction). Inspect responses are small
    (low single-digit KB each) so this is generous.
    """
    MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._entries: "OrderedDict[tuple[str, int], dict[str, Any]]" = OrderedDict()

    def get(self, sha256: str) -> dict[str, Any] | None:
        key = (sha256, INSPECT_SCHEMA_VERSION)
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, sha256: str, value: dict[str, Any]) -> None:
        key = (sha256, INSPECT_SCHEMA_VERSION)
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self.MAX_ENTRIES:
            self._entries.popitem(last=False)

    def invalidate(self, sha256: str) -> None:
        for ver in list({k[1] for k in self._entries if k[0] == sha256}):
            self._entries.pop((sha256, ver), None)
