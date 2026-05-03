"""Assemble the JSON payload for ``GET /3mf/{token}/inspect``.

This module owns the cheap-side reads (ZIP + XML/JSON parsing) only.
Any data that requires libslic3r (notably ``use_set_per_plate`` for
un-sliced 3MFs) is plumbed in by the endpoint layer, not here.
"""
from __future__ import annotations

import io
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from typing import Any

from .threemf import get_bounding_box, get_plate_count, get_used_filament_slots

logger = logging.getLogger(__name__)

# Bump when the response shape changes in a way that should invalidate
# any in-memory cache the endpoint layer keeps.
INSPECT_SCHEMA_VERSION = 2


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


def _parse_per_plate_slice_info(slice_info_xml: str) -> dict[int, dict]:
    """Parse per-plate metadata from slice_info.config XML.

    Returns a dict keyed by plate id (1-based) with each plate's
    estimate, support_used, label_object_enabled, nozzle_diameters,
    filament_maps, limit_filament_maps, outside, and warnings.
    """
    result: dict[int, dict] = {}
    try:
        root = ET.fromstring(slice_info_xml)
    except ET.ParseError:
        logger.warning("inspect: failed to parse slice_info.config as XML")
        return result

    for plate_el in root.findall("plate"):
        meta: dict[str, str] = {}
        for m in plate_el.findall("metadata"):
            k = m.get("key", "")
            v = m.get("value", "")
            if k:
                meta[k] = v

        try:
            plate_id = int(meta.get("index", "0"))
        except ValueError:
            plate_id = 0
        if plate_id <= 0:
            continue

        # Parse filament used_m sorted by id
        filament_entries: list[tuple[int, float]] = []
        for fil_el in plate_el.findall("filament"):
            try:
                fid = int(fil_el.get("id", "0"))
                used_m = float(fil_el.get("used_m", "0"))
                filament_entries.append((fid, used_m))
            except ValueError:
                pass
        filament_entries.sort(key=lambda x: x[0])
        filament_used_m = [v for _, v in filament_entries]

        # Parse estimate fields (defensive on empty strings)
        def _float_or_none(s: str) -> float | None:
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        time_seconds = _float_or_none(meta.get("prediction", ""))
        weight_g = _float_or_none(meta.get("weight", ""))
        first_layer_time = _float_or_none(meta.get("first_layer_time", ""))

        estimate: dict[str, Any] | None = None
        if time_seconds is not None and weight_g is not None:
            estimate = {
                "time_seconds": time_seconds,
                "weight_g": weight_g,
                "first_layer_time": first_layer_time,
                "filament_used_m": filament_used_m,
            }

        # Parse boolean fields
        def _bool_or_none(s: str) -> bool | None:
            if s == "true":
                return True
            if s == "false":
                return False
            return None

        support_used = _bool_or_none(meta.get("support_used", ""))
        label_object_enabled = _bool_or_none(meta.get("label_object_enabled", ""))
        outside = _bool_or_none(meta.get("outside", ""))

        # Raw string fields
        nozzle_diameters = meta.get("nozzle_diameters") or None
        filament_maps = meta.get("filament_maps") or None
        limit_filament_maps = meta.get("limit_filament_maps") or None

        # Parse warnings
        warnings: list[dict[str, Any]] = []
        for w_el in plate_el.findall("warning"):
            msg = w_el.get("msg", "")
            level_str = w_el.get("level", "")
            error_code = w_el.get("error_code", "")
            try:
                level = int(level_str)
            except ValueError:
                level = 0
            warnings.append({"msg": msg, "level": level, "error_code": error_code})

        result[plate_id] = {
            "estimate": estimate,
            "support_used": support_used,
            "label_object_enabled": label_object_enabled,
            "nozzle_diameters": nozzle_diameters,
            "filament_maps": filament_maps,
            "limit_filament_maps": limit_filament_maps,
            "outside": outside,
            "warnings": warnings,
        }

    return result


