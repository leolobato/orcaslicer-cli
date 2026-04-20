import io
import unittest
import zipfile

from app.threemf import get_used_filament_slots


def _make_3mf(slice_info: str | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "")
        if slice_info is not None:
            zf.writestr("Metadata/slice_info.config", slice_info)
    return buf.getvalue()


class GetUsedFilamentSlotsTests(unittest.TestCase):
    def test_single_filament_plate_returns_slot_zero(self) -> None:
        xml = (
            '<?xml version="1.0"?>'
            "<config><plate>"
            '<metadata key="index" value="1"/>'
            '<filament id="1" type="PLA" used_g="17.47"/>'
            "</plate></config>"
        )
        self.assertEqual(get_used_filament_slots(_make_3mf(xml), plate=1), {0})

    def test_picks_slots_from_requested_plate(self) -> None:
        xml = (
            '<?xml version="1.0"?>'
            "<config>"
            '<plate><metadata key="index" value="1"/>'
            '<filament id="1"/></plate>'
            '<plate><metadata key="index" value="2"/>'
            '<filament id="2"/><filament id="4"/></plate>'
            "</config>"
        )
        data = _make_3mf(xml)
        self.assertEqual(get_used_filament_slots(data, plate=1), {0})
        self.assertEqual(get_used_filament_slots(data, plate=2), {1, 3})

    def test_returns_none_when_slice_info_missing(self) -> None:
        self.assertIsNone(get_used_filament_slots(_make_3mf(None), plate=1))

    def test_returns_none_for_malformed_xml(self) -> None:
        self.assertIsNone(
            get_used_filament_slots(_make_3mf("<not-xml"), plate=1),
        )

    def test_returns_none_for_missing_plate(self) -> None:
        xml = (
            '<?xml version="1.0"?>'
            "<config><plate>"
            '<metadata key="index" value="1"/>'
            '<filament id="1"/>'
            "</plate></config>"
        )
        self.assertIsNone(get_used_filament_slots(_make_3mf(xml), plate=3))

    def test_plate_without_filaments_returns_empty_set(self) -> None:
        xml = (
            '<?xml version="1.0"?>'
            "<config><plate>"
            '<metadata key="index" value="1"/>'
            "</plate></config>"
        )
        # Distinct from None — we know the plate exists and uses no filaments.
        self.assertEqual(get_used_filament_slots(_make_3mf(xml), plate=1), set())

    def test_ignores_non_integer_ids(self) -> None:
        xml = (
            '<?xml version="1.0"?>'
            "<config><plate>"
            '<metadata key="index" value="1"/>'
            '<filament id="abc"/>'
            '<filament id="2"/>'
            "</plate></config>"
        )
        self.assertEqual(get_used_filament_slots(_make_3mf(xml), plate=1), {1})

    def test_ignores_non_zip_input(self) -> None:
        self.assertIsNone(get_used_filament_slots(b"not a zip file", plate=1))


if __name__ == "__main__":
    unittest.main()
