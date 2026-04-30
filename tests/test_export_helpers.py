import unittest

from app import profiles


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


if __name__ == "__main__":
    unittest.main()
