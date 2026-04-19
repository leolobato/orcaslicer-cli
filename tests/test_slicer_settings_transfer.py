import unittest

from app.slicer import _extract_declared_customizations, _overlay_3mf_settings


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
        allowed = {"wall_filament", "filament_settings_id", "sparse_infill_pattern"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["wall_filament"], 1)
        self.assertEqual(updated["filament_settings_id"], ["PLA Basic @BBL A1M"])
        self.assertEqual(updated["sparse_infill_pattern"], "gyroid")
        self.assertIn("sparse_infill_pattern", overlaid_keys)
        self.assertNotIn("wall_filament", overlaid_keys)
        self.assertNotIn("filament_settings_id", overlaid_keys)

    def test_overlay_applies_allowlisted_keys(self) -> None:
        process_profile = {
            "name": "Target",
            "setting_id": "GP004",
            "support_threshold_angle": "30",
            "enable_support": "0",
            "sparse_infill_density": "15%",
            "bottom_surface_pattern": "monotonic",
        }
        threemf_settings = {
            "support_threshold_angle": "25",
            "enable_support": "1",
            "sparse_infill_density": "15%",
            "bottom_surface_pattern": "monotonicline",
        }
        allowed = {"support_threshold_angle", "enable_support", "sparse_infill_density"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["support_threshold_angle"], "25")
        self.assertEqual(updated["enable_support"], "1")
        self.assertEqual(updated["sparse_infill_density"], "15%")
        # Not in the allowlist — must not be overlaid even though the 3MF has a different value.
        self.assertEqual(updated["bottom_surface_pattern"], "monotonic")
        self.assertEqual(overlaid_keys, allowed)

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
        allowed = {"name", "from", "inherits", "sparse_infill_pattern"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["name"], "Target")
        self.assertEqual(updated["from"], "system")
        self.assertEqual(updated["inherits"], "base")
        self.assertEqual(updated["sparse_infill_pattern"], "gyroid")
        self.assertEqual(overlaid_keys, {"sparse_infill_pattern"})

    def test_overlay_transfers_nothing_when_allowlist_empty(self) -> None:
        process_profile = {
            "sparse_infill_pattern": "grid",
            "support_threshold_angle": "30",
        }
        threemf_settings = {
            "sparse_infill_pattern": "gyroid",
            "support_threshold_angle": "25",
        }

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, set())

        self.assertEqual(updated, process_profile)
        self.assertEqual(overlaid_keys, set())


class DeclaredCustomizationsTests(unittest.TestCase):
    def test_extracts_process_slot_only(self) -> None:
        threemf_settings = {
            "different_settings_to_system": [
                "sparse_infill_density;sparse_infill_pattern",
                "",
                "fan_min_speed;nozzle_temperature",
                "",
            ],
        }
        self.assertEqual(
            _extract_declared_customizations(threemf_settings),
            {"sparse_infill_density", "sparse_infill_pattern"},
        )

    def test_returns_empty_when_field_missing(self) -> None:
        self.assertEqual(_extract_declared_customizations({}), set())

    def test_returns_empty_when_process_slot_blank(self) -> None:
        threemf_settings = {"different_settings_to_system": ["", "", "", ""]}
        self.assertEqual(_extract_declared_customizations(threemf_settings), set())


if __name__ == "__main__":
    unittest.main()
