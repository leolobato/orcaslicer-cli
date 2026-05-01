import io
import unittest
import zipfile

from app.threemf import extract_plate, get_bounding_box


_NS_DECL = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
)


def _cube_mesh(verts: list[tuple[float, float, float]]) -> str:
    vx = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    # A degenerate-but-parseable triangle list (real triangles unnecessary for bbox).
    tx = '<triangle v1="0" v2="1" v3="2"/>'
    return f"<mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh>"


def _make_3mf(
    model_xml: str,
    model_settings_xml: str | None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "")
        zf.writestr("3D/3dmodel.model", model_xml)
        if model_settings_xml is not None:
            zf.writestr(
                "Metadata/model_settings.config", model_settings_xml,
            )
    return buf.getvalue()


def _two_part_model_xml(
    normal_verts: list[tuple[float, float, float]],
    modifier_verts: list[tuple[float, float, float]],
) -> str:
    return (
        f'<?xml version="1.0"?>'
        f"<model {_NS_DECL}>"
        "<resources>"
        f'<object id="1" type="model">{_cube_mesh(normal_verts)}</object>'
        f'<object id="2" type="model">{_cube_mesh(modifier_verts)}</object>'
        '<object id="10" type="model"><components>'
        '<component objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        "</components></object>"
        "</resources>"
        '<build><item objectid="10"'
        ' transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>'
        "</model>"
    )


class BoundingBoxModifierExclusionTests(unittest.TestCase):
    def test_excludes_modifier_part_from_bbox(self) -> None:
        # Normal part: a 10x10x10 cube near origin.
        normal = [(0, 0, 0), (10, 0, 0), (0, 10, 0)]
        # "Modifier": a giant cube far from origin that would blow up the bbox.
        modifier = [(500, 500, 500), (600, 500, 500), (500, 600, 500)]

        model_xml = _two_part_model_xml(normal, modifier)
        ms_xml = (
            '<?xml version="1.0"?>'
            '<config><object id="10">'
            '<part id="1" subtype="normal_part"/>'
            '<part id="2" subtype="modifier_part"/>'
            "</object></config>"
        )

        bbox = get_bounding_box(_make_3mf(model_xml, ms_xml))

        self.assertIsNotNone(bbox)
        self.assertEqual(bbox.size_x, 10)
        self.assertEqual(bbox.size_y, 10)

    def test_excludes_negative_and_support_parts(self) -> None:
        normal = [(0, 0, 0), (5, 0, 0), (0, 5, 0)]
        far_away = [(900, 900, 900), (901, 900, 900), (900, 901, 900)]

        # All four non-printable subtypes should be filtered.
        for subtype in (
            "modifier_part",
            "negative_part",
            "support_enforcer",
            "support_blocker",
        ):
            with self.subTest(subtype=subtype):
                model_xml = _two_part_model_xml(normal, far_away)
                ms_xml = (
                    '<?xml version="1.0"?>'
                    '<config><object id="10">'
                    '<part id="1" subtype="normal_part"/>'
                    f'<part id="2" subtype="{subtype}"/>'
                    "</object></config>"
                )
                bbox = get_bounding_box(_make_3mf(model_xml, ms_xml))
                self.assertEqual(bbox.size_x, 5)
                self.assertEqual(bbox.size_y, 5)

    def test_includes_all_when_model_settings_missing(self) -> None:
        # Legacy 3MF without per-part metadata: include everything (prior behavior).
        normal = [(0, 0, 0), (10, 0, 0), (0, 10, 0)]
        other = [(100, 0, 0), (110, 0, 0), (100, 10, 0)]
        model_xml = _two_part_model_xml(normal, other)

        bbox = get_bounding_box(_make_3mf(model_xml, model_settings_xml=None))

        self.assertEqual(bbox.size_x, 110)

    def test_includes_part_when_subtype_attribute_missing(self) -> None:
        # A <part> without a subtype defaults to normal_part (matches OrcaSlicer
        # ``ModelVolume::type_from_string`` fallback).
        normal = [(0, 0, 0), (5, 0, 0), (0, 5, 0)]
        other = [(20, 0, 0), (25, 0, 0), (20, 5, 0)]
        model_xml = _two_part_model_xml(normal, other)
        ms_xml = (
            '<?xml version="1.0"?>'
            '<config><object id="10">'
            '<part id="1"/>'
            '<part id="2"/>'
            "</object></config>"
        )

        bbox = get_bounding_box(_make_3mf(model_xml, ms_xml))

        self.assertEqual(bbox.size_x, 25)


