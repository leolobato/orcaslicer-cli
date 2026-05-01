"""Parse 3MF archives to extract object bounding boxes."""

import io
import logging
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}

_IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class BBox(NamedTuple):
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def size_x(self) -> float:
        return self.max_x - self.min_x

    @property
    def size_y(self) -> float:
        return self.max_y - self.min_y

    @property
    def size_z(self) -> float:
        return self.max_z - self.min_z


def _parse_transform(attr: str | None) -> list[float]:
    if attr is None:
        return list(_IDENTITY)
    return [float(v) for v in attr.strip().split()]


def _apply_transform(
    x: float, y: float, z: float, t: list[float],
) -> tuple[float, float, float]:
    m00, m01, m02, m10, m11, m12, m20, m21, m22, m30, m31, m32 = t
    return (
        m00 * x + m10 * y + m20 * z + m30,
        m01 * x + m11 * y + m21 * z + m31,
        m02 * x + m12 * y + m22 * z + m32,
    )


def _chain_transforms(a: list[float], b: list[float]) -> list[float]:
    """Multiply two row-major 3MF affine transforms (12 floats each)."""
    result = [0.0] * 12
    for r in range(3):
        for c in range(3):
            result[r * 3 + c] = (
                a[r * 3 + 0] * b[0 * 3 + c]
                + a[r * 3 + 1] * b[1 * 3 + c]
                + a[r * 3 + 2] * b[2 * 3 + c]
            )
    for c in range(3):
        result[9 + c] = (
            a[9] * b[0 * 3 + c]
            + a[10] * b[1 * 3 + c]
            + a[11] * b[2 * 3 + c]
            + b[9 + c]
        )
    return result


def _collect_vertices_recursive(
    obj_elem: ET.Element,
    transform: list[float],
    objects: dict[str, ET.Element],
    zf: zipfile.ZipFile | None = None,
    printable_objectids: set[str] | None = None,
) -> list[tuple[float, float, float]]:
    """Collect transformed vertices from an object, following component refs and sub-models.

    When ``printable_objectids`` is provided, components whose ``objectid`` is not in
    the set are skipped — used to exclude modifier/support/negative volumes from the
    build-volume bounding box. The filter applies only at this call's component level;
    deeper recursion does not propagate it.
    """
    ns_p = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    points = []

    mesh = obj_elem.find("m:mesh", _NS)
    if mesh is not None:
        for v in mesh.findall("m:vertices/m:vertex", _NS):
            x = float(v.get("x"))
            y = float(v.get("y"))
            z = float(v.get("z"))
            points.append(_apply_transform(x, y, z, transform))

    for comp in obj_elem.findall("m:components/m:component", _NS):
        comp_obj_id = comp.get("objectid")
        if printable_objectids is not None and comp_obj_id not in printable_objectids:
            continue
        comp_transform = _parse_transform(comp.get("transform"))
        # Vertex flows: local space → component transform → parent's build
        # transform → world. So the combined map is "comp first, then build" —
        # `_chain_transforms(comp, build)`, NOT the other way around.
        combined = _chain_transforms(comp_transform, transform)
        path = comp.get(f"{{{ns_p}}}path")

        if path and zf:
            # Sub-model file reference
            zip_path = path.lstrip("/")
            try:
                sub_data = zf.read(zip_path).decode()
                sub_root = ET.fromstring(sub_data)
                sub_objects = {
                    o.get("id"): o
                    for o in sub_root.findall(".//m:resources/m:object", _NS)
                }
                ref_obj = sub_objects.get(comp_obj_id)
                if ref_obj is not None:
                    points.extend(_collect_vertices_recursive(
                        ref_obj, combined, sub_objects, zf,
                    ))
            except (KeyError, ET.ParseError):
                pass
        else:
            ref_obj = objects.get(comp_obj_id)
            if ref_obj is not None:
                points.extend(_collect_vertices_recursive(
                    ref_obj, combined, objects, zf,
                ))

    return points


