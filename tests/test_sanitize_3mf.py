import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.slicer import _sanitize_3mf, _strip_plater_name_metadata


def _write_3mf(path: Path, settings: dict | None, model_settings_xml: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "")
        if settings is not None:
            zf.writestr(
                "Metadata/project_settings.config",
                json.dumps(settings),
            )
        if model_settings_xml is not None:
            zf.writestr(
                "Metadata/model_settings.config",
                model_settings_xml,
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


class StripPlaterNameMetadataTests(unittest.TestCase):
    """Standalone string-level tests for the regex helper."""

    def test_strips_a_single_plater_name_metadata_entry(self) -> None:
        xml = (
            '<config>\n'
            '  <plate>\n'
            '    <metadata key="plater_id" value="1"/>\n'
            '    <metadata key="plater_name" value="5th Edition"/>\n'
            '    <metadata key="locked" value="false"/>\n'
            '  </plate>\n'
            '</config>\n'
        )

        out, n = _strip_plater_name_metadata(xml)

        self.assertEqual(n, 1)
        self.assertNotIn("plater_name", out)
        self.assertIn('<metadata key="plater_id" value="1"/>', out)
        self.assertIn('<metadata key="locked" value="false"/>', out)

    def test_strips_multiple_plater_name_entries(self) -> None:
        xml = (
            '<config>\n'
            '  <plate>\n'
            '    <metadata key="plater_name" value="A"/>\n'
            '  </plate>\n'
            '  <plate>\n'
            '    <metadata key="plater_name" value="B with spaces"/>\n'
            '  </plate>\n'
            '</config>\n'
        )

        out, n = _strip_plater_name_metadata(xml)

        self.assertEqual(n, 2)
        self.assertNotIn("plater_name", out)

    def test_does_not_match_keys_that_only_contain_plater_name_substring(self) -> None:
        xml = '<metadata key="my_plater_name_field" value="X"/>\n'

        out, n = _strip_plater_name_metadata(xml)

        self.assertEqual(n, 0)
        self.assertEqual(out, xml)

    def test_returns_unchanged_xml_when_no_plater_name_present(self) -> None:
        xml = '<config><plate><metadata key="plater_id" value="1"/></plate></config>'

        out, n = _strip_plater_name_metadata(xml)

        self.assertEqual(n, 0)
        self.assertEqual(out, xml)


class SanitizePlaterNameInZipTests(unittest.TestCase):
    """End-to-end: zip roundtrip strips plater_name from model_settings.config."""

    _MODEL_SETTINGS = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n'
        '  <object id="2">\n'
        '    <metadata key="name" value="Ed 5.stl"/>\n'
        '  </object>\n'
        '  <plate>\n'
        '    <metadata key="plater_id" value="1"/>\n'
        '    <metadata key="plater_name" value="5th Edition"/>\n'
        '    <metadata key="locked" value="false"/>\n'
        '  </plate>\n'
        '</config>\n'
    )

    def test_strips_plater_name_when_no_other_changes_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, settings=None, model_settings_xml=self._MODEL_SETTINGS)

            out = _sanitize_3mf(str(src), tmp, machine_profile=None)

            self.assertNotEqual(out, str(src))
            with zipfile.ZipFile(out, "r") as zf:
                xml = zf.read("Metadata/model_settings.config").decode()
            self.assertNotIn("plater_name", xml)
            self.assertIn('<metadata key="plater_id" value="1"/>', xml)
            self.assertIn('<metadata key="locked" value="false"/>', xml)

    def test_no_op_when_model_settings_has_no_plater_name(self) -> None:
        clean = (
            '<config><plate><metadata key="plater_id" value="1"/></plate></config>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, settings=None, model_settings_xml=clean)

            out = _sanitize_3mf(str(src), tmp, machine_profile=None)

            # Nothing to change → original returned.
            self.assertEqual(out, str(src))

    def test_strip_combines_with_clamp_rule_in_one_sanitized_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(
                src,
                settings={"raft_first_layer_expansion": "-1"},
                model_settings_xml=self._MODEL_SETTINGS,
            )

            out = _sanitize_3mf(str(src), tmp, machine_profile=None)

            self.assertNotEqual(out, str(src))
            with zipfile.ZipFile(out, "r") as zf:
                proj = json.loads(zf.read("Metadata/project_settings.config").decode())
                model_xml = zf.read("Metadata/model_settings.config").decode()
            self.assertEqual(proj["raft_first_layer_expansion"], "0")
            self.assertNotIn("plater_name", model_xml)


if __name__ == "__main__":
    unittest.main()