class BoundingBoxTransformCompositionTests(unittest.TestCase):
    """The build item's transform places the parent object in world space; a
    component's transform places its mesh inside the parent's frame. So a
    vertex must flow ``local -> component -> build`` — meaning the combined
    transform is "component first, then build". A latent bug applied them
    in the reverse order, which only manifested when both transforms were
    non-identity (e.g. the user-reported benchy with a rotated component
    and a build translation centering it on the bed).
    """

    def test_component_rotation_does_not_flip_build_translation(self) -> None:
        # Symmetric vertices around the local origin — the bbox center is
        # invariant under rotation, so it must land exactly at the build
        # translation.
        verts = [(-1, -1, 0), (1, -1, 0), (0, 1, 0)]
        # Component rotates 90° around Z.
        comp_t = "0 -1 0 1 0 0 0 0 1 0 0 0"
        # Build item translates by (90, 90, 24) — A1 Mini bed center.
        build_t = "1 0 0 0 1 0 0 0 1 90 90 24"

        model_xml = (
            f'<?xml version="1.0"?>'
            f"<model {_NS_DECL}>"
            "<resources>"
            f'<object id="1" type="model">{_cube_mesh(verts)}</object>'
            '<object id="10" type="model"><components>'
            f'<component objectid="1" transform="{comp_t}"/>'
            "</components></object>"
            "</resources>"
            f'<build><item objectid="10" transform="{build_t}"/></build>'
            "</model>"
        )

        bbox = get_bounding_box(_make_3mf(model_xml, model_settings_xml=None))

        center_x = (bbox.min_x + bbox.max_x) / 2
        center_y = (bbox.min_y + bbox.max_y) / 2
        center_z = (bbox.min_z + bbox.max_z) / 2

        # With the wrong composition order, the rotation would have rotated
        # the build translation vector — flipping Y to -90 (off-bed).
        self.assertAlmostEqual(center_x, 90.0, places=4)
        self.assertAlmostEqual(center_y, 90.0, places=4)
        self.assertAlmostEqual(center_z, 24.0, places=4)


def _extracted_bbox(plate_3mf: bytes) -> tuple[float, float, float]:
    """Return (size_x, size_y, size_z) of the inline mesh in an extracted plate."""
    bbox = get_bounding_box(plate_3mf)
    assert bbox is not None
    return bbox.size_x, bbox.size_y, bbox.size_z


