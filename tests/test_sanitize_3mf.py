import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.slicer import (
    _resize_flush_volumes,
    _sanitize_3mf,
    _strip_plater_name_metadata,
    _truncate_per_filament_lists,
    _truncate_structural_arrays,
)


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


class ResizeFlushVolumesTests(unittest.TestCase):
    def test_shrinks_matrix_to_single_filament(self) -> None:
        settings = {
            "flush_volumes_matrix": [
                "0", "266", "271",
                "199", "0", "309",
                "251", "207", "0",
            ],
            "flush_volumes_vector": ["140", "140"] * 3,
        }

        changed = _resize_flush_volumes(settings, target_n=1)

        self.assertTrue(changed)
        self.assertEqual(settings["flush_volumes_matrix"], ["0"])
        self.assertEqual(settings["flush_volumes_vector"], ["140", "140"])

    def test_preserves_existing_pairs_when_shrinking(self) -> None:
        settings = {
            "flush_volumes_matrix": [
                "0", "10", "20",
                "30", "0", "40",
                "50", "60", "0",
            ],
        }

        _resize_flush_volumes(settings, target_n=2)

        self.assertEqual(
            settings["flush_volumes_matrix"],
            ["0", "10", "30", "0"],
        )

    def test_grows_matrix_with_defaults(self) -> None:
        settings = {
            "flush_volumes_matrix": ["0", "10", "30", "0"],
        }

        _resize_flush_volumes(settings, target_n=3)

        self.assertEqual(
            settings["flush_volumes_matrix"],
            ["0", "10", "140", "30", "0", "140", "140", "140", "0"],
        )

    def test_no_op_when_size_already_matches(self) -> None:
        settings = {
            "flush_volumes_matrix": ["0", "10", "30", "0"],
            "flush_volumes_vector": ["140", "140", "140", "140"],
        }
        snapshot = {k: list(v) for k, v in settings.items()}

        changed = _resize_flush_volumes(settings, target_n=2)

        self.assertFalse(changed)
        self.assertEqual(settings, snapshot)

    def test_skips_when_target_count_zero(self) -> None:
        settings = {"flush_volumes_matrix": ["0", "10", "30", "0"]}

        changed = _resize_flush_volumes(settings, target_n=0)

        self.assertFalse(changed)
        self.assertEqual(settings["flush_volumes_matrix"], ["0", "10", "30", "0"])

    def test_handles_malformed_matrix_length(self) -> None:
        # Length 3 isn't a perfect square — treat as empty and rebuild.
        settings = {"flush_volumes_matrix": ["0", "10", "20"]}

        _resize_flush_volumes(settings, target_n=2)

        self.assertEqual(
            settings["flush_volumes_matrix"],
            ["0", "140", "140", "0"],
        )

    def test_resizes_flush_multiplier_to_match_nozzle_count(self) -> None:
        """flush_multiplier is per-nozzle; if the 3MF was authored on a
        multi-nozzle printer and we're slicing for a single-nozzle one,
        OrcaSlicer's gcode-export size check
        (``filament_count^2 * flush_multiplier.size() == matrix.size()``)
        fails unless flush_multiplier is also resized."""
        settings = {
            "flush_volumes_matrix": ["0", "10", "30", "0"] * 2,  # 2 heads
            "flush_multiplier": ["0.7", "0.7"],
        }

        _resize_flush_volumes(settings, target_n=2, nozzle_count=1)

        self.assertEqual(settings["flush_multiplier"], ["0.7"])
        # Matrix should now be N*N*1 = 4 entries.
        self.assertEqual(len(settings["flush_volumes_matrix"]), 4)

    def test_grows_flush_multiplier_for_multi_nozzle_target(self) -> None:
        settings = {
            "flush_volumes_matrix": ["0", "10", "30", "0"],
            "flush_multiplier": ["1"],
        }

        _resize_flush_volumes(settings, target_n=2, nozzle_count=2)

        self.assertEqual(settings["flush_multiplier"], ["1", "1"])
        self.assertEqual(len(settings["flush_volumes_matrix"]), 8)

    def test_normalizes_string_flush_multiplier_to_list(self) -> None:
        # Bambu sometimes serializes `flush_multiplier` as a pipe-separated
        # string instead of a JSON list — Orca's gcode-export size check
        # parses it as `ConfigOptionFloats`, but our resize logic only saw a
        # list and silently skipped it, leaving the size mismatch in place.
        settings = {
            "flush_volumes_matrix": ["0"] * 49,
            "flush_multiplier": "1|1",
        }

        _resize_flush_volumes(settings, target_n=7, nozzle_count=1)

        self.assertEqual(settings["flush_multiplier"], ["1"])

    def test_normalizes_scalar_flush_multiplier_to_list(self) -> None:
        settings = {
            "flush_volumes_matrix": ["0"] * 49,
            "flush_multiplier": "1",
        }

        _resize_flush_volumes(settings, target_n=7, nozzle_count=1)

        self.assertEqual(settings["flush_multiplier"], ["1"])


    def test_resize_writes_through_sanitize_3mf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {
                "flush_volumes_matrix": [
                    "0", "266", "271",
                    "199", "0", "309",
                    "251", "207", "0",
                ],
                "flush_volumes_vector": ["140"] * 6,
            })

            out = _sanitize_3mf(str(src), tmp, None, target_filament_count=1)

            self.assertNotEqual(out, str(src))
            s = _read_settings(out)
            self.assertEqual(s["flush_volumes_matrix"], ["0"])
            self.assertEqual(s["flush_volumes_vector"], ["140", "140"])


