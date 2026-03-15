import unittest
from unittest.mock import patch

from app.slicer import _smart_settings_transfer


class SlicerSettingsTransferTests(unittest.TestCase):
    def test_fallback_overlay_skips_filament_assignment_keys(self) -> None:
        process_profile = {
            "name": "Target",
            "setting_id": "GP004",
            "wall_filament": 1,
            "filament_settings_id": ["PLA Basic @BBL A1M"],
            "sparse_infill_pattern": "grid",
        }
        threemf_settings = {
            "wall_filament": 255,
            "filament_settings_id": ["External spool"],
            "sparse_infill_pattern": "gyroid",
        }

        updated, result = _smart_settings_transfer(process_profile, threemf_settings)

        self.assertEqual(result.status, "no_3mf_settings")
        self.assertEqual(updated["wall_filament"], 1)
        self.assertEqual(updated["filament_settings_id"], ["PLA Basic @BBL A1M"])
        self.assertEqual(updated["sparse_infill_pattern"], "gyroid")

    @patch("app.slicer.resolve_profile_by_name")
    def test_smart_transfer_does_not_apply_filament_assignment_customizations(self, mock_resolve) -> None:
        process_profile = {
            "name": "Target",
            "setting_id": "GP004",
            "wall_filament": 1,
            "filament_settings_id": ["PLA Basic @BBL A1M"],
            "sparse_infill_density": "15%",
        }
        original_profile = {
            "name": "Original",
            "setting_id": "GP001",
            "wall_filament": 1,
            "filament_settings_id": ["PLA Basic @BBL A1M"],
            "sparse_infill_density": "15%",
        }
        threemf_settings = {
            "print_settings_id": "Original",
            "wall_filament": 255,
            "filament_settings_id": ["External spool"],
            "sparse_infill_density": "25%",
        }
        mock_resolve.return_value = original_profile

        updated, result = _smart_settings_transfer(process_profile, threemf_settings)

        self.assertEqual(result.status, "applied")
        self.assertEqual(updated["wall_filament"], 1)
        self.assertEqual(updated["filament_settings_id"], ["PLA Basic @BBL A1M"])
        self.assertEqual(updated["sparse_infill_density"], "25%")
        self.assertEqual(result.transferred, [
            {"key": "sparse_infill_density", "value": '"25%"', "original": '"15%"'},
        ])


if __name__ == "__main__":
    unittest.main()
