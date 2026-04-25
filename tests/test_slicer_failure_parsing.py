import tempfile
import unittest

from app.slicer import _build_failure, _extract_critical_warnings, _format_exit_reason


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


class FormatExitReasonTests(unittest.TestCase):
    def test_positive_returncode_reports_exit_code(self) -> None:
        self.assertEqual(_format_exit_reason(1), "OrcaSlicer exited with code 1")

    def test_sigsegv_reports_signal_name_and_mesh_hint(self) -> None:
        message = _format_exit_reason(-11)
        self.assertIn("SIGSEGV", message)
        self.assertIn("crashed", message)
        self.assertIn("malformed mesh", message)

    def test_sigabrt_reports_signal_name_and_mesh_hint(self) -> None:
        message = _format_exit_reason(-6)
        self.assertIn("SIGABRT", message)
        self.assertIn("malformed mesh", message)

    def test_sigterm_reports_signal_without_mesh_hint(self) -> None:
        message = _format_exit_reason(-15)
        self.assertIn("SIGTERM", message)
        self.assertNotIn("malformed mesh", message)


class BuildFailureTests(unittest.TestCase):
    def test_signal_kill_without_critical_warnings_uses_signal_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            err = _build_failure(-11, tmpdir, orca_output="some orca stdout")
        self.assertIn("SIGSEGV", str(err))
        self.assertEqual(err.critical_warnings, [])

    def test_critical_warnings_take_precedence_over_signal_message(self) -> None:
        output = (
            "default_status_callback: percent=-1, warning_step=6, "
            "message=Floating regions detected., message_type=2"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            err = _build_failure(-11, tmpdir, orca_output=output)
        self.assertIn("Floating regions", str(err))
        self.assertNotIn("SIGSEGV", str(err))


if __name__ == "__main__":
    unittest.main()
