import io
import json
import unittest
import zipfile

from app.slice_request import parse_filament_profile_ids


def _build_project_3mf(filament_ids: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps({"filament_settings_id": filament_ids}),
        )
    return buf.getvalue()


class SliceRequestParsingTests(unittest.TestCase):
    def test_accepts_legacy_dense_list(self) -> None:
        result, error = parse_filament_profile_ids(
            '["GFSA00_02","GFSNLS03_07"]',
            _build_project_3mf(["DEFAULT0", "DEFAULT1"]),
        )

        self.assertIsNone(error)
        self.assertEqual(result, ["GFSA00_02", "GFSNLS03_07"])

    def test_applies_sparse_string_overrides_against_project_defaults(self) -> None:
        result, error = parse_filament_profile_ids(
            '{"1":"GFSNLS03_07"}',
            _build_project_3mf(["DEFAULT0", "DEFAULT1"]),
        )

        self.assertIsNone(error)
        self.assertEqual(result, ["DEFAULT0", "GFSNLS03_07"])

    def test_applies_tray_aware_override_objects(self) -> None:
        result, error = parse_filament_profile_ids(
            '{"0":{"profile_setting_id":"GFSA00_02","tray_slot":2}}',
            _build_project_3mf(["DEFAULT0", "DEFAULT1"]),
        )

        self.assertIsNone(error)
        self.assertEqual(result, ["GFSA00_02", "DEFAULT1"])

    def test_rejects_non_integer_tray_slot(self) -> None:
        result, error = parse_filament_profile_ids(
            '{"0":{"profile_setting_id":"GFSA00_02","tray_slot":"2"}}',
            _build_project_3mf(["DEFAULT0"]),
        )

        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(error, "tray_slot for project filament 0 must be an integer")

    def test_rejects_object_overrides_when_project_has_no_default_filament_ids(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Metadata/project_settings.config", json.dumps({}))

        result, error = parse_filament_profile_ids(
            '{"0":{"profile_setting_id":"GFSA00_02","tray_slot":2}}',
            buf.getvalue(),
        )

        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(
            error,
            "filament_profiles object format requires input 3MF project filament_settings_id entries",
        )


if __name__ == "__main__":
    unittest.main()
