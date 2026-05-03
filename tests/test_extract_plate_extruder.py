import io
import unittest
import zipfile

from app.threemf import _read_object_extruders, extract_plate


_NS = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'


def _make_3mf(model_xml: str, model_settings_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "")
        zf.writestr("3D/3dmodel.model", model_xml)
        zf.writestr("Metadata/model_settings.config", model_settings_xml)
    return buf.getvalue()


def _model_xml() -> str:
    verts = "".join(
        f'<vertex x="{x}" y="{y}" z="{z}"/>'
        for x, y, z in [(0, 0, 0), (10, 0, 0), (0, 10, 0)]
    )
    mesh = (
        f"<mesh><vertices>{verts}</vertices>"
        '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh>'
    )
    return (
        '<?xml version="1.0"?>'
        f"<model {_NS}>"
        f'<resources><object id="1" type="model">{mesh}</object></resources>'
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>'
        "</model>"
    )


def _model_settings(extruder: str | None) -> str:
    extruder_meta = (
        f'<metadata key="extruder" value="{extruder}"/>'
        if extruder is not None
        else ""
    )
    return (
        '<?xml version="1.0"?>'
        '<config>'
        '<plate>'
        '<metadata key="plater_id" value="1"/>'
        '<model_instance>'
        '<metadata key="object_id" value="1"/>'
        '<metadata key="instance_id" value="0"/>'
        '<metadata key="identify_id" value="42"/>'
        '</model_instance>'
        '</plate>'
        '<object id="1">'
        '<metadata key="name" value="Part"/>'
        f"{extruder_meta}"
        '<part id="1" subtype="normal_part"/>'
        '</object>'
        '</config>'
    )


class ReadObjectExtrudersTests(unittest.TestCase):
    def test_reads_per_object_extruder(self) -> None:
        ms = _model_settings(extruder="7")
        with zipfile.ZipFile(io.BytesIO(_make_3mf(_model_xml(), ms))) as zf:
            self.assertEqual(_read_object_extruders(zf), {"1": "7"})

    def test_omits_object_when_extruder_metadata_missing(self) -> None:
        ms = _model_settings(extruder=None)
        with zipfile.ZipFile(io.BytesIO(_make_3mf(_model_xml(), ms))) as zf:
            self.assertEqual(_read_object_extruders(zf), {})

    def test_returns_empty_when_model_settings_missing(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "")
            zf.writestr("3D/3dmodel.model", _model_xml())
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            self.assertEqual(_read_object_extruders(zf), {})


class ExtractPlatePropagatesExtruderTests(unittest.TestCase):
    def test_rebuilt_plate_carries_source_extruder(self) -> None:
        src = _make_3mf(_model_xml(), _model_settings(extruder="7"))

        out = extract_plate(src)
        self.assertIsNotNone(out)

        with zipfile.ZipFile(io.BytesIO(out)) as zf:
            ms = zf.read("Metadata/model_settings.config").decode()

        self.assertIn('key="extruder" value="7"', ms)
        self.assertNotIn('key="extruder" value="1"', ms)

    def test_rebuilt_plate_defaults_to_one_when_source_lacks_extruder(self) -> None:
        src = _make_3mf(_model_xml(), _model_settings(extruder=None))

        out = extract_plate(src)
        self.assertIsNotNone(out)

        with zipfile.ZipFile(io.BytesIO(out)) as zf:
            ms = zf.read("Metadata/model_settings.config").decode()

        self.assertIn('key="extruder" value="1"', ms)


if __name__ == "__main__":
    unittest.main()