def _parse_plate_names(zf: zipfile.ZipFile) -> dict[int, str]:
    """Parse plate names from model_settings.config.

    Returns a dict mapping plate id (1-based) to plater_name string.
    """
    if "Metadata/model_settings.config" not in zf.namelist():
        return {}
    try:
        xml_bytes = zf.read("Metadata/model_settings.config")
    except KeyError:
        return {}
    try:
        root = ET.fromstring(xml_bytes.decode())
    except ET.ParseError:
        logger.warning("inspect: failed to parse model_settings.config as XML")
        return {}

    names: dict[int, str] = {}
    for plate_el in root.findall("plate"):
        plate_id: int | None = None
        plate_name: str = ""
        for m in plate_el.findall("metadata"):
            k = m.get("key", "")
            v = m.get("value", "")
            if k == "plater_id":
                try:
                    plate_id = int(v)
                except ValueError:
                    pass
            elif k == "plater_name":
                plate_name = v
        if plate_id is not None:
            names[plate_id] = plate_name

    return names


def _parse_plates(
    zf: zipfile.ZipFile,
    file_bytes: bytes,
    per_plate_slice_info: dict[int, dict],
    plate_names: dict[int, str],
) -> list[dict[str, Any]]:
    plate_count = get_plate_count(file_bytes)
    plates: list[dict[str, Any]] = []
    for i in range(1, max(1, plate_count) + 1):
        used = get_used_filament_slots(file_bytes, plate=i)
        slice_data = per_plate_slice_info.get(i, {})

        plate: dict[str, Any] = {
            "id": i,
            "name": plate_names.get(i, ""),
            # `None` means "the slice metadata didn't say" — for un-sliced
            # 3MFs this gets filled in later by `orca-headless use-set`.
            "used_filament_indices": sorted(used) if used is not None else None,
            "estimate": slice_data.get("estimate"),
            "support_used": slice_data.get("support_used"),
            "label_object_enabled": slice_data.get("label_object_enabled"),
            "nozzle_diameters": slice_data.get("nozzle_diameters"),
            "filament_maps": slice_data.get("filament_maps"),
            "limit_filament_maps": slice_data.get("limit_filament_maps"),
            "outside": slice_data.get("outside"),
            "warnings": slice_data.get("warnings", []),
        }
        plates.append(plate)
    return plates


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
        # Parse filament tags directly via ElementTree for consistency,
        # but the regex approach also works fine here; keep it as-is.
        try:
            root = ET.fromstring(slice_info_xml)
        except ET.ParseError:
            root = None

        sliced: list[dict[str, Any]] = []
        if root is not None:
            for plate_el in root.findall("plate"):
                for fil_el in plate_el.findall("filament"):
                    try:
                        fid = int(fil_el.get("id", "0"))
                    except ValueError:
                        continue
                    sliced.append({
                        "slot": fid - 1,
                        "type": fil_el.get("type", ""),
                        "color": fil_el.get("color", ""),
                        "filament_id": fil_el.get("tray_info_idx", ""),
                        "settings_id": "",
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

        # Parse per-plate data from slice_info.config (sliced only)
        per_plate_slice_info: dict[int, dict] = {}
        if slice_info_xml is not None:
            per_plate_slice_info = _parse_per_plate_slice_info(slice_info_xml)

        # Parse plate names from model_settings.config (always)
        plate_names = _parse_plate_names(zf)

        out["plates"] = _parse_plates(zf, file_bytes, per_plate_slice_info, plate_names)
        out["filaments"] = _parse_filaments(project_settings, slice_info_xml)
        out["printer_model"] = project_settings.get("printer_model", "")
        out["printer_variant"] = project_settings.get("printer_variant", "")
        out["curr_bed_type"] = project_settings.get("curr_bed_type", "")

        # Back-compat: global estimate = plate 1's estimate (without first_layer_time)
        plate_1_data = per_plate_slice_info.get(1, {})
        plate_1_estimate = plate_1_data.get("estimate")
        if plate_1_estimate is not None:
            out["estimate"] = {
                "time_seconds": plate_1_estimate["time_seconds"],
                "weight_g": plate_1_estimate["weight_g"],
                "filament_used_m": plate_1_estimate["filament_used_m"],
            }

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