class ExtractPlateMeshDataTests(unittest.TestCase):
    """``extract_plate`` rebuilds a multi-plate 3MF as a single-plate 3MF with
    inline geometry. Three pre-existing bugs were fixed together:

    1. Component transforms were ignored when descending into sub-models (and
       local component refs were dropped entirely).
    2. The build-item transform was applied at the end instead of being
       composed with comp transforms during traversal.
    3. Modifier parts were included in the geometry, polluting the mesh and
       skewing the bed-recentering bbox.
    """

    def _build_3mf(
        self,
        objects_xml: str,
        build_xml: str,
        model_settings_xml: str,
        sub_models: dict[str, str] | None = None,
    ) -> bytes:
        model_xml = (
            f'<?xml version="1.0"?>'
            f"<model {_NS_DECL} "
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            f"<resources>{objects_xml}</resources>"
            f"<build>{build_xml}</build>"
            "</model>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "")
            zf.writestr("3D/3dmodel.model", model_xml)
            zf.writestr("Metadata/model_settings.config", model_settings_xml)
            for path, content in (sub_models or {}).items():
                zf.writestr(path, content)
        return buf.getvalue()

    def test_local_component_ref_with_rotation_is_honored(self) -> None:
        # A 6×6×6 cube triangle at the local origin, rotated 90° around Z by
        # the component transform, then translated by the build item. With the
        # pre-fix code, both the rotation and the local-ref descent were
        # silently dropped — the extracted mesh would be empty.
        # Triangle picked so the rotation produces a different bbox shape:
        # local extent X=[0,6], Y=[0,2]; after 90° rotation: X=[-2,0], Y=[0,6].
        verts = [(0, 0, 0), (6, 0, 0), (6, 2, 0)]
        objects_xml = (
            f'<object id="1" type="model">{_cube_mesh(verts)}</object>'
            '<object id="10" type="model"><components>'
            # 90° rotation around Z (column-major: X-basis maps to (0,1,0)).
            '<component objectid="1" transform="0 1 0 -1 0 0 0 0 1 0 0 0"/>'
            "</components></object>"
        )
        build_xml = (
            '<item objectid="10" transform="1 0 0 0 1 0 0 0 1 50 50 0"/>'
        )
        ms = (
            '<?xml version="1.0"?><config>'
            '<object id="10"><part id="1" subtype="normal_part"/></object>'
            '<plate><metadata key="plater_id" value="1"/>'
            '<model_instance><metadata key="object_id" value="10"/>'
            "</model_instance></plate>"
            "</config>"
        )

        out = extract_plate(self._build_3mf(objects_xml, build_xml, ms), 90, 90)
        self.assertIsNotNone(out)
        sx, sy, _ = _extracted_bbox(out)
        # After the 90° Z rotation, the local 6×2 footprint must become 2×6.
        self.assertAlmostEqual(sx, 2.0, places=4)
        self.assertAlmostEqual(sy, 6.0, places=4)

    def test_modifier_part_excluded_from_extracted_mesh(self) -> None:
        # Two components on the same parent: one normal_part (small) and one
        # modifier_part (huge). The extracted mesh must come from the normal
        # part only — otherwise the modifier blows up the bed-recentering bbox.
        small = [(0, 0, 0), (3, 0, 0), (0, 4, 0)]
        huge = [(0, 0, 0), (500, 0, 0), (0, 500, 0)]
        objects_xml = (
            f'<object id="1" type="model">{_cube_mesh(small)}</object>'
            f'<object id="2" type="model">{_cube_mesh(huge)}</object>'
            '<object id="10" type="model"><components>'
            '<component objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            "</components></object>"
        )
        build_xml = (
            '<item objectid="10" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        )
        ms = (
            '<?xml version="1.0"?><config>'
            '<object id="10">'
            '<part id="1" subtype="normal_part"/>'
            '<part id="2" subtype="modifier_part"/>'
            "</object>"
            '<plate><metadata key="plater_id" value="1"/>'
            '<model_instance><metadata key="object_id" value="10"/>'
            "</model_instance></plate>"
            "</config>"
        )

        out = extract_plate(self._build_3mf(objects_xml, build_xml, ms), 90, 90)
        self.assertIsNotNone(out)
        sx, sy, _ = _extracted_bbox(out)
        self.assertAlmostEqual(sx, 3.0, places=4)
        self.assertAlmostEqual(sy, 4.0, places=4)

    def test_submodel_path_component_with_rotation_is_honored(self) -> None:
        # A 3×4 triangle living in a sub-model file, referenced via p:path.
        # The component rotates it 90° around Z, so the extracted mesh's
        # footprint must be 4×3.
        verts = [(0, 0, 0), (3, 0, 0), (0, 4, 0)]
        sub_model = (
            f'<?xml version="1.0"?>'
            f"<model {_NS_DECL}>"
            "<resources>"
            f'<object id="5" type="model">{_cube_mesh(verts)}</object>'
            "</resources>"
            "</model>"
        )
        objects_xml = (
            '<object id="10" type="model"><components>'
            '<component p:path="/3D/Objects/sub.model" objectid="5" '
            'transform="0 1 0 -1 0 0 0 0 1 0 0 0"/>'
            "</components></object>"
        )
        build_xml = (
            '<item objectid="10" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        )
        ms = (
            '<?xml version="1.0"?><config>'
            '<object id="10"><part id="5" subtype="normal_part"/></object>'
            '<plate><metadata key="plater_id" value="1"/>'
            '<model_instance><metadata key="object_id" value="10"/>'
            "</model_instance></plate>"
            "</config>"
        )

        out = extract_plate(
            self._build_3mf(
                objects_xml, build_xml, ms,
                sub_models={"3D/Objects/sub.model": sub_model},
            ),
            90, 90,
        )
        self.assertIsNotNone(out)
        sx, sy, _ = _extracted_bbox(out)
        self.assertAlmostEqual(sx, 4.0, places=4)
        self.assertAlmostEqual(sy, 3.0, places=4)


class BoundingBoxRealBenchyTests(unittest.TestCase):
    """Regression test for the user-reported benchy 3MF — the file lives outside
    this repo, so the test is skipped when the fixture is unavailable.
    """

    FIXTURE = (
        "/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/"
        "_benchy-test/benchy-orca.3mf"
    )

    def test_benchy_with_six_modifier_parts_fits_180mm_bed(self) -> None:
        import os
        if not os.path.exists(self.FIXTURE):
            self.skipTest("benchy fixture not present")
        with open(self.FIXTURE, "rb") as f:
            bbox = get_bounding_box(f.read())
        self.assertIsNotNone(bbox)
        # The actual benchy is ~31x60x48mm; before the fix this reported ~395x429x62.
        self.assertLess(bbox.size_x, 70)
        self.assertLess(bbox.size_y, 70)
        self.assertLess(bbox.size_z, 60)
        # The user centered the model on the A1 Mini bed (180×180); the build
        # translation in the 3MF is roughly (90.5, 86.8). Before the chain-order
        # fix, the rotated component pushed Y to ~-90 and the model was
        # reported off-bed.
        center_x = (bbox.min_x + bbox.max_x) / 2
        center_y = (bbox.min_y + bbox.max_y) / 2
        self.assertAlmostEqual(center_x, 90, delta=5)
        self.assertAlmostEqual(center_y, 90, delta=5)


if __name__ == "__main__":
    unittest.main()
