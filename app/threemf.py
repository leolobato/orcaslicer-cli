"""Parse 3MF archives to extract object bounding boxes."""

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}

_IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


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
) -> list[tuple[float, float, float]]:
    """Collect transformed vertices from an object, following component refs and sub-models."""
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
        comp_transform = _parse_transform(comp.get("transform"))
        combined = _chain_transforms(transform, comp_transform)
        comp_obj_id = comp.get("objectid")
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

        all_points: list[tuple[float, float, float]] = []
        for item in root.findall("m:build/m:item", _NS):
            obj_id = item.get("objectid")
            obj_elem = objects.get(obj_id)
            if obj_elem is None:
                continue
            item_transform = _parse_transform(item.get("transform"))
            all_points.extend(_collect_vertices_recursive(
                obj_elem, item_transform, objects, zf,
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
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Collect world-space vertices and triangles for all plate objects.

    Iterates all build items matching object_ids, applies each item's
    build transform, and follows component p:path references to sub-model files.
    Returns (world_vertices, triangles).
    """
    ns_p = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    root = ET.fromstring(root_model)

    objects: dict[str, ET.Element] = {
        obj.get("id"): obj
        for obj in root.findall(".//m:resources/m:object", _NS)
    }

    all_verts: list[tuple[float, float, float]] = []
    all_tris: list[tuple[int, int, int]] = []

    def collect_from_element(elem: ET.Element):
        mesh = elem.find("m:mesh", _NS)
        if mesh is not None:
            offset = len(all_verts)
            for v in mesh.findall("m:vertices/m:vertex", _NS):
                all_verts.append((
                    float(v.get("x")), float(v.get("y")), float(v.get("z")),
                ))
            for t in mesh.findall("m:triangles/m:triangle", _NS):
                all_tris.append((
                    int(t.get("v1")) + offset,
                    int(t.get("v2")) + offset,
                    int(t.get("v3")) + offset,
                ))

        for comp in elem.findall("m:components/m:component", _NS):
            path = comp.get(f"{{{ns_p}}}path")
            comp_obj_id = comp.get("objectid")
            if path:
                zip_path = path.lstrip("/")
                try:
                    sub_data = zf.read(zip_path).decode()
                    sub_root = ET.fromstring(sub_data)
                    for sub_obj in sub_root.findall(".//m:resources/m:object", _NS):
                        if comp_obj_id is None or sub_obj.get("id") == comp_obj_id:
                            collect_from_element(sub_obj)
                            break
                except (KeyError, ET.ParseError):
                    pass

    # Process ALL build items that match plate objects (not just the first)
    for item in root.findall("m:build/m:item", _NS):
        if item.get("objectid") not in object_ids:
            continue
        obj_elem = objects.get(item.get("objectid"))
        if obj_elem is None:
            continue

        # Collect mesh in object-local space, then apply build-item transform
        local_verts_start = len(all_verts)
        collect_from_element(obj_elem)

        # Apply this item's build transform to the vertices just collected
        build_transform = _parse_transform(item.get("transform"))
        for i in range(local_verts_start, len(all_verts)):
            x, y, z = all_verts[i]
            all_verts[i] = _apply_transform(x, y, z, build_transform)

    return all_verts, all_tris


def extract_plate(
    file_bytes: bytes,
    bed_center_x: float = 90.0,
    bed_center_y: float = 90.0,
    plate_id: str = "1",
) -> bytes | None:
    """Extract a plate's geometry from a multi-plate 3MF into a fresh simple 3MF.

    Returns new 3MF bytes with a single inline mesh centered on the bed,
    or None if extraction fails.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            if "Metadata/model_settings.config" not in zf.namelist():
                return None

            ms = zf.read("Metadata/model_settings.config").decode()
            plate_ids = _get_plate_object_ids(ms, plate_id)
            if not plate_ids:
                return None

            root_model_path = None
            for name in zf.namelist():
                if name == "3D/3dmodel.model":
                    root_model_path = name
                    break
            if root_model_path is None:
                return None

            root_model = zf.read(root_model_path).decode()
            world_verts, tris = _collect_mesh_data(
                zf, root_model, plate_ids,
            )

        if not world_verts or not tris:
            return None

        # Compute bounding box and center on target bed
        xs = [v[0] for v in world_verts]
        ys = [v[1] for v in world_verts]
        zs = [v[2] for v in world_verts]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        min_z = min(zs)

        tx = bed_center_x - cx
        ty = bed_center_y - cy
        tz = -min_z

        final_verts = [(x + tx, y + ty, z + tz) for x, y, z in world_verts]
        height = max(zs) - min(zs)

        # Build minimal 3MF XML
        v_xml = "".join(
            f'    <vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>\n'
            for v in final_verts
        )
        t_xml = "".join(
            f'    <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>\n'
            for t in tris
        )

        model_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US"'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
            " <resources>\n"
            '  <object id="1" type="model">\n'
            "   <mesh>\n"
            "    <vertices>\n" + v_xml + "    </vertices>\n"
            "    <triangles>\n" + t_xml + "    </triangles>\n"
            "   </mesh>\n"
            "  </object>\n"
            " </resources>\n"
            " <build>\n"
            '  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
            " </build>\n"
            "</model>"
        )

        ms_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<config>\n"
            '  <object id="1">\n'
            '    <metadata key="name" value="Model"/>\n'
            '    <metadata key="extruder" value="1"/>\n'
            '    <part id="1" subtype="normal_part">\n'
            '      <metadata key="name" value="Model"/>\n'
            '      <metadata key="matrix"'
            ' value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
            '      <metadata key="source_object_id" value="0"/>\n'
            '      <metadata key="source_volume_id" value="0"/>\n'
            '      <metadata key="source_offset_x" value="0"/>\n'
            '      <metadata key="source_offset_y" value="0"/>\n'
            f'      <metadata key="source_offset_z" value="{-height / 2}"/>\n'
            "    </part>\n"
            "  </object>\n"
            "  <plate>\n"
            '    <metadata key="plater_id" value="1"/>\n'
            '    <metadata key="plater_name" value=""/>\n'
            '    <metadata key="locked" value="false"/>\n'
            "    <model_instance>\n"
            '      <metadata key="object_id" value="1"/>\n'
            '      <metadata key="instance_id" value="0"/>\n'
            '      <metadata key="identify_id" value="1"/>\n'
            "    </model_instance>\n"
            "  </plate>\n"
            "  <assemble>\n"
            '   <assemble_item object_id="1" instance_id="0"'
            ' transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0" />\n'
            "  </assemble>\n"
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
            zf_out.writestr("[Content_Types].xml", ct_xml)
            zf_out.writestr("_rels/.rels", rels_xml)

        result = buf.getvalue()
        logger.info(
            "Extracted plate %s from multi-plate 3MF: %d vertices, %d triangles, %d bytes",
            plate_id, len(final_verts), len(tris), len(result),
        )
        return result

    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        logger.warning("Failed to extract first plate from 3MF: %s", exc)
        return None
