import json
import os
import tempfile
import unittest
import zipfile

from app.slicer import (
    _extract_declared_customizations,
    _extract_declared_filament_customizations,
    _extract_declared_machine_customizations,
    _overlay_3mf_filament_settings,
    _overlay_3mf_machine_settings,
    _overlay_3mf_settings,
    _patch_output_settings,
)


class OverlaySettingsTests(unittest.TestCase):
    def test_overlay_skips_filament_assignment_keys(self) -> None:
        process_profile = {
            "name": "Target",
            "setting_id": "GP004",
            "wall_filament": 1,
            "filament_settings_id": ["PLA Basic @BBL A1M"],
            "sparse_infill_pattern": "grid",
        }
        threemf_settings = {
            "wall_filament": 255,
            "filament_settings_id": ["External spool"],
            "sparse_infill_pattern": "gyroid",
        }
        allowed = {"wall_filament", "filament_settings_id", "sparse_infill_pattern"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["wall_filament"], 1)
        self.assertEqual(updated["filament_settings_id"], ["PLA Basic @BBL A1M"])
        self.assertEqual(updated["sparse_infill_pattern"], "gyroid")
        self.assertIn("sparse_infill_pattern", overlaid_keys)
        self.assertNotIn("wall_filament", overlaid_keys)
        self.assertNotIn("filament_settings_id", overlaid_keys)

    def test_overlay_applies_allowlisted_keys(self) -> None:
        process_profile = {
            "name": "Target",
            "setting_id": "GP004",
            "support_threshold_angle": "30",
            "enable_support": "0",
            "sparse_infill_density": "15%",
            "bottom_surface_pattern": "monotonic",
        }
        threemf_settings = {
            "support_threshold_angle": "25",
            "enable_support": "1",
            "sparse_infill_density": "15%",
            "bottom_surface_pattern": "monotonicline",
        }
        allowed = {"support_threshold_angle", "enable_support", "sparse_infill_density"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["support_threshold_angle"], "25")
        self.assertEqual(updated["enable_support"], "1")
        self.assertEqual(updated["sparse_infill_density"], "15%")
        # Not in the allowlist — must not be overlaid even though the 3MF has a different value.
        self.assertEqual(updated["bottom_surface_pattern"], "monotonic")
        self.assertEqual(overlaid_keys, allowed)

    def test_overlay_skips_profile_metadata_keys(self) -> None:
        process_profile = {
            "name": "Target",
            "from": "system",
            "inherits": "base",
            "sparse_infill_pattern": "grid",
        }
        threemf_settings = {
            "name": "Overwritten",
            "from": "other",
            "inherits": "other_base",
            "sparse_infill_pattern": "gyroid",
        }
        allowed = {"name", "from", "inherits", "sparse_infill_pattern"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["name"], "Target")
        self.assertEqual(updated["from"], "system")
        self.assertEqual(updated["inherits"], "base")
        self.assertEqual(updated["sparse_infill_pattern"], "gyroid")
        self.assertEqual(overlaid_keys, {"sparse_infill_pattern"})

    def test_overlay_injects_key_absent_from_process_profile(self) -> None:
        # `brim_type` is not written into any BBL process profile; OrcaSlicer
        # falls back to a compile-time default. When a 3MF declares it as a
        # customization, the overlay must inject it onto the target profile
        # so the user's choice survives. (Regression: previously a `k not in
        # process_profile` guard skipped this case.)
        process_profile = {
            "name": "0.20mm Standard @BBL A1M",
            "setting_id": "GP004",
            "sparse_infill_density": "15%",
        }
        threemf_settings = {
            "brim_type": "no_brim",
            "sparse_infill_density": "15%",
        }
        allowed = {"brim_type"}

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, allowed)

        self.assertEqual(updated["brim_type"], "no_brim")
        self.assertEqual(overlaid_keys, {"brim_type"})

    def test_overlay_transfers_nothing_when_allowlist_empty(self) -> None:
        process_profile = {
            "sparse_infill_pattern": "grid",
            "support_threshold_angle": "30",
        }
        threemf_settings = {
            "sparse_infill_pattern": "gyroid",
            "support_threshold_angle": "25",
        }

        updated, overlaid_keys = _overlay_3mf_settings(process_profile, threemf_settings, set())

        self.assertEqual(updated, process_profile)
        self.assertEqual(overlaid_keys, set())


