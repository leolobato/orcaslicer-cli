import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.slicer import _sanitize_3mf


def _write_3mf(path: Path, settings: dict | None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "")
        if settings is not None:
            zf.writestr(
                "Metadata/project_settings.config",
                json.dumps(settings),
            )


def _read_settings(path: str) -> dict:
    with zipfile.ZipFile(path, "r") as zf:
        return json.loads(
            zf.read("Metadata/project_settings.config").decode()
        )


class SanitizeRebrandTests(unittest.TestCase):
    def test_rebrands_printer_model_and_settings_id_to_match_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {
                "printer_model": "Bambu Lab P1P",
                "printer_settings_id": "Bambu Lab P1P 0.4 nozzle",
            })
            machine = {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "printer_model": "Bambu Lab A1 mini",
            }

            out = _sanitize_3mf(str(src), tmp, machine)

            self.assertNotEqual(out, str(src))
            s = _read_settings(out)
            self.assertEqual(s["printer_model"], "Bambu Lab A1 mini")
            self.assertEqual(s["printer_settings_id"], "Bambu Lab A1 mini 0.4 nozzle")

    def test_no_rewrite_when_already_matches_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {
                "printer_model": "Bambu Lab A1 mini",
                "printer_settings_id": "Bambu Lab A1 mini 0.4 nozzle",
            })
            machine = {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "printer_model": "Bambu Lab A1 mini",
            }

            out = _sanitize_3mf(str(src), tmp, machine)

            # No rewrite → original file is returned as-is.
            self.assertEqual(out, str(src))

    def test_adds_printer_identity_when_absent_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {})
            machine = {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "printer_model": "Bambu Lab A1 mini",
            }

            out = _sanitize_3mf(str(src), tmp, machine)

            self.assertNotEqual(out, str(src))
            s = _read_settings(out)
            self.assertEqual(s["printer_model"], "Bambu Lab A1 mini")
            self.assertEqual(s["printer_settings_id"], "Bambu Lab A1 mini 0.4 nozzle")

    def test_skips_rebrand_when_machine_profile_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {
                "printer_model": "Bambu Lab P1P",
            })

            out = _sanitize_3mf(str(src), tmp, None)

            # No changes requested → original returned.
            self.assertEqual(out, str(src))

    def test_no_op_when_project_settings_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, None)
            machine = {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "printer_model": "Bambu Lab A1 mini",
            }

            out = _sanitize_3mf(str(src), tmp, machine)

            self.assertEqual(out, str(src))

    def test_clamp_rule_still_applied_alongside_rebrand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {
                "printer_model": "Bambu Lab P1P",
                "wall_filament": 0,  # below the clamp minimum of 1
            })
            machine = {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "printer_model": "Bambu Lab A1 mini",
            }

            out = _sanitize_3mf(str(src), tmp, machine)

            s = _read_settings(out)
            self.assertEqual(s["printer_model"], "Bambu Lab A1 mini")
            self.assertEqual(s["wall_filament"], 1)


if __name__ == "__main__":
    unittest.main()
