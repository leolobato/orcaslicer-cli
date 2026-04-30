import unittest

from app.slicer import (
    _extract_declared_customizations,
    _extract_declared_filament_customizations,
    _overlay_3mf_filament_settings,
    _overlay_3mf_settings,
)


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

    def test_overlay_injects_key_absent_from_process_profile(self) -> None:
        # `brim_type` is not written into any BBL process profile; OrcaSlicer
        # falls back to a compile-time default. When a 3MF declares it as a
        # customization, the overlay must inject it onto the target profile
        # so the user's choice survives. (Regression: previously a `k not in
        # process_profile` guard skipped this case.)
        process_profile = {
            "name": "0.20mm Standard @BBL A1M",
            "setting_id": "GP004",
            "sparse_infill_density": "15%",
        }
        threemf_settings = {
            "brim_type": "no_brim",
            "sparse_infill_density": "15%",
        }
        allowed = {"brim_type"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["brim_type"], "no_brim")
        self.assertEqual(overlaid_keys, {"brim_type"})

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


class DeclaredFilamentCustomizationsTests(unittest.TestCase):
    def test_extracts_per_filament_slots(self) -> None:
        threemf_settings = {
            "different_settings_to_system": [
                "sparse_infill_pattern",
                "",
                "nozzle_temperature;fan_min_speed",
                "filament_max_volumetric_speed",
            ],
        }
        result = _extract_declared_filament_customizations(threemf_settings)
        self.assertEqual(result, [
            {"nozzle_temperature", "fan_min_speed"},
            {"filament_max_volumetric_speed"},
        ])

    def test_returns_empty_when_no_filament_slots(self) -> None:
        threemf_settings = {"different_settings_to_system": ["a", ""]}
        self.assertEqual(_extract_declared_filament_customizations(threemf_settings), [])

    def test_returns_empty_when_field_missing(self) -> None:
        self.assertEqual(_extract_declared_filament_customizations({}), [])

    def test_blank_slot_yields_empty_set(self) -> None:
        threemf_settings = {"different_settings_to_system": ["", "", "", "nozzle_temperature"]}
        self.assertEqual(
            _extract_declared_filament_customizations(threemf_settings),
            [set(), {"nozzle_temperature"}],
        )


class OverlayFilamentSettingsTests(unittest.TestCase):
    def test_extracts_slot_value_from_combined_vector(self) -> None:
        filament_profile = {
            "name": "Bambu PLA Basic @BBL A1M",
            "nozzle_temperature": ["220"],
            "fan_min_speed": ["60"],
        }
        threemf_settings = {
            "nozzle_temperature": ["220", "225"],
            "fan_min_speed": ["60", "80"],
        }
        allowed = {"nozzle_temperature", "fan_min_speed"}

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=1, allowed_keys=allowed,
        )

        self.assertEqual(updated["nozzle_temperature"], ["225"])
        self.assertEqual(updated["fan_min_speed"], ["80"])
        by_key = {e["key"]: e for e in entries}
        self.assertEqual(by_key["nozzle_temperature"]["value"], "225")
        self.assertEqual(by_key["nozzle_temperature"]["original"], "220")
        self.assertEqual(by_key["fan_min_speed"]["value"], "80")

    def test_skips_key_when_slot_out_of_bounds(self) -> None:
        filament_profile = {"nozzle_temperature": ["220"]}
        threemf_settings = {"nozzle_temperature": ["220"]}  # only 1 slot in 3MF

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=1, allowed_keys={"nozzle_temperature"},
        )

        self.assertEqual(updated, filament_profile)
        self.assertEqual(entries, [])

    def test_skips_keys_not_in_allowlist(self) -> None:
        filament_profile = {"nozzle_temperature": ["220"], "fan_min_speed": ["60"]}
        threemf_settings = {"nozzle_temperature": ["225"], "fan_min_speed": ["80"]}

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=0, allowed_keys={"nozzle_temperature"},
        )

        self.assertEqual(updated["nozzle_temperature"], ["225"])
        self.assertEqual(updated["fan_min_speed"], ["60"])  # not in allowlist
        self.assertEqual([e["key"] for e in entries], ["nozzle_temperature"])

    def test_transfers_nothing_when_values_equal(self) -> None:
        filament_profile = {"nozzle_temperature": ["220"]}
        threemf_settings = {"nozzle_temperature": ["220"]}

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=0, allowed_keys={"nozzle_temperature"},
        )

        self.assertEqual(updated, filament_profile)
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()