class DeclaredCustomizationsTests(unittest.TestCase):
    def test_extracts_process_slot_only(self) -> None:
        threemf_settings = {
            "different_settings_to_system": [
                "sparse_infill_density;sparse_infill_pattern",
                "",
                "fan_min_speed;nozzle_temperature",
                "",
            ],
        }
        self.assertEqual(
            _extract_declared_customizations(threemf_settings),
            {"sparse_infill_density", "sparse_infill_pattern"},
        )

    def test_returns_empty_when_field_missing(self) -> None:
        self.assertEqual(_extract_declared_customizations({}), set())

    def test_returns_empty_when_process_slot_blank(self) -> None:
        threemf_settings = {"different_settings_to_system": ["", "", "", ""]}
        self.assertEqual(_extract_declared_customizations(threemf_settings), set())


class DeclaredFilamentCustomizationsTests(unittest.TestCase):
    """OrcaSlicer's ``different_settings_to_system`` layout is
    ``[process, filament_0, ..., filament_{N-1}, printer]`` — the trailing
    slot is always the printer (extracted separately)."""

    def test_extracts_per_filament_slots_two_filaments(self) -> None:
        threemf_settings = {
            "different_settings_to_system": [
                "sparse_infill_pattern",            # process
                "nozzle_temperature;fan_min_speed",  # filament 0
                "filament_max_volumetric_speed",     # filament 1
                "retraction_speed",                  # printer (skipped here)
            ],
        }
        self.assertEqual(
            _extract_declared_filament_customizations(threemf_settings),
            [
                {"nozzle_temperature", "fan_min_speed"},
                {"filament_max_volumetric_speed"},
            ],
        )

    def test_single_filament_layout(self) -> None:
        # ``[process, filament_0, printer]`` — exactly one filament slot.
        threemf_settings = {
            "different_settings_to_system": [
                "sparse_infill_pattern",
                "nozzle_temperature",
                "retraction_speed",
            ],
        }
        self.assertEqual(
            _extract_declared_filament_customizations(threemf_settings),
            [{"nozzle_temperature"}],
        )

    def test_returns_empty_when_no_filament_slots(self) -> None:
        # Length 2 = ``[process, printer]`` only.
        threemf_settings = {"different_settings_to_system": ["a", "b"]}
        self.assertEqual(_extract_declared_filament_customizations(threemf_settings), [])

    def test_returns_empty_when_field_missing(self) -> None:
        self.assertEqual(_extract_declared_filament_customizations({}), [])

    def test_blank_slot_yields_empty_set(self) -> None:
        threemf_settings = {
            "different_settings_to_system": [
                "",                       # process
                "",                       # filament 0
                "nozzle_temperature",     # filament 1
                "machine_max_jerk_x",     # printer
            ],
        }
        self.assertEqual(
            _extract_declared_filament_customizations(threemf_settings),
            [set(), {"nozzle_temperature"}],
        )


class DeclaredMachineCustomizationsTests(unittest.TestCase):
    def test_extracts_trailing_printer_slot(self) -> None:
        # The user-reported benchy 3MF: 1 filament, printer customizations
        # in the trailing slot.
        threemf_settings = {
            "different_settings_to_system": [
                "bridge_speed;sparse_infill_density",
                "",
                "deretraction_speed;machine_max_jerk_x;retraction_speed;z_hop",
            ],
        }
        self.assertEqual(
            _extract_declared_machine_customizations(threemf_settings),
            {"deretraction_speed", "machine_max_jerk_x", "retraction_speed", "z_hop"},
        )

    def test_extracts_trailing_slot_for_multi_filament(self) -> None:
        threemf_settings = {
            "different_settings_to_system": [
                "process",
                "filament_0",
                "filament_1",
                "machine_max_jerk_x;machine_max_jerk_y",
            ],
        }
        self.assertEqual(
            _extract_declared_machine_customizations(threemf_settings),
            {"machine_max_jerk_x", "machine_max_jerk_y"},
        )

    def test_returns_empty_when_field_missing(self) -> None:
        self.assertEqual(_extract_declared_machine_customizations({}), set())

    def test_returns_empty_when_too_short(self) -> None:
        # A single-element list has only a process slot, no printer slot.
        self.assertEqual(
            _extract_declared_machine_customizations(
                {"different_settings_to_system": ["process_only"]},
            ),
            set(),
        )

    def test_returns_empty_when_printer_slot_blank(self) -> None:
        threemf_settings = {"different_settings_to_system": ["a", "b", ""]}
        self.assertEqual(_extract_declared_machine_customizations(threemf_settings), set())


