"""Parse 3MF archives to extract object bounding boxes."""

import io
import logging
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


def get_bounding_box(file_bytes: bytes) -> BBox | None:
    """Return the overall bounding box of all build items in a 3MF file.

    Returns None if no geometry is found.
    """
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        model_path = None
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

    def collect_vertices(
        obj_elem: ET.Element, transform: list[float],
    ) -> list[tuple[float, float, float]]:
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
            ref_obj = objects.get(comp.get("objectid"))
            if ref_obj is not None:
                points.extend(collect_vertices(ref_obj, combined))

        return points

    all_points: list[tuple[float, float, float]] = []
    for item in root.findall("m:build/m:item", _NS):
        obj_id = item.get("objectid")
        obj_elem = objects.get(obj_id)
        if obj_elem is None:
            continue
        item_transform = _parse_transform(item.get("transform"))
        all_points.extend(collect_vertices(obj_elem, item_transform))

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


def validate_model_fits(
    file_bytes: bytes, machine_profile: dict[str, Any],
) -> str | None:
    """Check that the 3MF model fits within the machine's build volume.

    Returns an error message if it doesn't fit, or None if it's fine.
    """
    volume = get_build_volume(machine_profile)
    if volume is None:
        logger.debug("Cannot determine build volume from machine profile, skipping check")
        return None

    bbox = get_bounding_box(file_bytes)
    if bbox is None:
        logger.debug("No geometry found in 3MF, skipping check")
        return None

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
    exceeded = []
    if model_w > bed_w + tolerance:
        exceeded.append(f"width {model_w:.1f}mm > {bed_w:.0f}mm")
    if model_d > bed_d + tolerance:
        exceeded.append(f"depth {model_d:.1f}mm > {bed_d:.0f}mm")
    if model_h > bed_h + tolerance:
        exceeded.append(f"height {model_h:.1f}mm > {bed_h:.0f}mm")

    if exceeded:
        return (
            f"Model does not fit the build volume ({bed_w:.0f}x{bed_d:.0f}x{bed_h:.0f}mm): "
            + ", ".join(exceeded)
        )
    return None
