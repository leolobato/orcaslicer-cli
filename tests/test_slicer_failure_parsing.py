import json
import os
import tempfile
import unittest

from app.slicer import (
    _build_failure,
    _extract_critical_warnings,
    _extract_result_json_error,
    _extract_validation_errors,
    _format_exit_reason,
)


def _write_result_json(tmpdir: str, payload: dict) -> None:
    with open(os.path.join(tmpdir, "result.json"), "w") as f:
        json.dump(payload, f)


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


class ExtractValidationErrorsTests(unittest.TestCase):
    def test_extracts_organic_supports_validator_message(self) -> None:
        output = (
            "[2026-05-03 12:10:42.949462] [0x00007ffb6e375300] [error]   "
            "got error when validate: Variable layer height is not "
            "supported with Organic supports.\n"
            "Variable layer height is not supported with Organic supports.\n"
            "run found error, return -51, exit...\n"
        )

        errors = _extract_validation_errors(output)

        self.assertEqual(
            errors,
            ["Variable layer height is not supported with Organic supports."],
        )

    def test_returns_empty_when_no_validator_error(self) -> None:
        output = (
            "[2026-04-20 11:40:12.480579] [0x7f6e498a6300] [debug]   "
            "default_status_callback: percent=50, warning_step=-1, "
            "message=Slicing mesh, message_type=0\n"
        )

        self.assertEqual(_extract_validation_errors(output), [])

    def test_deduplicates_repeated_validator_errors(self) -> None:
        line = (
            "[error]   got error when validate: "
            "Variable layer height is not supported with Organic supports.\n"
        )
        self.assertEqual(
            _extract_validation_errors(line + line),
            ["Variable layer height is not supported with Organic supports."],
        )


class BuildFailureValidationTests(unittest.TestCase):
    def test_validation_error_preempts_critical_warning(self) -> None:
        output = (
            "default_status_callback: percent=-1, warning_step=6, "
            "message=Floating regions detected., message_type=2\n"
            "[error]   got error when validate: Variable layer height is "
            "not supported with Organic supports.\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            err = _build_failure(51, tmpdir, orca_output=output)

        self.assertIn("Variable layer height", str(err))
        self.assertNotIn("Floating regions", str(err))
        self.assertIn(
            "Variable layer height is not supported with Organic supports.",
            err.critical_warnings,
        )
        self.assertIn("Floating regions detected.", err.critical_warnings)

    def test_validation_error_preempts_signal_message(self) -> None:
        output = (
            "[error]   got error when validate: Variable layer height is "
            "not supported with Organic supports.\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            err = _build_failure(51, tmpdir, orca_output=output)

        self.assertIn("Variable layer height", str(err))
        self.assertNotIn("exited with code", str(err))


class ExtractResultJsonErrorTests(unittest.TestCase):
    def test_returns_error_string_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_result_json(tmpdir, {
                "return_code": 51,
                "error_string": "Variable layer height is not supported with Organic supports.",
            })
            self.assertEqual(
                _extract_result_json_error(tmpdir),
                "Variable layer height is not supported with Organic supports.",
            )

    def test_returns_empty_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(_extract_result_json_error(tmpdir), "")

    def test_returns_empty_when_json_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "result.json"), "w") as f:
                f.write("{not valid json")
            self.assertEqual(_extract_result_json_error(tmpdir), "")

    def test_returns_empty_when_field_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_result_json(tmpdir, {"return_code": 0})
            self.assertEqual(_extract_result_json_error(tmpdir), "")


class BuildFailureResultJsonPriorityTests(unittest.TestCase):
    def test_result_json_takes_priority_over_log_validator(self) -> None:
        # A different validate message in the log vs result.json — result.json wins.
        output = (
            "[error]   got error when validate: Stale log message we should "
            "ignore.\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_result_json(tmpdir, {
                "return_code": 38,
                "error_string": "Filaments are not compatible with the plate type.",
            })
            err = _build_failure(38, tmpdir, orca_output=output)

        self.assertIn("Filaments are not compatible", str(err))
        self.assertNotIn("Stale log message", str(err))

    def test_result_json_surfaces_non_validate_cli_errors(self) -> None:
        # A canonical `cli_errors` entry — proves the path covers generic
        # CLI failures (out-of-memory, file-not-found, etc.), not just
        # `Print::validate()` rejections.
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_result_json(tmpdir, {
                "return_code": 37,
                "error_string": "Object conflicts were detected when using print-by-object mode. Please verify the slicing of all plates in Orca Slicer before uploading.",
            })
            err = _build_failure(37, tmpdir, orca_output="")

        self.assertIn("Object conflicts were detected", str(err))

    def test_falls_back_to_validation_regex_when_result_json_missing(self) -> None:
        output = (
            "[error]   got error when validate: Variable layer height is "
            "not supported with Organic supports.\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            err = _build_failure(51, tmpdir, orca_output=output)

        self.assertIn("Variable layer height", str(err))


if __name__ == "__main__":
    unittest.main()