class OverlayMachineSettingsTests(unittest.TestCase):
    def test_applies_listed_keys_to_machine_profile(self) -> None:
        machine_profile = {
            "name": "Bambu Lab A1 mini 0.4 nozzle",
            "retraction_speed": ["30"],
            "z_hop": ["0.4"],
            "z_hop_types": ["Auto Lift"],
            "machine_max_jerk_x": ["9", "9"],
        }
        threemf_settings = {
            "retraction_speed": ["40"],
            "z_hop": ["0"],
            "z_hop_types": ["Slope Lift"],
            "machine_max_jerk_x": ["20", "9"],
            # Not in allowed_keys; must not be applied.
            "nozzle_temperature": ["220"],
        }
        allowed = {"retraction_speed", "z_hop", "z_hop_types", "machine_max_jerk_x"}

        updated, entries = _overlay_3mf_machine_settings(
            machine_profile, threemf_settings, allowed,
        )

        self.assertEqual(updated["retraction_speed"], ["40"])
        self.assertEqual(updated["z_hop"], ["0"])
        self.assertEqual(updated["z_hop_types"], ["Slope Lift"])
        self.assertEqual(updated["machine_max_jerk_x"], ["20", "9"])
        self.assertNotIn("nozzle_temperature", updated)
        self.assertEqual(
            {e["key"] for e in entries},
            {"retraction_speed", "z_hop", "z_hop_types", "machine_max_jerk_x"},
        )

    def test_skips_keys_already_matching(self) -> None:
        machine_profile = {"retraction_speed": ["40"]}
        threemf_settings = {"retraction_speed": ["40"]}
        updated, entries = _overlay_3mf_machine_settings(
            machine_profile, threemf_settings, {"retraction_speed"},
        )
        self.assertEqual(updated, machine_profile)
        self.assertEqual(entries, [])

    def test_skips_keys_absent_from_threemf_settings(self) -> None:
        machine_profile = {"retraction_speed": ["30"]}
        # The allowlist references a key the 3MF doesn't actually carry —
        # we can't transfer what isn't there.
        updated, entries = _overlay_3mf_machine_settings(
            machine_profile, {}, {"retraction_speed"},
        )
        self.assertEqual(updated, machine_profile)
        self.assertEqual(entries, [])


class OverlayFilamentSettingsTests(unittest.TestCase):
    def test_extracts_slot_value_from_combined_vector(self) -> None:
        filament_profile = {
            "name": "Bambu PLA Basic @BBL A1M",
            "nozzle_temperature": ["220"],
            "fan_min_speed": ["60"],
        }
        threemf_settings = {
            "nozzle_temperature": ["220", "225"],
            "fan_min_speed": ["60", "80"],
        }
        allowed = {"nozzle_temperature", "fan_min_speed"}

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=1, allowed_keys=allowed,
        )

        self.assertEqual(updated["nozzle_temperature"], ["225"])
        self.assertEqual(updated["fan_min_speed"], ["80"])
        by_key = {e["key"]: e for e in entries}
        self.assertEqual(by_key["nozzle_temperature"]["value"], "225")
        self.assertEqual(by_key["nozzle_temperature"]["original"], "220")
        self.assertEqual(by_key["fan_min_speed"]["value"], "80")

    def test_skips_key_when_slot_out_of_bounds(self) -> None:
        filament_profile = {"nozzle_temperature": ["220"]}
        threemf_settings = {"nozzle_temperature": ["220"]}  # only 1 slot in 3MF

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=1, allowed_keys={"nozzle_temperature"},
        )

        self.assertEqual(updated, filament_profile)
        self.assertEqual(entries, [])

    def test_skips_keys_not_in_allowlist(self) -> None:
        filament_profile = {"nozzle_temperature": ["220"], "fan_min_speed": ["60"]}
        threemf_settings = {"nozzle_temperature": ["225"], "fan_min_speed": ["80"]}

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=0, allowed_keys={"nozzle_temperature"},
        )

        self.assertEqual(updated["nozzle_temperature"], ["225"])
        self.assertEqual(updated["fan_min_speed"], ["60"])  # not in allowlist
        self.assertEqual([e["key"] for e in entries], ["nozzle_temperature"])

    def test_transfers_nothing_when_values_equal(self) -> None:
        filament_profile = {"nozzle_temperature": ["220"]}
        threemf_settings = {"nozzle_temperature": ["220"]}

        updated, entries = _overlay_3mf_filament_settings(
            filament_profile, threemf_settings, slot_idx=0, allowed_keys={"nozzle_temperature"},
        )

        self.assertEqual(updated, filament_profile)
        self.assertEqual(entries, [])


