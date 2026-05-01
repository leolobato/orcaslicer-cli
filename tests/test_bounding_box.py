import io
import unittest
import zipfile

from app.threemf import get_bounding_box


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


if __name__ == "__main__":
    unittest.main()
