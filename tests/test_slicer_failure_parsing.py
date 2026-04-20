import unittest

from app.slicer import _extract_critical_warnings


class ExtractCriticalWarningsTests(unittest.TestCase):
    def test_extracts_floating_regions_warning(self) -> None:
        output = (
            "[2026-04-20 11:40:12.480579] [0x7f6e498a6300] [debug]   "
            "default_status_callback: percent=50, warning_step=-1, "
            "message=Checking support necessity, message_type=0\n"
            "[2026-04-20 11:40:12.480938] [0x7f6e498a6300] [debug]   "
            "default_status_callback: percent=-1, warning_step=6, "
            "message=It seems object Octopus_sup_v6.stl has floating "
            "regions. Please re-orient the object or enable support "
            "generation., message_type=2\n"
            "[2026-04-20 11:40:12.481210] [0x7f6e498a6300] [debug]   "
            "default_status_callback: percent=70, warning_step=-1, "
            "message=Generating skirt & brim, message_type=0\n"
        )

        warnings = _extract_critical_warnings(output)

        self.assertEqual(len(warnings), 1)
        self.assertIn("floating regions", warnings[0])
        self.assertIn("enable support generation", warnings[0])

    def test_ignores_non_critical_messages(self) -> None:
        output = (
            "default_status_callback: percent=5, warning_step=-1, "
            "message=Slicing mesh, message_type=0\n"
            "default_status_callback: percent=50, warning_step=-1, "
            "message=Generating infill regions, message_type=1\n"
        )

        self.assertEqual(_extract_critical_warnings(output), [])

    def test_deduplicates_repeated_warnings(self) -> None:
        line = (
            "default_status_callback: percent=-1, warning_step=6, "
            "message=Floating regions detected., message_type=2"
        )
        output = f"{line}\n{line}\n{line}\n"

        self.assertEqual(
            _extract_critical_warnings(output),
            ["Floating regions detected."],
        )

    def test_returns_multiple_distinct_warnings(self) -> None:
        output = (
            "default_status_callback: percent=-1, warning_step=6, "
            "message=First problem., message_type=2\n"
            "default_status_callback: percent=-1, warning_step=7, "
            "message=Second problem., message_type=2\n"
        )

        self.assertEqual(
            _extract_critical_warnings(output),
            ["First problem.", "Second problem."],
        )

    def test_message_with_embedded_commas_is_captured_intact(self) -> None:
        output = (
            "default_status_callback: percent=-1, warning_step=6, "
            "message=Object X has floating regions, tentacles, and overhangs., "
            "message_type=2"
        )

        warnings = _extract_critical_warnings(output)

        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0],
            "Object X has floating regions, tentacles, and overhangs.",
        )

    def test_empty_output_returns_empty_list(self) -> None:
        self.assertEqual(_extract_critical_warnings(""), [])


if __name__ == "__main__":
    unittest.main()