class TruncatePerFilamentListsTests(unittest.TestCase):
    def test_truncates_known_per_filament_lists_to_target_n(self) -> None:
        settings = {
            "filament_settings_id": ["A", "B", "C", "D"],
            "filament_colour": ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"],
            "filament_type": ["PLA", "PLA", "PETG", "PLA"],
            "nozzle_temperature": ["220", "220", "240", "220"],
            "hot_plate_temp": ["55", "55", "70", "55"],
        }

        touched = _truncate_per_filament_lists(settings, target_n=1)

        self.assertEqual(set(touched), {
            "filament_settings_id", "filament_colour", "filament_type",
            "nozzle_temperature", "hot_plate_temp",
        })
        for key in touched:
            self.assertEqual(len(settings[key]), 1)
        self.assertEqual(settings["filament_colour"], ["#FF0000"])
        self.assertEqual(settings["filament_type"], ["PLA"])

    def test_skips_flush_matrix_and_vector(self) -> None:
        settings = {
            "filament_settings_id": ["A", "B", "C", "D"],
            "flush_volumes_matrix": ["0"] * 16,
            "flush_volumes_vector": ["140"] * 8,
        }

        touched = _truncate_per_filament_lists(settings, target_n=1)

        self.assertEqual(touched, {"filament_settings_id": 4})
        self.assertEqual(len(settings["flush_volumes_matrix"]), 16)
        self.assertEqual(len(settings["flush_volumes_vector"]), 8)

    def test_ignores_lists_that_do_not_match_filament_count(self) -> None:
        # `compatible_printers` etc. happen to be lists but aren't per-filament.
        settings = {
            "filament_settings_id": ["A", "B", "C", "D"],
            "compatible_printers": ["P1S", "X1C"],
            "filament_colour": ["#000"] * 4,
        }

        _truncate_per_filament_lists(settings, target_n=1)

        self.assertEqual(settings["compatible_printers"], ["P1S", "X1C"])
        self.assertEqual(settings["filament_colour"], ["#000"])

    def test_does_not_touch_keys_that_coincidentally_match_filament_count(self) -> None:
        # Real percussion-frog-instrument.3mf shape: 4 authored filaments,
        # plus several non-per-filament keys that happen to be length-4:
        #   - bed_exclude_area / printable_area: 4 polygon vertices (coPoints)
        #   - print_compatible_printers: 4 printer names (coStrings)
        #   - chamber_temperatures: per-print-condition values
        # Truncating any of these to 1 corrupts the project. bed_exclude_area
        # at length 1 turns a polygon into a single point, which segfaults
        # OrcaSlicer during area computation (we hit this in production once).
        settings = {
            "filament_settings_id": ["A", "B", "C", "D"],
            "filament_colour": ["#000", "#111", "#222", "#333"],
            "bed_exclude_area": [
                {"x": 0, "y": 0}, {"x": 100, "y": 0},
                {"x": 100, "y": 50}, {"x": 0, "y": 50},
            ],
            "printable_area": [
                {"x": 0, "y": 0}, {"x": 180, "y": 0},
                {"x": 180, "y": 180}, {"x": 0, "y": 180},
            ],
            "print_compatible_printers": [
                "Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab X1 0.4 nozzle",
                "Bambu Lab P1S 0.4 nozzle", "Bambu Lab X1E 0.4 nozzle",
            ],
            "chamber_temperatures": ["0", "0", "0", "0"],
        }

        touched = _truncate_per_filament_lists(settings, target_n=1)

        self.assertIn("filament_settings_id", touched)
        self.assertIn("filament_colour", touched)
        self.assertNotIn("bed_exclude_area", touched)
        self.assertNotIn("printable_area", touched)
        self.assertNotIn("print_compatible_printers", touched)
        self.assertNotIn("chamber_temperatures", touched)
        self.assertEqual(len(settings["bed_exclude_area"]), 4)
        self.assertEqual(len(settings["printable_area"]), 4)
        self.assertEqual(len(settings["print_compatible_printers"]), 4)
        self.assertEqual(len(settings["chamber_temperatures"]), 4)

    def test_no_op_when_target_already_matches(self) -> None:
        settings = {
            "filament_settings_id": ["A"],
            "filament_colour": ["#000"],
        }

        touched = _truncate_per_filament_lists(settings, target_n=1)

        self.assertEqual(touched, {})

    def test_no_op_when_target_n_zero_or_negative(self) -> None:
        settings = {
            "filament_settings_id": ["A", "B"],
            "filament_colour": ["#000", "#FFF"],
        }

        self.assertEqual(_truncate_per_filament_lists(settings, target_n=0), {})
        self.assertEqual(_truncate_per_filament_lists(settings, target_n=-1), {})
        self.assertEqual(settings["filament_colour"], ["#000", "#FFF"])

    def test_truncates_even_without_filament_settings_id_anchor(self) -> None:
        # Per-filament keys longer than target_n must be shrunk regardless of
        # whether the 3MF has the conventional `filament_settings_id` anchor;
        # otherwise OrcaSlicer's `filament_colour.size()² × heads ==
        # matrix.size()` check trips at G-code export.
        settings = {
            "filament_colour": ["#000", "#FFF", "#F0F", "#0FF"],
            "nozzle_temperature": ["220", "220", "220", "220"],
        }

        touched = _truncate_per_filament_lists(settings, target_n=1)

        self.assertEqual(touched, {"filament_colour": 4, "nozzle_temperature": 4})
        self.assertEqual(settings["filament_colour"], ["#000"])
        self.assertEqual(settings["nozzle_temperature"], ["220"])

    def test_writes_through_sanitize_3mf(self) -> None:
        # Reproduces the percussion-frog 3MF shape: 4 authored filaments,
        # plate uses only slot 0, slice runs with target_filament_count=1.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {
                "filament_settings_id": [
                    "Bambu PLA Marble @BBL A1M",
                    "Bambu PLA Marble @BBL A1M",
                    "Bambu PLA Marble @BBL A1M",
                    "Bambu PLA Marble @BBL A1M",
                ],
                "filament_colour": ["#9B9EA0", "#9B9EA0", "#9B9EA0", "#9B9EA0"],
                "filament_type": ["PLA", "PLA", "PLA", "PLA"],
                "nozzle_temperature": ["220", "220", "220", "220"],
                "flush_volumes_matrix": ["0"] * 16,
                "flush_volumes_vector": ["140"] * 8,
            })

            out = _sanitize_3mf(str(src), tmp, None, target_filament_count=1)

            self.assertNotEqual(out, str(src))
            s = _read_settings(out)
            # Matrix/vector resized via _resize_flush_volumes.
            self.assertEqual(len(s["flush_volumes_matrix"]), 1)
            self.assertEqual(len(s["flush_volumes_vector"]), 2)
            # Per-filament lists truncated via _truncate_per_filament_lists —
            # this is the fix for "Flush volumes matrix do not match to the
            # correct size!" at G-code export.
            self.assertEqual(len(s["filament_colour"]), 1)
            self.assertEqual(len(s["filament_settings_id"]), 1)
            self.assertEqual(len(s["filament_type"]), 1)
            self.assertEqual(len(s["nozzle_temperature"]), 1)


