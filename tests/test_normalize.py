import unittest

from app.normalize import _DEFAULTS, normalize_process_profile


class NormalizeProcessProfileTests(unittest.TestCase):
    def test_single_filament_is_noop(self) -> None:
        profile = {"name": "Target", "pressure_advance": ["0.025"]}

        result = normalize_process_profile(profile, 1)

        self.assertIs(result, profile)

    def test_zero_filaments_is_noop(self) -> None:
        profile = {"name": "Target"}

        result = normalize_process_profile(profile, 0)

        self.assertIs(result, profile)

    def test_missing_key_is_injected_at_target_length(self) -> None:
        profile = {"name": "Target"}

        result = normalize_process_profile(profile, 2)

        self.assertEqual(result["pressure_advance"], ["0.02", "0.02"])
        self.assertEqual(result["textured_cool_plate_temp"], ["40", "40"])
        self.assertEqual(result["filament_ironing_flow"], ["nil", "nil"])

    def test_length_one_vector_is_padded_by_repeat(self) -> None:
        profile = {
            "name": "Target",
            "pressure_advance": ["0.025"],
            "enable_pressure_advance": ["1"],
        }

        result = normalize_process_profile(profile, 3)

        self.assertEqual(result["pressure_advance"], ["0.025", "0.025", "0.025"])
        self.assertEqual(result["enable_pressure_advance"], ["1", "1", "1"])

    def test_partial_vector_pads_using_last_value(self) -> None:
        profile = {"name": "Target", "pressure_advance": ["0.025", "0.030"]}

        result = normalize_process_profile(profile, 4)

        self.assertEqual(result["pressure_advance"], ["0.025", "0.030", "0.030", "0.030"])

    def test_already_at_target_length_is_left_alone(self) -> None:
        profile = {"name": "Target", "pressure_advance": ["0.025", "0.030"]}

        result = normalize_process_profile(profile, 2)

        self.assertEqual(result["pressure_advance"], ["0.025", "0.030"])

    def test_longer_than_target_is_left_alone(self) -> None:
        profile = {"name": "Target", "pressure_advance": ["0.025", "0.030", "0.035"]}

        result = normalize_process_profile(profile, 2)

        self.assertEqual(result["pressure_advance"], ["0.025", "0.030", "0.035"])

    def test_scalar_in_vector_slot_gets_wrapped(self) -> None:
        profile = {"name": "Target", "pressure_advance": "0.025"}

        result = normalize_process_profile(profile, 2)

        self.assertEqual(result["pressure_advance"], ["0.025", "0.025"])

    def test_empty_list_is_injected_from_defaults(self) -> None:
        profile = {"name": "Target", "pressure_advance": []}

        result = normalize_process_profile(profile, 2)

        self.assertEqual(result["pressure_advance"], ["0.02", "0.02"])

    def test_input_dict_is_not_mutated(self) -> None:
        profile = {"name": "Target", "pressure_advance": ["0.025"]}

        result = normalize_process_profile(profile, 2)

        self.assertEqual(profile["pressure_advance"], ["0.025"])
        self.assertEqual(result["pressure_advance"], ["0.025", "0.025"])
        self.assertIsNot(result, profile)

    def test_unrelated_keys_are_preserved(self) -> None:
        profile = {
            "name": "Target",
            "sparse_infill_pattern": "gyroid",
            "layer_height": "0.16",
        }

        result = normalize_process_profile(profile, 2)

        self.assertEqual(result["name"], "Target")
        self.assertEqual(result["sparse_infill_pattern"], "gyroid")
        self.assertEqual(result["layer_height"], "0.16")

    def test_all_defaults_keys_are_covered_on_empty_profile(self) -> None:
        result = normalize_process_profile({}, 2)

        for key in _DEFAULTS:
            self.assertIn(key, result)
            self.assertEqual(len(result[key]), 2, msg=f"key {key} not padded to length 2")

    def test_ironing_flow_injects_nil_sentinel(self) -> None:
        """Nullable keys default to the literal 'nil' string per OrcaSlicer serialization."""
        result = normalize_process_profile({}, 2)

        self.assertEqual(result["filament_ironing_flow"], ["nil", "nil"])
        self.assertEqual(result["filament_ironing_inset"], ["nil", "nil"])
        self.assertEqual(result["filament_ironing_spacing"], ["nil", "nil"])
        self.assertEqual(result["filament_ironing_speed"], ["nil", "nil"])


if __name__ == "__main__":
    unittest.main()
