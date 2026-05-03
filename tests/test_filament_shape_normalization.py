import unittest

from app.slicer import _normalize_filament_vector_shapes


class NormalizeFilamentVectorShapesTests(unittest.TestCase):
    def test_wraps_scalar_filament_notes_into_list(self) -> None:
        profile = {"name": "X", "filament_notes": ""}
        out = _normalize_filament_vector_shapes(profile)
        self.assertEqual(out["filament_notes"], [""])

    def test_preserves_already_listed_filament_notes(self) -> None:
        profile = {"name": "X", "filament_notes": ["hello"]}
        out = _normalize_filament_vector_shapes(profile)
        self.assertEqual(out["filament_notes"], ["hello"])

    def test_leaves_other_keys_untouched(self) -> None:
        profile = {
            "name": "X",
            "compatible_prints_condition": "",
            "filament_notes": "abc",
        }
        out = _normalize_filament_vector_shapes(profile)
        self.assertEqual(out["compatible_prints_condition"], "")
        self.assertEqual(out["filament_notes"], ["abc"])

    def test_does_not_mutate_input(self) -> None:
        profile = {"name": "X", "filament_notes": ""}
        _normalize_filament_vector_shapes(profile)
        self.assertEqual(profile["filament_notes"], "")

    def test_no_op_when_key_missing(self) -> None:
        profile = {"name": "X"}
        out = _normalize_filament_vector_shapes(profile)
        self.assertEqual(out, profile)


if __name__ == "__main__":
    unittest.main()
