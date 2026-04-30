import unittest

from app import profiles
from tests._profile_test_helpers import reset_profiles_state


class SafeFilenameTests(unittest.TestCase):
    def test_lowercases_and_replaces_unsafe_chars(self):
        self.assertEqual(
            profiles._safe_filename("Eryone Matte Imported", fallback="x"),
            "eryone_matte_imported.json",
        )

    def test_preserves_at_sign_replacement(self):
        # @ is not in [a-z0-9._-], so it gets replaced with _.
        result = profiles._safe_filename(
            "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
            fallback="x",
        )
        self.assertEqual(
            result,
            "eryone_matte_imported_bambu_lab_a1_mini_0.4_nozzle.json",
        )

    def test_collapses_runs_of_underscores(self):
        self.assertEqual(
            profiles._safe_filename("a !! b", fallback="x"),
            "a_b.json",
        )

    def test_trims_edges(self):
        self.assertEqual(
            profiles._safe_filename("  hello  ", fallback="x"),
            "hello.json",
        )

    def test_falls_back_when_empty(self):
        self.assertEqual(profiles._safe_filename("???", fallback="GFXX01"), "gfxx01.json")
        self.assertEqual(profiles._safe_filename("", fallback="GFXX01"), "gfxx01.json")

    def test_falls_back_to_literal_profile_when_both_empty(self):
        self.assertEqual(profiles._safe_filename("???", fallback="!!!"), "profile.json")


class FilamentAliasTests(unittest.TestCase):
    def test_strips_at_suffix(self):
        self.assertEqual(
            profiles._filament_alias("Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle"),
            "Eryone Matte Imported",
        )

    def test_returns_unchanged_when_no_at(self):
        self.assertEqual(
            profiles._filament_alias("Eryone Matte Imported"),
            "Eryone Matte Imported",
        )

    def test_strips_only_at_first_at(self):
        self.assertEqual(
            profiles._filament_alias("foo @bar @baz"),
            "foo",
        )

    def test_idempotent_on_already_stripped(self):
        # Re-export should not double-strip.
        once = profiles._filament_alias("PLA @Printer A")
        twice = profiles._filament_alias(once)
        self.assertEqual(once, twice)
        self.assertEqual(once, "PLA")


class PrinterVariantCountTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_profiles_state()

    def tearDown(self) -> None:
        reset_profiles_state()

    def _index_machine(self, name: str, raw: dict) -> None:
        # Mimic _index_profile but in-place to avoid disk loading.
        key = profiles._profile_key("BBL", name)
        profiles._raw_profiles[key] = {**raw, "name": name}
        profiles._type_map[key] = "machine"
        profiles._vendor_map[key] = "BBL"
        profiles._name_index.setdefault(name, []).append(key)

    def test_returns_length_of_printer_extruder_variant(self):
        self._index_machine(
            "Bambu Lab P1P 0.4 nozzle",
            {
                "printer_extruder_variant": ["Direct Drive Standard", "Direct Drive High Flow"],
            },
        )
        self.assertEqual(profiles._printer_variant_count("Bambu Lab P1P 0.4 nozzle"), 2)

    def test_returns_one_for_single_variant_printer(self):
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {
                "printer_extruder_variant": ["Direct Drive Standard"],
            },
        )
        self.assertEqual(profiles._printer_variant_count("Bambu Lab A1 mini 0.4 nozzle"), 1)

    def test_inherits_from_parent_when_field_absent(self):
        self._index_machine(
            "fdm_bbl_3dp_001_common",
            {
                "printer_extruder_variant": ["Direct Drive Standard", "Direct Drive High Flow"],
            },
        )
        self._index_machine(
            "Bambu Lab P1P 0.4 nozzle",
            {"inherits": "fdm_bbl_3dp_001_common"},
        )
        self.assertEqual(profiles._printer_variant_count("Bambu Lab P1P 0.4 nozzle"), 2)

    def test_returns_one_when_printer_unknown(self):
        self.assertEqual(profiles._printer_variant_count("Unknown Printer"), 1)

    def test_returns_one_when_field_missing_after_resolution(self):
        self._index_machine("Plain Printer", {})
        self.assertEqual(profiles._printer_variant_count("Plain Printer"), 1)


if __name__ == "__main__":
    unittest.main()
