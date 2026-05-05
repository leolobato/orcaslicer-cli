"""Slicing helpers: profile materialisation for the headless binary."""

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from .inspect import parse_inspect_data
from .profiles import (
    ProfileNotFoundError,
    _safe_filename,
    get_machine_model_id,
    get_profile,
    get_profile_by_id_or_name,
    iter_inheritance_chain,
    resolve_profile_by_name,
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

# Filament keys OrcaSlicer declares as `coStrings` (string vectors) but the GUI
# sometimes exports as a bare scalar — wrapping at write time avoids the
# `set_at(): Assigning from an empty vector` SIGABRT when the loader sees a
# scalar `""`.
_FILAMENT_VECTOR_STRING_KEYS = frozenset({"filament_notes"})


def _normalize_filament_for_write(profile: dict[str, Any]) -> dict[str, Any]:
    """Defensive shape/metadata fixes for a filament profile about to be written.

    Wraps scalar values into single-element lists for known coStrings keys,
    and defaults `type`/`from` (user-imported profiles often omit them, which
    causes the loader to reject the JSON with `unknown config type`).
    """
    out = dict(profile)
    for key in _FILAMENT_VECTOR_STRING_KEYS:
        val = out.get(key)
        if isinstance(val, str):
            out[key] = [val]
    out.setdefault("type", "filament")
    out.setdefault("from", "system")
    return out


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


class IncompatibleFilamentError(Exception):
    """Raised when a filament profile isn't compatible with the target machine.

    The headless binary's ``construct_full_config`` SIGSEGVs when a filament
    profile is incompatible with the resolved machine (e.g. an A1 mini machine
    paired with a P2S filament): the per-key vector merge dereferences keys
    that don't exist in the foreign filament's config. Surfacing this as a
    400 *before* invoking the binary keeps the failure actionable instead of
    landing as ``exit -11`` with no context.
    """

    def __init__(self, mismatches: list[dict[str, Any]]):
        self.mismatches = mismatches
        msg = "; ".join(
            f"slot {m['slot']} filament {m['filament']!r} is only compatible with "
            f"{m['compatible_printers']!r} (machine {m['machine']!r})"
            for m in mismatches
        )
        super().__init__(msg)


def _filament_compat_mismatches(
    machine_name: str,
    filament_paths: list[str],
    filament_names: list[str],
) -> list[dict[str, Any]]:
    """Return per-slot compatibility mismatches against ``machine_name``.

    A filament is treated as compatible when its ``compatible_printers`` list
    is missing/empty (meaning "applies anywhere") or contains the resolved
    machine's display name. Anything else is a mismatch.
    """
    mismatches: list[dict[str, Any]] = []
    for slot, (path, name) in enumerate(zip(filament_paths, filament_names)):
        try:
            cfg = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        compat = cfg.get("compatible_printers") or []
        if not compat:
            continue
        if machine_name in compat:
            continue
        mismatches.append({
            "slot": slot,
            "filament": name,
            "compatible_printers": compat,
            "machine": machine_name,
        })
    return mismatches


def _write_chain_link(
    out_dir: Path,
    name: str,
    *,
    is_filament: bool,
    written_names: set[str],
) -> None:
    """Write one chain link's flat config to a unique JSON inside ``out_dir``.

    Idempotent across slots: if ``name`` was already written (typical when
    multiple filament slots share an ancestor), skip. ``inherits`` is
    preserved on disk so ``PresetCollection::get_selected_preset_parent``
    can chase the chain at slice time — that's how the bundle reconstructs
    "what was customized" via ``dirty_options_without_option_list``.
    """
    if name in written_names:
        return
    flat = resolve_profile_by_name(name)
    if flat is None:
        raise ProfileNotFoundError(
            f"Inheritance chain link '{name}' could not be resolved"
        )
    payload = dict(flat)
    payload.setdefault("name", name)
    if is_filament:
        payload = _normalize_filament_for_write(payload)
    fname = _safe_filename(name, fallback=f"link-{len(written_names)}") + ".json"
    (out_dir / fname).write_text(json.dumps(payload))
    written_names.add(name)


async def materialize_profiles_for_binary(
    machine_id: str,
    process_id: str,
    filament_setting_ids: list[str],
) -> dict[str, Any]:
    """Resolve profile inheritance and write per-category chain dirs the binary can load.

    Each category gets a directory with one JSON per link in the
    inheritance chain (leaf + every ancestor). The binary feeds them
    into a real ``PresetBundle`` via ``PresetCollection::load_preset``,
    then ``select_preset_by_name`` on the leaf — libslic3r handles
    parent lookup for ``get_selected_preset_parent`` /
    ``dirty_options_without_option_list``.

    Why chains instead of single flat JSONs: the bundle's
    ``load_project_embedded_presets`` (called later from the 3MF's
    project-local filament variants) does
    ``preset->config = inherit_preset->config`` then overlays the
    project-local deltas (Preset.cpp:1559-1572). For that to work, the
    parent must already be in the same collection.

    Returns:
      - ``machine_chain_dir`` / ``machine_leaf_name``
      - ``process_chain_dir`` / ``process_leaf_name``
      - ``filament_chain_dir`` (one shared dir, dedup'd across slots)
      - ``filament_leaf_names``: list of leaf preset names per slot
      - ``printer_model_id``: BBL ``model_id`` for the machine (e.g. ``"N1"``)
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="orca-headless-profiles-"))
    machine_dir = tmp_dir / "machine"
    process_dir = tmp_dir / "process"
    filament_dir = tmp_dir / "filaments"
    for d in (machine_dir, process_dir, filament_dir):
        d.mkdir(parents=True, exist_ok=True)

    # --- machine chain ---
    machine = get_profile("machine", machine_id)
    machine_leaf_name = machine.get("name", machine_id)
    machine_written: set[str] = set()
    for link_name, _raw in iter_inheritance_chain(machine_leaf_name):
        _write_chain_link(machine_dir, link_name, is_filament=False, written_names=machine_written)

    # --- process chain ---
    process = get_profile("process", process_id)
    process_leaf_name = process.get("name", process_id)
    process_written: set[str] = set()
    for link_name, _raw in iter_inheritance_chain(process_leaf_name):
        _write_chain_link(process_dir, link_name, is_filament=False, written_names=process_written)

    # --- filament chains (shared dir, dedup'd by name across slots) ---
    filament_leaf_names: list[str] = []
    filament_written: set[str] = set()
    for fid in filament_setting_ids:
        fcfg = get_profile_by_id_or_name("filament", fid)
        leaf_name = fcfg.get("name", fid)
        filament_leaf_names.append(leaf_name)
        for link_name, _raw in iter_inheritance_chain(leaf_name):
            _write_chain_link(
                filament_dir, link_name, is_filament=True, written_names=filament_written,
            )

    # Compatibility check uses the leaf filament's flat config. Read each
    # leaf's written JSON for the check rather than re-resolving.
    filament_leaf_paths = [
        str(filament_dir / (_safe_filename(name, fallback="leaf") + ".json"))
        for name in filament_leaf_names
    ]
    mismatches = _filament_compat_mismatches(
        machine_leaf_name,
        filament_leaf_paths,
        filament_leaf_names,
    )
    if mismatches:
        raise IncompatibleFilamentError(mismatches)

    return {
        "machine_chain_dir": str(machine_dir),
        "machine_leaf_name": machine_leaf_name,
        "process_chain_dir": str(process_dir),
        "process_leaf_name": process_leaf_name,
        "filament_chain_dir": str(filament_dir),
        "filament_leaf_names": filament_leaf_names,
        "printer_model_id": get_machine_model_id(machine_id),
    }


def validate_3mf_preset_references(file_bytes: bytes) -> list[dict[str, str]]:
    """Return 3MF-referenced presets that don't resolve in the catalog.

    The GUI runs ``PresetBundle::validate_presets``
    (``vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:1260``) on every
    3MF load, surfacing a "preset not found" error when the file
    references a printer/process/filament name we don't have. Our binary
    skips that check because the Python service pre-resolves
    ``inherits`` before writing the temp JSONs — but the *3MF's* own
    ``printer_settings_id`` / ``print_settings_id`` /
    ``filament_settings_id`` strings can still name a preset the
    catalog has since renamed or removed. When that happens the binary
    silently slices with the resolved fallback (the request's
    ``machine_id`` / ``process_id`` / ``filament_settings_ids``) and
    the user never learns the file's authored intent didn't survive.

    Each entry: ``{"category", "name"}``. Empty list = all references
    resolve. Best-effort only — never raises (a malformed 3MF returns
    no findings rather than blocking the slice).
    """
    findings: list[dict[str, str]] = []
    try:
        info = parse_inspect_data(file_bytes)
    except Exception:
        # Inspector is best-effort itself; stay quiet.
        return findings

    # Display-name fields from project_settings.config.
    candidates: list[tuple[str, str]] = []
    if printer := str(info.get("printer_settings_id") or "").strip():
        candidates.append(("machine", printer))
    if process := str(info.get("print_settings_id") or "").strip():
        candidates.append(("process", process))
    seen_filaments: set[str] = set()
    for f in info.get("filaments") or []:
        name = str(f.get("settings_id") or "").strip()
        if name and name not in seen_filaments:
            seen_filaments.add(name)
            candidates.append(("filament", name))

    for category, name in candidates:
        try:
            get_profile_by_id_or_name(category, name)
        except ProfileNotFoundError:
            findings.append({"category": category, "name": name})

    return findings