class TruncateStructuralArraysTests(unittest.TestCase):
    def test_truncates_inherits_group_preserving_process_and_printer(self) -> None:
        # Shape: [process, fil_1, fil_2, fil_3, fil_4, printer]
        settings = {
            "filament_settings_id": ["A", "B", "C", "D"],
            "inherits_group": [
                "0.20mm Standard @BBL X1C",
                "fil_1_inherit", "fil_2_inherit",
                "fil_3_inherit", "fil_4_inherit",
                "Bambu Lab X1E 0.4 nozzle",
            ],
        }

        touched = _truncate_structural_arrays(settings, target_n=1)

        self.assertEqual(touched, {"inherits_group": 6})
        self.assertEqual(settings["inherits_group"], [
            "0.20mm Standard @BBL X1C",
            "fil_1_inherit",
            "Bambu Lab X1E 0.4 nozzle",
        ])

    def test_truncates_different_settings_to_system(self) -> None:
        settings = {
            "filament_settings_id": ["A", "B", "C", "D"],
            "different_settings_to_system": [
                "enable_support;sparse_infill_density",
                "fil_1_diff", "fil_2_diff", "fil_3_diff", "fil_4_diff",
                "printer_diff",
            ],
        }

        touched = _truncate_structural_arrays(settings, target_n=2)

        self.assertEqual(touched, {"different_settings_to_system": 6})
        self.assertEqual(settings["different_settings_to_system"], [
            "enable_support;sparse_infill_density",
            "fil_1_diff", "fil_2_diff",
            "printer_diff",
        ])

    def test_skips_arrays_with_unexpected_length(self) -> None:
        # Length doesn't match N+2 — leave it alone.
        settings = {
            "filament_settings_id": ["A", "B", "C", "D"],
            "inherits_group": ["a", "b", "c"],
        }

        touched = _truncate_structural_arrays(settings, target_n=1)

        self.assertEqual(touched, {})
        self.assertEqual(settings["inherits_group"], ["a", "b", "c"])

    def test_no_op_when_target_already_matches(self) -> None:
        settings = {
            "filament_settings_id": ["A"],
            "inherits_group": ["proc", "fil_1", "printer"],
        }

        self.assertEqual(_truncate_structural_arrays(settings, target_n=1), {})

    def test_no_op_when_filament_settings_id_missing(self) -> None:
        settings = {
            "inherits_group": ["a", "b", "c", "d", "e", "f"],
        }
        snapshot = list(settings["inherits_group"])

        self.assertEqual(_truncate_structural_arrays(settings, target_n=1), {})
        self.assertEqual(settings["inherits_group"], snapshot)

    def test_writes_through_sanitize_3mf_with_percussion_frog_shape(self) -> None:
        # Reproduces the percussion-frog crash: 4 authored filaments, sliced
        # with 1. Without truncating `inherits_group`, OrcaSlicer 2.3.2
        # SIGSEGVs in `OrcaSlicer.cpp:1647-1655` because `inherits_group`
        # iterates past the end of the truncated `filament_settings_id`.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.3mf"
            _write_3mf(src, {
                "filament_settings_id": [
                    "Bambu PLA Metal @BBL X1C",
                    "Bambu ASA @BBL X1E 0.4 nozzle",
                    "Generic PETG",
                    "Bambu PLA Matte @BBL X1C",
                ],
                "filament_colour": ["#AA6443", "#FFFFFF", "#898989", "#E8DBB7"],
                "inherits_group": ["", "", "", "", "", ""],
                "different_settings_to_system": [
                    "enable_support;sparse_infill_density;sparse_infill_pattern;"
                    "support_critical_regions_only;support_type",
                    "", "", "", "", "",
                ],
                "flush_volumes_matrix": ["0"] * 16,
                "flush_volumes_vector": ["140"] * 8,
            })

            out = _sanitize_3mf(str(src), tmp, None, target_filament_count=1)

            self.assertNotEqual(out, str(src))
            s = _read_settings(out)
            self.assertEqual(len(s["filament_settings_id"]), 1)
            # Both structural arrays now match `len(filament_settings_id) + 2`.
            self.assertEqual(len(s["inherits_group"]), 3)
            self.assertEqual(len(s["different_settings_to_system"]), 3)
            # Process slot at [0] preserved, printer slot at [-1] preserved.
            self.assertEqual(
                s["different_settings_to_system"][0],
                "enable_support;sparse_infill_density;sparse_infill_pattern;"
                "support_critical_regions_only;support_type",
            )


if __name__ == "__main__":
    unittest.main()
