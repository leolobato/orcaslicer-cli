"""Slicing helpers: profile materialisation for the headless binary."""

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from .profiles import (
    get_machine_model_id,
    get_profile,
    get_profile_by_id_or_name,
)

logger = logging.getLogger(__name__)

# API-facing plate type values mapped to OrcaSlicer curr_bed_type labels.
PLATE_TYPE_API_TO_ORCA = {
    "cool_plate": "Cool Plate",
    "engineering_plate": "Engineering Plate",
    "high_temp_plate": "High Temp Plate",
    "textured_pei_plate": "Textured PEI Plate",
    "textured_cool_plate": "Textured Cool Plate",
    "supertack_plate": "Supertack Plate",
}
SUPPORTED_PLATE_TYPES = tuple(PLATE_TYPE_API_TO_ORCA.keys())

# Valid values for parameter overrides
VALID_INFILL_PATTERNS = frozenset({
    "grid", "line", "cubic", "cubicsubdiv", "gyroid", "lightning",
    "honeycomb", "3dhoneycomb", "rectilinear", "monotonic", "monotoniclines",
    "alignedrectilinear", "hilbertcurve", "archimedeanchords",
    "octagramspiral", "supportcubic", "adaptivecubic",
})
VALID_SUPPORT_TYPES = frozenset({"normal", "tree", "none"})
VALID_BRIM_TYPES = frozenset({
    "auto_brim", "outer_only", "inner_only", "outer_and_inner", "no_brim",
})


class ModelTooBigError(Exception):
    pass


class SlicingError(Exception):
    def __init__(
        self,
        message: str,
        orca_output: str | None = None,
        critical_warnings: list[str] | None = None,
    ):
        super().__init__(message)
        self.orca_output = orca_output
        self.critical_warnings = critical_warnings or []


async def materialize_profiles_for_binary(
    machine_id: str,
    process_id: str,
    filament_setting_ids: list[str],
) -> dict[str, Any]:
    """Resolve profile inheritance and write flattened JSONs the binary can load.

    Returns:
      - "machine":  absolute path to the resolved machine profile JSON
      - "process":  absolute path to the resolved process profile JSON
      - "filaments": list of absolute paths to resolved filament profile JSONs
      - "printer_model_id": BBL ``model_id`` for the machine (e.g. ``"N1"``),
        or ``""`` for vendors that don't declare one. Stamped onto
        ``slice_info.config[printer_model_id]`` by the binary so consumers
        can identify the target physical printer.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="orca-headless-profiles-"))
    machine = get_profile("machine", machine_id)
    process = get_profile("process", process_id)
    machine_path = tmp_dir / "machine.json"
    process_path = tmp_dir / "process.json"
    machine_path.write_text(json.dumps(machine))
    process_path.write_text(json.dumps(process))

    filament_paths: list[str] = []
    filament_names: list[str] = []
    for i, fid in enumerate(filament_setting_ids):
        fcfg = get_profile_by_id_or_name("filament", fid)
        fpath = tmp_dir / f"filament-{i}.json"
        fpath.write_text(json.dumps(fcfg))
        filament_paths.append(str(fpath))
        # The 3MF stores per-slot filament selections as display names
        # (e.g. "Bambu PLA Basic @BBL A1M"), not setting_ids. The binary's
        # per-filament-slot name guard for project overrides compares
        # against those, so forward the display name rather than the slug.
        filament_names.append(fcfg.get("name", fid))

    return {
        "machine": str(machine_path),
        "process": str(process_path),
        "filaments": filament_paths,
        "filament_names": filament_names,
        "printer_model_id": get_machine_model_id(machine_id),
    }