def _read_printable_objectids(
    zf: zipfile.ZipFile,
) -> dict[str, set[str]] | None:
    """Read ``Metadata/model_settings.config`` and return parent-objectid → printable part ids.

    OrcaSlicer stores per-volume metadata in this file as ``<part id="N" subtype="...">``
    inside ``<object id="M">``; the ``id`` attribute matches the ``objectid`` of the
    corresponding ``<component>`` in the 3D model XML (see OrcaSlicer
    ``src/libslic3r/Format/bbs_3mf.cpp:_handle_start_config_volume``). Only
    ``normal_part`` is printable geometry; ``modifier_part``, ``negative_part``,
    ``support_enforcer`` and ``support_blocker`` are non-printing region markers and
    are routinely scaled to extreme dimensions, so they must be excluded from the
    build-volume bounding box.

    Returns ``None`` if the config is missing or unparseable, signalling callers to
    fall back to including every component (preserves prior behavior for legacy 3MFs
    without per-part metadata).
    """
    try:
        if "Metadata/model_settings.config" not in zf.namelist():
            return None
        raw = zf.read("Metadata/model_settings.config").decode()
        root = ET.fromstring(raw)
    except (KeyError, ET.ParseError, UnicodeDecodeError):
        return None

    result: dict[str, set[str]] = {}
    for obj in root.findall("object"):
        obj_id = obj.get("id")
        if obj_id is None:
            continue
        printable: set[str] = set()
        for part in obj.findall("part"):
            part_id = part.get("id")
            if part_id is None:
                continue
            if part.get("subtype", "normal_part") == "normal_part":
                printable.add(part_id)
        result[obj_id] = printable
    return result


def get_bounding_box(file_bytes: bytes) -> BBox | None:
    """Return the overall bounding box of all build items in a 3MF file.

    Follows component references to sub-model files.
    Returns None if no geometry is found.
    """
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        model_path = None
        for name in zf.namelist():
            if name == "3D/3dmodel.model":
                model_path = name
                break
        if model_path is None:
            # Fallback: first .model file
            for name in zf.namelist():
                if name.lower().endswith(".model"):
                    model_path = name
                    break
        if model_path is None:
            return None

        tree = ET.parse(zf.open(model_path))
        root = tree.getroot()

        objects: dict[str, ET.Element] = {}
        for obj in root.findall(".//m:resources/m:object", _NS):
            objects[obj.get("id")] = obj

        printable_per_object = _read_printable_objectids(zf)

        all_points: list[tuple[float, float, float]] = []
        for item in root.findall("m:build/m:item", _NS):
            obj_id = item.get("objectid")
            obj_elem = objects.get(obj_id)
            if obj_elem is None:
                continue
            item_transform = _parse_transform(item.get("transform"))
            printable = (
                printable_per_object.get(obj_id)
                if printable_per_object is not None
                else None
            )
            all_points.extend(_collect_vertices_recursive(
                obj_elem, item_transform, objects, zf,
                printable_objectids=printable,
            ))

    if not all_points:
        return None

    xs = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]
    zs = [p[2] for p in all_points]
    return BBox(
        min_x=min(xs), min_y=min(ys), min_z=min(zs),
        max_x=max(xs), max_y=max(ys), max_z=max(zs),
    )


def get_build_volume(machine_profile: dict[str, Any]) -> tuple[float, float, float] | None:
    """Extract (width, depth, height) from a machine profile's printable_area and printable_height.

    printable_area is a list like ["0x0", "256x0", "256x256", "0x256"].
    Returns None if the fields are missing.
    """
    area = machine_profile.get("printable_area")
    height = machine_profile.get("printable_height")
    if not area or not height:
        return None

    xs = []
    ys = []
    for coord in area:
        parts = str(coord).split("x")
        if len(parts) == 2:
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))

    if not xs or not ys:
        return None

    width = max(xs) - min(xs)
    depth = max(ys) - min(ys)
    return (width, depth, float(height))


class FitCheck(NamedTuple):
    fits: bool
    needs_arrange: bool
    error: str | None


