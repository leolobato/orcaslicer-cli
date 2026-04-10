import unittest

from app.slicer import _overlay_3mf_settings


class OverlaySettingsTests(unittest.TestCase):
    def test_overlay_skips_filament_assignment_keys(self) -> None:
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

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings)

        self.assertEqual(updated["wall_filament"], 1)
        self.assertEqual(updated["filament_settings_id"], ["PLA Basic @BBL A1M"])
        self.assertEqual(updated["sparse_infill_pattern"], "gyroid")
        self.assertIn("sparse_infill_pattern", overlaid_keys)
        self.assertNotIn("wall_filament", overlaid_keys)
        self.assertNotIn("filament_settings_id", overlaid_keys)

    def test_overlay_applies_all_matching_keys(self) -> None:
        process_profile = {
            "name": "Target",
            "setting_id": "GP004",
            "support_threshold_angle": "30",
            "enable_support": "0",
            "sparse_infill_density": "15%",
        }
        threemf_settings = {
            "support_threshold_angle": "25",
            "enable_support": "1",
            "sparse_infill_density": "15%",
        }

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings)

        self.assertEqual(updated["support_threshold_angle"], "25")
        self.assertEqual(updated["enable_support"], "1")
        self.assertEqual(updated["sparse_infill_density"], "15%")
        self.assertIn("support_threshold_angle", overlaid_keys)
        self.assertIn("enable_support", overlaid_keys)
        self.assertIn("sparse_infill_density", overlaid_keys)

    def test_overlay_skips_profile_metadata_keys(self) -> None:
        process_profile = {
            "name": "Target",
            "from": "system",
            "inherits": "base",
            "sparse_infill_pattern": "grid",
        }
        threemf_settings = {
            "name": "Overwritten",
            "from": "other",
            "inherits": "other_base",
            "sparse_infill_pattern": "gyroid",
        }

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings)

        self.assertEqual(updated["name"], "Target")
        self.assertEqual(updated["from"], "system")
        self.assertEqual(updated["inherits"], "base")
        self.assertEqual(updated["sparse_infill_pattern"], "gyroid")


if __name__ == "__main__":
    unittest.main()
