import unittest

from app.slicer import _normalize_filament_for_write


class NormalizeFilamentForWriteTests(unittest.TestCase):
    def test_wraps_scalar_filament_notes_into_list(self) -> None:
        profile = {"name": "X", "type": "filament", "from": "system", "filament_notes": ""}
        out = _normalize_filament_for_write(profile)
        self.assertEqual(out["filament_notes"], [""])

    def test_preserves_already_listed_filament_notes(self) -> None:
        profile = {"name": "X", "type": "filament", "from": "system", "filament_notes": ["hello"]}
        out = _normalize_filament_for_write(profile)
        self.assertEqual(out["filament_notes"], ["hello"])

    def test_leaves_other_keys_untouched(self) -> None:
        profile = {
            "name": "X",
            "type": "filament",
            "from": "system",
            "compatible_prints_condition": "",
            "filament_notes": "abc",
        }
        out = _normalize_filament_for_write(profile)
        self.assertEqual(out["compatible_prints_condition"], "")
        self.assertEqual(out["filament_notes"], ["abc"])

    def test_does_not_mutate_input(self) -> None:
        profile = {"name": "X", "filament_notes": ""}
        _normalize_filament_for_write(profile)
        self.assertEqual(profile["filament_notes"], "")
        self.assertNotIn("type", profile)
        self.assertNotIn("from", profile)

    def test_no_op_on_filament_notes_when_missing(self) -> None:
        profile = {"name": "X", "type": "filament", "from": "system"}
        out = _normalize_filament_for_write(profile)
        self.assertNotIn("filament_notes", out)

    def test_defaults_type_and_from_when_missing(self) -> None:
        profile = {"name": "X"}
        out = _normalize_filament_for_write(profile)
        self.assertEqual(out["type"], "filament")
        self.assertEqual(out["from"], "system")

    def test_preserves_existing_type_and_from(self) -> None:
        profile = {"name": "X", "type": "filament", "from": "User"}
        out = _normalize_filament_for_write(profile)
        self.assertEqual(out["type"], "filament")
        self.assertEqual(out["from"], "User")


if __name__ == "__main__":
    unittest.main()