def validate_model_fits(
    file_bytes: bytes, machine_profile: dict[str, Any],
) -> FitCheck:
    """Check that the 3MF model fits within the machine's build volume.

    Returns a FitCheck indicating whether the model fits, needs rearranging,
    or is too large entirely.
    """
    volume = get_build_volume(machine_profile)
    if volume is None:
        logger.debug("Cannot determine build volume from machine profile, skipping check")
        return FitCheck(fits=True, needs_arrange=False, error=None)

    bbox = get_bounding_box(file_bytes)
    if bbox is None:
        logger.debug("No geometry found in 3MF, skipping check")
        return FitCheck(fits=True, needs_arrange=False, error=None)

    bed_w, bed_d, bed_h = volume
    model_w = bbox.size_x
    model_d = bbox.size_y
    model_h = bbox.size_z

    logger.info(
        "Model bounds: %.1f x %.1f x %.1f mm, build volume: %.0f x %.0f x %.0f mm",
        model_w, model_d, model_h, bed_w, bed_d, bed_h,
    )

    # Allow 0.5mm tolerance for floating point
    tolerance = 0.5

    # Check if the model dimensions are too large for the bed
    exceeded = []
    if model_w > bed_w + tolerance:
        exceeded.append(f"width {model_w:.1f}mm > {bed_w:.0f}mm")
    if model_d > bed_d + tolerance:
        exceeded.append(f"depth {model_d:.1f}mm > {bed_d:.0f}mm")
    if model_h > bed_h + tolerance:
        exceeded.append(f"height {model_h:.1f}mm > {bed_h:.0f}mm")

    if exceeded:
        return FitCheck(
            fits=False,
            needs_arrange=False,
            error=(
                f"Model does not fit the build volume ({bed_w:.0f}x{bed_d:.0f}x{bed_h:.0f}mm): "
                + ", ".join(exceeded)
            ),
        )

    # Model dimensions fit — check if the position is outside the bed
    position_ok = (
        bbox.min_x >= -tolerance
        and bbox.min_y >= -tolerance
        and bbox.max_x <= bed_w + tolerance
        and bbox.max_y <= bed_d + tolerance
    )

    if not position_ok:
        logger.info(
            "Model dimensions fit but position is off-plate "
            "(x: %.1f..%.1f, y: %.1f..%.1f vs bed %.0fx%.0f), needs arrange",
            bbox.min_x, bbox.max_x, bbox.min_y, bbox.max_y, bed_w, bed_d,
        )
        return FitCheck(fits=True, needs_arrange=True, error=None)

    return FitCheck(fits=True, needs_arrange=False, error=None)


def get_plate_count(file_bytes: bytes) -> int:
    """Return the number of plates in a 3MF file."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            if "Metadata/model_settings.config" not in zf.namelist():
                return 1
            ms = zf.read("Metadata/model_settings.config").decode()
            return len(re.findall(r'plater_id"\s+value="\d+"', ms))
    except (zipfile.BadZipFile, KeyError):
        return 1


def get_used_filament_slots(file_bytes: bytes, plate: int = 1) -> set[int] | None:
    """Return the 0-indexed filament slots actually used by the given plate.

    Reads `Metadata/slice_info.config`, which records per-plate filament usage
    as `<filament id="N" .../>` (1-indexed). Returns `None` if the file is
    absent, malformed, or the requested plate isn't listed — callers should
    then assume every slot may be used.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None
            raw = zf.read("Metadata/slice_info.config").decode()
    except (zipfile.BadZipFile, KeyError):
        return None

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    for plate_el in root.iter("plate"):
        plate_idx = None
        for meta in plate_el.findall("metadata"):
            if meta.get("key") == "index":
                try:
                    plate_idx = int(meta.get("value") or "")
                except ValueError:
                    plate_idx = None
                break
        if plate_idx != plate:
            continue
        slots: set[int] = set()
        for fil in plate_el.findall("filament"):
            raw_id = fil.get("id")
            if raw_id is None:
                continue
            try:
                one_based = int(raw_id)
            except ValueError:
                continue
            if one_based >= 1:
                slots.add(one_based - 1)
        return slots
    return None


def _get_plate_object_ids(model_settings: str, plate_id: str = "1") -> set[str]:
    """Extract object IDs assigned to a specific plate."""
    pattern = (
        r"<plate>\s*<metadata\s+key=\"plater_id\"\s+value=\""
        + re.escape(plate_id)
        + r"\".*?</plate>"
    )
    match = re.search(pattern, model_settings, re.DOTALL)
    if not match:
        return set()
    return set(re.findall(r'object_id"\s+value="(\d+)"', match.group(0)))