class PatchOutputSettingsTests(unittest.TestCase):
    """``_patch_output_settings`` records customizations into the output 3MF's
    ``different_settings_to_system`` so OrcaSlicer can re-open the file and
    show the user's overrides instead of silently reverting them to system
    defaults. The slot layout must match OrcaSlicer's loader:
    ``[process, filament_0, …, filament_{N-1}, printer]``."""

    def _make_output_3mf(self, settings: dict) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".3mf", delete=False)
        tmp.close()
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("[Content_Types].xml", "")
            zf.writestr(
                "Metadata/project_settings.config",
                json.dumps(settings),
            )
        return tmp.name

    def _read_diff(self, path: str) -> list:
        with zipfile.ZipFile(path) as zf:
            settings = json.loads(zf.read("Metadata/project_settings.config"))
        return settings.get("different_settings_to_system", [])

    def test_writes_machine_keys_to_trailing_slot_single_filament(self) -> None:
        path = self._make_output_3mf({
            "different_settings_to_system": ["existing_process", "", ""],
        })
        try:
            _patch_output_settings(
                path,
                customized_keys=set(),
                machine_customized_keys={"retraction_speed", "z_hop"},
                num_filaments=1,
            )
            diff = self._read_diff(path)
            # 1 filament → length 3, printer at index 2.
            self.assertEqual(len(diff), 3)
            self.assertEqual(diff[0], "existing_process")
            self.assertEqual(diff[1], "")
            self.assertEqual(
                set(diff[2].split(";")), {"retraction_speed", "z_hop"},
            )
        finally:
            os.unlink(path)

    def test_writes_filament_at_index_plus_one_two_filaments(self) -> None:
        path = self._make_output_3mf({
            "different_settings_to_system": ["", "", "", ""],
        })
        try:
            _patch_output_settings(
                path,
                customized_keys=set(),
                filament_customized_keys={
                    0: {"nozzle_temperature"},
                    1: {"fan_min_speed"},
                },
                machine_customized_keys={"retraction_speed"},
                num_filaments=2,
            )
            diff = self._read_diff(path)
            # 2 filaments → [process, filament_0, filament_1, printer], len 4.
            self.assertEqual(len(diff), 4)
            self.assertEqual(diff[1], "nozzle_temperature")
            self.assertEqual(diff[2], "fan_min_speed")
            self.assertEqual(diff[3], "retraction_speed")
        finally:
            os.unlink(path)

    def test_pads_short_diff_to_canonical_width(self) -> None:
        # An output written by some pipeline that left the field empty —
        # we must still pad to ``num_filaments + 2`` so machine_idx is correct.
        path = self._make_output_3mf({"different_settings_to_system": []})
        try:
            _patch_output_settings(
                path,
                customized_keys=set(),
                machine_customized_keys={"z_hop"},
                num_filaments=1,
            )
            diff = self._read_diff(path)
            self.assertEqual(len(diff), 3)
            self.assertEqual(diff[0], "")
            self.assertEqual(diff[1], "")
            self.assertEqual(diff[2], "z_hop")
        finally:
            os.unlink(path)

    def test_merges_with_existing_keys_in_each_slot(self) -> None:
        path = self._make_output_3mf({
            "different_settings_to_system": [
                "alpha;beta",
                "",
                "existing_machine",
            ],
        })
        try:
            _patch_output_settings(
                path,
                customized_keys={"gamma"},
                machine_customized_keys={"new_machine"},
                num_filaments=1,
            )
            diff = self._read_diff(path)
            self.assertEqual(set(diff[0].split(";")), {"alpha", "beta", "gamma"})
            self.assertEqual(
                set(diff[2].split(";")), {"existing_machine", "new_machine"},
            )
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
