"""Convert STL files to minimal 3MF archives for OrcaSlicer slicing.

Supports both binary and ASCII STL formats. The generated 3MF is a minimal
valid archive that OrcaSlicer CLI can slice directly.
"""

import io
import logging
import struct
import zipfile

logger = logging.getLogger(__name__)


def detect_file_type(filename: str | None, data: bytes) -> str:
    """Detect whether data is a 3MF or STL file.

    Returns ``"3mf"``, ``"stl"``, or ``"unknown"``.
    """
    if filename:
        lower = filename.lower()
        if lower.endswith(".3mf"):
            return "3mf"
        if lower.endswith(".stl"):
            return "stl"

    # ZIP magic bytes → 3MF
    if data[:2] == b"PK":
        return "3mf"

    # Binary STL check: 80-byte header + 4-byte triangle count
    if len(data) >= 84:
        num_tris = struct.unpack_from("<I", data, 80)[0]
        expected = 84 + num_tris * 50
        if expected == len(data):
            return "stl"

    # ASCII STL starts with "solid"
    if data[:6].lower().startswith(b"solid"):
        # But some binary STLs also start with "solid" in the header.
        # Double-check: if it contains "facet" within the first 1000 bytes,
        # it's likely ASCII.
        if b"facet" in data[:1000]:
            return "stl"

    return "unknown"


def _parse_binary_stl(data: bytes) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary STL file into deduplicated vertices and triangles."""
    num_tris = struct.unpack_from("<I", data, 80)[0]
    offset = 84

    vertex_map: dict[tuple[int, int, int], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    for _ in range(num_tris):
        # Skip normal (3 floats = 12 bytes)
        offset += 12
        tri_indices = []
        for _ in range(3):
            x, y, z = struct.unpack_from("<fff", data, offset)
            offset += 12
            # Round to 6 decimal places for deduplication (micrometer precision)
            key = (round(x * 1e6), round(y * 1e6), round(z * 1e6))
            if key not in vertex_map:
                vertex_map[key] = len(vertices)
                vertices.append((x, y, z))
            tri_indices.append(vertex_map[key])
        # Skip attribute byte count (2 bytes)
        offset += 2
        triangles.append((tri_indices[0], tri_indices[1], tri_indices[2]))

    return vertices, triangles


def _parse_ascii_stl(data: bytes) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse an ASCII STL file into deduplicated vertices and triangles."""
    text = data.decode("ascii", errors="replace")

    vertex_map: dict[tuple[int, int, int], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    tri_verts: list[int] = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("vertex"):
            parts = stripped.split()
            if len(parts) >= 4:
                x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                key = (round(x * 1e6), round(y * 1e6), round(z * 1e6))
                if key not in vertex_map:
                    vertex_map[key] = len(vertices)
                    vertices.append((x, y, z))
                tri_verts.append(vertex_map[key])
        elif stripped.startswith("endfacet"):
            if len(tri_verts) == 3:
                triangles.append((tri_verts[0], tri_verts[1], tri_verts[2]))
            tri_verts = []

    return vertices, triangles


def _is_binary_stl(data: bytes) -> bool:
    """Determine whether STL data is binary or ASCII format."""
    if len(data) >= 84:
        num_tris = struct.unpack_from("<I", data, 80)[0]
        expected = 84 + num_tris * 50
        if expected == len(data):
            return True
    return False


def stl_to_3mf(
    stl_data: bytes,
    bed_center_x: float = 128.0,
    bed_center_y: float = 128.0,
) -> bytes:
    """Convert an STL file to a minimal valid 3MF archive.

    The model is centered on (bed_center_x, bed_center_y) and placed on Z=0.
    """
    if _is_binary_stl(stl_data):
        vertices, triangles = _parse_binary_stl(stl_data)
    else:
        vertices, triangles = _parse_ascii_stl(stl_data)

    if not vertices or not triangles:
        raise ValueError("STL file contains no geometry")

    logger.info("Parsed STL: %d vertices, %d triangles", len(vertices), len(triangles))

    # Compute bounding box and center on bed
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    min_z = min(zs)

    tx = bed_center_x - cx
    ty = bed_center_y - cy
    tz = -min_z  # Place on Z=0

    final_verts = [(x + tx, y + ty, z + tz) for x, y, z in vertices]
    height = max(zs) - min(zs)

    # Build 3MF XML
    v_xml = "".join(
        f'    <vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>\n'
        for v in final_verts
    )
    t_xml = "".join(
        f'    <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>\n'
        for t in triangles
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
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
        zf.writestr("Metadata/model_settings.config", ms_xml)
        zf.writestr("[Content_Types].xml", ct_xml)
        zf.writestr("_rels/.rels", rels_xml)

    result = buf.getvalue()
    logger.info(
        "Created 3MF from STL: %d vertices, %d triangles, %d bytes",
        len(final_verts), len(triangles), len(result),
    )
    return result