def _collect_mesh_data(
    zf: zipfile.ZipFile,
    root_model: str,
    object_ids: set[str],
    printable_per_object: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Collect world-space mesh data per build item on the plate.

    Iterates build items matching ``object_ids``, composes the build transform
    with each component's transform as it descends, and follows component refs
    both inline (same root model) and via ``p:path`` to sub-model files.

    When ``printable_per_object`` is provided, components at the build-item's
    immediate level whose ``objectid`` is not listed as a printable part for
    the parent object are skipped (see ``_read_printable_objectids``). This
    excludes modifier/negative/support volumes which are routinely scaled to
    extreme dimensions and would otherwise wreck the bed-recentering bbox.

    Returns a list of per-build-item entries: each entry is a dict with
    ``src_object_id`` (the build/item's objectid, used for metadata lookup),
    ``verts`` (world-space vertex list) and ``tris`` (tuples of three vertex
    indices plus a pre-escaped XML attribute fragment carrying any non-vN
    attributes from the source ``<triangle>`` — e.g. ``paint_color``,
    ``paint_supports``, ``paint_seam`` — so multi-material/seam/support
    painting survives the rebuild). Multiple entries are emitted when the
    plate hosts multiple distinct objects or instance copies, so the caller
    can preserve per-object identity in the output 3MF.
    """
    ns_p = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    root = ET.fromstring(root_model)

    objects: dict[str, ET.Element] = {
        obj.get("id"): obj
        for obj in root.findall(".//m:resources/m:object", _NS)
    }

    def collect_from_element(
        elem: ET.Element,
        transform: list[float],
        local_objects: dict[str, ET.Element],
        item_verts: list[tuple[float, float, float]],
        item_tris: list[tuple[int, int, int, str]],
        printable_objectids: set[str] | None = None,
    ) -> None:
        mesh = elem.find("m:mesh", _NS)
        if mesh is not None:
            offset = len(item_verts)
            for v in mesh.findall("m:vertices/m:vertex", _NS):
                x = float(v.get("x"))
                y = float(v.get("y"))
                z = float(v.get("z"))
                item_verts.append(_apply_transform(x, y, z, transform))
            for t in mesh.findall("m:triangles/m:triangle", _NS):
                extra = "".join(
                    f' {k}="{_xml_escape(v)}"'
                    for k, v in t.attrib.items()
                    if k not in ("v1", "v2", "v3") and not k.startswith("{")
                )
                item_tris.append((
                    int(t.get("v1")) + offset,
                    int(t.get("v2")) + offset,
                    int(t.get("v3")) + offset,
                    extra,
                ))

        for comp in elem.findall("m:components/m:component", _NS):
            comp_obj_id = comp.get("objectid")
            if (
                printable_objectids is not None
                and comp_obj_id not in printable_objectids
            ):
                continue
            comp_transform = _parse_transform(comp.get("transform"))
            combined = _chain_transforms(comp_transform, transform)
            path = comp.get(f"{{{ns_p}}}path")
            if path:
                zip_path = path.lstrip("/")
                try:
                    sub_data = zf.read(zip_path).decode()
                    sub_root = ET.fromstring(sub_data)
                    sub_objects = {
                        o.get("id"): o
                        for o in sub_root.findall(
                            ".//m:resources/m:object", _NS,
                        )
                    }
                    ref = sub_objects.get(comp_obj_id)
                    if ref is not None:
                        collect_from_element(
                            ref, combined, sub_objects,
                            item_verts, item_tris,
                        )
                except (KeyError, ET.ParseError):
                    pass
            else:
                ref = local_objects.get(comp_obj_id)
                if ref is not None:
                    collect_from_element(
                        ref, combined, local_objects,
                        item_verts, item_tris,
                    )

    items: list[dict[str, Any]] = []
    for item in root.findall("m:build/m:item", _NS):
        item_obj_id = item.get("objectid")
        if item_obj_id not in object_ids:
            continue
        obj_elem = objects.get(item_obj_id)
        if obj_elem is None:
            continue

        build_transform = _parse_transform(item.get("transform"))
        printable = (
            printable_per_object.get(item_obj_id)
            if printable_per_object is not None
            else None
        )
        item_verts: list[tuple[float, float, float]] = []
        item_tris: list[tuple[int, int, int, str]] = []
        collect_from_element(
            obj_elem, build_transform, objects,
            item_verts, item_tris, printable,
        )
        if item_verts and item_tris:
            items.append({
                "src_object_id": item_obj_id,
                "verts": item_verts,
                "tris": item_tris,
            })

    return items


def _read_object_names(zf: zipfile.ZipFile) -> dict[str, str]:
    """Map each ``<object id>`` in ``model_settings.config`` to its display name."""
    try:
        if "Metadata/model_settings.config" not in zf.namelist():
            return {}
        raw = zf.read("Metadata/model_settings.config").decode()
        root = ET.fromstring(raw)
    except (KeyError, ET.ParseError, UnicodeDecodeError):
        return {}

    names: dict[str, str] = {}
    for obj in root.findall("object"):
        obj_id = obj.get("id")
        if obj_id is None:
            continue
        for meta in obj.findall("metadata"):
            if meta.get("key") == "name":
                value = meta.get("value")
                if value:
                    names[obj_id] = value
                break
    return names


# OrcaSlicer arranges plates in a grid whose column count is
# ``round(sqrt(N))``, rounded up when the square is non-perfect (see
# ``compute_colum_count`` in ``PartPlate.hpp``). Plates step by
# ``bed_size * (1 + LOGICAL_PART_PLATE_GAP)`` (gap = 1/5) — column index
# advances +X, row index advances -Y (``compute_shape_position`` in
# ``PartPlate.cpp``). Mirroring that formula here lets us recover the plate's
# plater-coords origin from its 1-indexed plate id, which is needed to
# convert the source's plater-space build transforms back into bed-relative
# (plate-local) coordinates when collapsing to a single plate.
_PLATE_GRID_GAP_FACTOR = 1.0 + 1.0 / 5.0


def _compute_plate_origin(
    plate_idx: int,
    plate_count: int,
    bed_w: float,
    bed_h: float,
) -> tuple[float, float]:
    """Return the plater-coords origin of plate ``plate_idx`` (0-based).

    Falls back to ``(0, 0)`` when the inputs are degenerate so a malformed
    multi-plate 3MF still produces some output rather than crashing.
    """
    if plate_count <= 0 or bed_w <= 0 or bed_h <= 0:
        return 0.0, 0.0
    cols_f = math.sqrt(float(plate_count))
    cols_round = round(cols_f)
    cols = cols_round + 1 if cols_f > cols_round else cols_round
    cols = max(1, cols)
    row = plate_idx // cols
    col = plate_idx % cols
    stride_x = bed_w * _PLATE_GRID_GAP_FACTOR
    stride_y = bed_h * _PLATE_GRID_GAP_FACTOR
    return col * stride_x, -row * stride_y


# Plate-level metadata in `model_settings.config` that we must NOT carry over
# when rebuilding a single-plate 3MF: identity / file-reference keys whose
# values either describe the source layout (and would mismatch our rebuilt
# plate index of 1) or point at thumbnail/gcode files we are not copying. The
# important plate-state keys (`bed_type`, `print_sequence`,
# `first_layer_print_sequence`, `other_layers_print_sequence`, `spiral_mode`,
# `filament_map_mode`, `filament_map`) DO get preserved — that's the whole
# point of this carry-over: OrcaSlicer's CLI reads them off the rebuilt
# `<plate>` element in `bbs_3mf.cpp` and applies them to the per-plate config.
_PLATE_METADATA_DROP = frozenset({
    "plater_id",
    "plater_name",
    "locked",
    "gcode_file",
    "thumbnail_file",
    "thumbnail_no_light_file",
    "top_file",
    "pick_file",
    "pattern_file",
    "pattern_bbox_file",
})


def _read_plate_metadata(
    zf: zipfile.ZipFile, plate_id: str,
) -> list[tuple[str, str]]:
    """Return ordered ``(key, value)`` pairs for the requested plate's metadata.

    Skips identity keys we always rewrite (``plater_id``, ``locked``,
    ``plater_name``) and stale file references (gcode/thumbnail paths that
    name files in the source archive, not the rebuilt one). Per-plate state
    keys like ``bed_type`` and the print-sequence overrides survive — these
    are normally read by ``bbs_3mf.cpp:_handle_start_config_plater`` and
    applied to ``PlateData::config``, but ``extract_plate`` rebuilt the plate
    block from scratch and silently dropped them before this function existed.
    """
    try:
        if "Metadata/model_settings.config" not in zf.namelist():
            return []
        raw = zf.read("Metadata/model_settings.config").decode()
        root = ET.fromstring(raw)
    except (KeyError, ET.ParseError, UnicodeDecodeError):
        return []

    for plate in root.findall("plate"):
        pid = ""
        for meta in plate.findall("metadata"):
            if meta.get("key") == "plater_id":
                pid = meta.get("value") or ""
                break
        if pid != plate_id:
            continue
        preserved: list[tuple[str, str]] = []
        for meta in plate.findall("metadata"):
            key = meta.get("key") or ""
            if not key or key in _PLATE_METADATA_DROP:
                continue
            value = meta.get("value") or ""
            preserved.append((key, value))
        return preserved
    return []


def _read_plate_instances(
    zf: zipfile.ZipFile, plate_id: str,
) -> list[dict[str, str]]:
    """Return ordered ``<model_instance>`` entries for the requested plate.

    Each entry has ``object_id``, ``instance_id`` and ``identify_id`` (any
    missing key defaults to an empty string). OrcaSlicer's gcode labeling
    uses ``identify_id`` as the per-object label id, so preserving it from
    the input keeps the output's per-object boundaries aligned with what
    the GUI would emit.
    """
    try:
        if "Metadata/model_settings.config" not in zf.namelist():
            return []
        raw = zf.read("Metadata/model_settings.config").decode()
        root = ET.fromstring(raw)
    except (KeyError, ET.ParseError, UnicodeDecodeError):
        return []

    for plate in root.findall("plate"):
        pid = ""
        for meta in plate.findall("metadata"):
            if meta.get("key") == "plater_id":
                pid = meta.get("value") or ""
                break
        if pid != plate_id:
            continue
        result: list[dict[str, str]] = []
        for inst in plate.findall("model_instance"):
            entry: dict[str, str] = {
                "object_id": "", "instance_id": "", "identify_id": "",
            }
            for meta in inst.findall("metadata"):
                key = meta.get("key")
                if key in entry:
                    entry[key] = meta.get("value") or ""
            result.append(entry)
        return result
    return []


def extract_plate(
    file_bytes: bytes,
    bed_center_x: float = 90.0,
    bed_center_y: float = 90.0,
    plate_id: str = "1",
) -> bytes | None:
    """Extract a plate's geometry from a multi-plate 3MF into a fresh simple 3MF.

    Each input build item becomes its own ``<object>`` in the output (with its
    original name + identify_id preserved when ``model_settings.config`` lists
    them), so OrcaSlicer's gcode emits real per-object label_object boundaries
    instead of collapsing every part into a single ``"Model"`` blob.

    Vertex placement: source build transforms live in plater coordinates (the
    multi-plate grid OrcaSlicer composes via ``compute_shape_position``); to
    get bed-relative positions we subtract the plate's plater-coords origin.
    This preserves the GUI's intended layout — e.g. when a plate's wipe tower
    is configured at (15, 145) and the GUI placed the model offset to leave
    that corner free, naive bed-centering would re-center the model on top of
    that corner and trip ``CLI_GCODE_PATH_CONFLICTS`` on the slice.

    When the source's build transforms don't align with our recovered grid
    (cross-printer 3MFs, hand-crafted files), the plate-local bbox can land
    off the bed. We fall back to bed-centering in that case so the slice
    still has a chance to succeed (``--arrange 1`` will tidy it up later).

    Returns new 3MF bytes, or None if extraction fails.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            if "Metadata/model_settings.config" not in zf.namelist():
                return None

            ms = zf.read("Metadata/model_settings.config").decode()
            plate_ids = _get_plate_object_ids(ms, plate_id)
            if not plate_ids:
                return None
            plate_count = len(re.findall(r'plater_id"\s+value="\d+"', ms))

            root_model_path = None
            for name in zf.namelist():
                if name == "3D/3dmodel.model":
                    root_model_path = name
                    break
            if root_model_path is None:
                return None

            root_model = zf.read(root_model_path).decode()
            printable_per_object = _read_printable_objectids(zf)
            object_names = _read_object_names(zf)
            plate_instances = _read_plate_instances(zf, plate_id)
            plate_metadata = _read_plate_metadata(zf, plate_id)
            # Carry the source's ``project_settings.config`` through. The
            # rebuilt 3MF is stamped as a BBL/Orca file (see the
            # ``Application`` metadata below), which routes OrcaSlicer's
            # loader into the BBL branch at ``OrcaSlicer.cpp:1552-1701``;
            # that branch dereferences ``printer_settings_id``,
            # ``print_settings_id``, ``filament_settings_id``, and
            # ``nozzle_diameter`` straight off the parsed config without
            # null-checks (lines 1607-1611), so a missing sidecar means
            # SIGSEGV. ``slicer.py``'s ``_sanitize_3mf`` then handles
            # printer-identity rebranding and per-filament truncation on
            # this same file before it reaches orca-slicer.
            project_settings_bytes: bytes | None = None
            if "Metadata/project_settings.config" in zf.namelist():
                project_settings_bytes = zf.read(
                    "Metadata/project_settings.config",
                )
            items = _collect_mesh_data(
                zf, root_model, plate_ids,
                printable_per_object=printable_per_object,
            )

        if not items:
            return None

        all_xs = [v[0] for it in items for v in it["verts"]]
        all_ys = [v[1] for it in items for v in it["verts"]]
        all_zs = [v[2] for it in items for v in it["verts"]]
        bed_w = bed_center_x * 2
        bed_h = bed_center_y * 2

        # Try plate-local positioning first: subtract the source plate's
        # plater-coords origin from every vertex so the model lands wherever
        # the GUI placed it on its bed.
        plate_local_used = False
        try:
            plate_idx_zero = max(0, int(plate_id) - 1)
        except ValueError:
            plate_idx_zero = 0
        if plate_count > 0:
            ox, oy = _compute_plate_origin(
                plate_idx_zero, plate_count, bed_w, bed_h,
            )
            local_min_x = min(all_xs) - ox
            local_max_x = max(all_xs) - ox
            local_min_y = min(all_ys) - oy
            local_max_y = max(all_ys) - oy
            tolerance = 5.0
            if (
                local_min_x >= -tolerance
                and local_max_x <= bed_w + tolerance
                and local_min_y >= -tolerance
                and local_max_y <= bed_h + tolerance
            ):
                tx = -ox
                ty = -oy
                plate_local_used = True

        if not plate_local_used:
            # Fall back to bed-centering when plate-local would land off-bed.
            cx = (min(all_xs) + max(all_xs)) / 2
            cy = (min(all_ys) + max(all_ys)) / 2
            tx = bed_center_x - cx
            ty = bed_center_y - cy
        tz = -min(all_zs)

        # Walk plate's model_instance entries in order to assign identify_ids
        # to build items; each entry is consumed by the first matching item.
        unused_instances = list(plate_instances)

        def take_identify_id(src_obj_id: str) -> str:
            for idx, inst in enumerate(unused_instances):
                if inst.get("object_id") == src_obj_id:
                    return unused_instances.pop(idx).get("identify_id", "") or ""
            return ""

        object_blocks: list[str] = []
        build_items: list[str] = []
        ms_objects: list[str] = []
        ms_instances: list[str] = []
        ms_assemble: list[str] = []

        for idx, item in enumerate(items, start=1):
            obj_id = idx
            src_obj_id = item["src_object_id"]
            name = object_names.get(src_obj_id, f"Object {idx}")
            identify_id = take_identify_id(src_obj_id) or str(obj_id)

            shifted = [
                (x + tx, y + ty, z + tz) for x, y, z in item["verts"]
            ]
            v_xml = "".join(
                f'    <vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>\n'
                for v in shifted
            )
            t_xml = "".join(
                f'    <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"{t[3]}/>\n'
                for t in item["tris"]
            )
            object_blocks.append(
                f'  <object id="{obj_id}" type="model">\n'
                "   <mesh>\n"
                "    <vertices>\n" + v_xml + "    </vertices>\n"
                "    <triangles>\n" + t_xml + "    </triangles>\n"
                "   </mesh>\n"
                "  </object>\n"
            )
            build_items.append(
                f'  <item objectid="{obj_id}"'
                ' transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
            )

            zs = [v[2] for v in item["verts"]]
            height = max(zs) - min(zs)
            xml_name = _xml_escape(name)
            ms_objects.append(
                f'  <object id="{obj_id}">\n'
                f'    <metadata key="name" value="{xml_name}"/>\n'
                '    <metadata key="extruder" value="1"/>\n'
                f'    <part id="{obj_id}" subtype="normal_part">\n'
                f'      <metadata key="name" value="{xml_name}"/>\n'
                '      <metadata key="matrix"'
                ' value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                '      <metadata key="source_object_id" value="0"/>\n'
                '      <metadata key="source_volume_id" value="0"/>\n'
                '      <metadata key="source_offset_x" value="0"/>\n'
                '      <metadata key="source_offset_y" value="0"/>\n'
                f'      <metadata key="source_offset_z" value="{-height / 2}"/>\n'
                "    </part>\n"
                "  </object>\n"
            )
            ms_instances.append(
                "    <model_instance>\n"
                f'      <metadata key="object_id" value="{obj_id}"/>\n'
                '      <metadata key="instance_id" value="0"/>\n'
                f'      <metadata key="identify_id" value="{identify_id}"/>\n'
                "    </model_instance>\n"
            )
            ms_assemble.append(
                f'   <assemble_item object_id="{obj_id}" instance_id="0"'
                ' transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0" />\n'
            )

        # Tag the rebuilt model as a BBL/Orca 3MF. Without these metadata
        # entries, ``bbs_3mf.cpp:3754-3764`` leaves ``m_is_bbl_3mf`` false,
        # which makes ``OrcaSlicer.cpp`` keep the default ``need_arrange =
        # true`` (line 1349, only flipped to false at line 1561 when
        # ``is_bbl_3mf``). Auto-arrange then re-centers the model on the
        # bed, throwing away the plate-local layout we just computed and
        # potentially colliding with the wipe tower at its configured
        # plate-3 position.
        model_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US"'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
            ' xmlns:BambuStudio='
            '"http://schemas.bambulab.com/package/2021">\n'
            ' <metadata name="Application">OrcaSlicer-2.3.2</metadata>\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
            " <resources>\n"
            + "".join(object_blocks)
            + " </resources>\n"
            " <build>\n"
            + "".join(build_items)
            + " </build>\n"
            "</model>"
        )

        plate_meta_xml = "".join(
            f'    <metadata key="{_xml_escape(k)}"'
            f' value="{_xml_escape(v)}"/>\n'
            for k, v in plate_metadata
        )

        ms_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<config>\n"
            + "".join(ms_objects)
            + "  <plate>\n"
            '    <metadata key="plater_id" value="1"/>\n'
            '    <metadata key="plater_name" value=""/>\n'
            '    <metadata key="locked" value="false"/>\n'
            + plate_meta_xml
            + "".join(ms_instances)
            + "  </plate>\n"
            "  <assemble>\n"
            + "".join(ms_assemble)
            + "  </assemble>\n"
            "</config>"
        )

        ct_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            ' <Default ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"'
            ' Extension="rels"/>\n'
            ' <Default ContentType='
            '"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"'
            ' Extension="model"/>\n'
            "</Types>"
        )

        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns='
            '"http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1"'
            ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            "</Relationships>"
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf_out:
            zf_out.writestr("3D/3dmodel.model", model_xml)
            zf_out.writestr("Metadata/model_settings.config", ms_xml)
            if project_settings_bytes is not None:
                zf_out.writestr(
                    "Metadata/project_settings.config",
                    project_settings_bytes,
                )
            zf_out.writestr("[Content_Types].xml", ct_xml)
            zf_out.writestr("_rels/.rels", rels_xml)

        result = buf.getvalue()
        total_verts = sum(len(it["verts"]) for it in items)
        total_tris = sum(len(it["tris"]) for it in items)
        logger.info(
            "Extracted plate %s: %d objects, %d vertices, %d triangles, %d bytes",
            plate_id, len(items), total_verts, total_tris, len(result),
        )
        return result

    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        logger.warning("Failed to extract first plate from 3MF: %s", exc)
        return None
