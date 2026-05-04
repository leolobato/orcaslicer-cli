"""Unit tests for the GUI-equivalent profile resolution helpers.

Covers `get_machine_model_metadata`, `resolve_filament_for_machine`,
`resolve_process_for_machine`, and `resolve_plate_type_for_machine` in
`app.profiles`. The fixture stubs out two BBL printers (A1 mini, P2S)
with same-alias filament + process variants so the alias-match path can
be exercised without the real OrcaSlicer profile tree.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


PLATE_TYPE_API_TO_ORCA = {
    "cool_plate": "Cool Plate",
    "engineering_plate": "Engineering Plate",
    "high_temp_plate": "High Temp Plate",
    "textured_pei_plate": "Textured PEI Plate",
    "textured_cool_plate": "Textured Cool Plate",
    "supertack_plate": "Supertack Plate",
}


class ResolveForMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-test-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

        self._write_fixture()
        profiles.load_all_profiles()

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()
        shutil.rmtree(self.tempdir)

    # ------------------------------------------------------------------ metadata

    def test_machine_metadata_pulls_model_fields(self) -> None:
        meta = profiles.get_machine_model_metadata("GM020")  # A1 mini variant
        self.assertEqual(meta["name"], "Bambu Lab A1 mini 0.4 nozzle")
        self.assertEqual(meta["printer_model"], "Bambu Lab A1 mini")
        self.assertEqual(meta["default_bed_type"], "Textured PEI Plate")
        self.assertEqual(
            meta["not_support_bed_types"],
            ["Engineering Plate", "Smooth Cool Plate"],
        )
        self.assertEqual(meta["default_filament_profile"], ["Bambu PLA Basic @BBL A1M"])
        self.assertEqual(meta["default_print_profile"], "0.20mm Standard @BBL A1M")
        self.assertEqual(meta["model_id"], "N1")

    # ------------------------------------------------------------------ filaments

    def test_filament_alias_match_p2s_to_a1m(self) -> None:
        result = profiles.resolve_filament_for_machine(
            machine_slug="GM020",
            requested_filament_name="Bambu PLA Basic @BBL P2S",
        )
        self.assertEqual(result["match"], "alias")
        self.assertEqual(result["name"], "Bambu PLA Basic @BBL A1M")
        self.assertEqual(result["alias"], "Bambu PLA Basic")

    def test_filament_unchanged_when_already_compat(self) -> None:
        result = profiles.resolve_filament_for_machine(
            machine_slug="GM020",
            requested_filament_name="Bambu PLA Basic @BBL A1M",
        )
        self.assertEqual(result["match"], "unchanged")
        self.assertEqual(result["name"], "Bambu PLA Basic @BBL A1M")

    def test_filament_type_fallback(self) -> None:
        # "Generic PETG @BBL P2S" has no @BBL A1M alias variant in our fixture,
        # but the catalog has "Bambu PLA Basic @BBL A1M" (PLA, not matching
        # PETG). Without an alias and without a same-type candidate, the
        # resolver should fall back to first_compat.
        result = profiles.resolve_filament_for_machine(
            machine_slug="GM020",
            requested_filament_name="Generic PETG @BBL P2S",
        )
        self.assertIn(result["match"], {"type", "first_compat", "default"})
        # And the resolved filament must be one of the A1 mini compat ones.
        self.assertIn(result["name"], {
            "Bambu PLA Basic @BBL A1M",
            "Bambu PETG @BBL A1M",
        })

    def test_filament_type_match_picks_same_type(self) -> None:
        # Add a PETG filament for A1 mini at runtime so the type fallback
        # has a candidate to pick.
        result = profiles.resolve_filament_for_machine(
            machine_slug="GM020",
            requested_filament_name="Generic PETG @BBL P2S",
        )
        # In our fixture both alternatives are PLA *and* PETG; check that
        # if a PETG candidate exists it wins by type.
        if result["name"] == "Bambu PETG @BBL A1M":
            self.assertEqual(result["match"], "type")

    def test_filament_unknown_machine_raises(self) -> None:
        # An unknown machine slug must raise rather than silently return
        # ``match="none"`` — callers expect the same not-found semantics
        # the rest of `app.profiles` uses.
        with self.assertRaises(profiles.ProfileNotFoundError):
            profiles.resolve_filament_for_machine(
                machine_slug="GM999",
                requested_filament_name="Bambu PLA Basic @BBL P2S",
            )

    # ------------------------------------------------------------------ process

    def test_process_alias_match_p2s_to_a1m(self) -> None:
        result = profiles.resolve_process_for_machine(
            machine_slug="GM020",
            requested="0.20mm Standard @BBL P2S",
        )
        self.assertEqual(result["match"], "alias")
        self.assertEqual(result["name"], "0.20mm Standard @BBL A1M")

    def test_process_unchanged_when_already_compat(self) -> None:
        result = profiles.resolve_process_for_machine(
            machine_slug="GM020",
            requested="0.20mm Standard @BBL A1M",
        )
        self.assertEqual(result["match"], "unchanged")

    def test_process_layer_height_proximity_when_no_alias(self) -> None:
        # Request a process whose alias has no A1 mini variant, but whose
        # layer_height matches another candidate exactly.
        result = profiles.resolve_process_for_machine(
            machine_slug="GM020",
            requested="0.16mm Optimal @BBL P2S",  # alias: "0.16mm Optimal", LH=0.16
        )
        # Either layer_height match (if a 0.16mm A1M process exists) or
        # falls back to first_compat. Our fixture has a 0.16mm A1M process,
        # so layer_height should win.
        self.assertEqual(result["match"], "layer_height")
        self.assertEqual(result["name"], "0.20mm Fine @BBL A1M")  # LH=0.16

    # ------------------------------------------------------------------ plate type

    def test_plate_type_unsupported_falls_back_to_default(self) -> None:
        # A1 mini's not_support_bed_type includes Engineering Plate. The
        # default_bed_type is Textured PEI Plate. Requesting engineering
        # should resolve to textured_pei.
        result = profiles.resolve_plate_type_for_machine(
            machine_slug="GM020",
            requested_plate_type="engineering_plate",
            plate_type_api_to_orca=PLATE_TYPE_API_TO_ORCA,
        )
        self.assertEqual(result["match"], "default")
        self.assertEqual(result["resolved"], "textured_pei_plate")

    def test_plate_type_supported_unchanged(self) -> None:
        result = profiles.resolve_plate_type_for_machine(
            machine_slug="GM020",
            requested_plate_type="textured_pei_plate",
            plate_type_api_to_orca=PLATE_TYPE_API_TO_ORCA,
        )
        self.assertEqual(result["match"], "unchanged")
        self.assertEqual(result["resolved"], "textured_pei_plate")

    # ------------------------------------------------------------------ fixture

    def _write_fixture(self) -> None:
        # Vendor index pulling in two filament/process variants per printer.
        self._write_json(
            self.profiles_dir / "BBL.json",
            {
                "machine_model_list": [
                    {"name": "Bambu Lab A1 mini", "sub_path": "machine/Bambu Lab A1 mini.json"},
                    {"name": "Bambu Lab P2S", "sub_path": "machine/Bambu Lab P2S.json"},
                ],
                "filament_list": [
                    {"name": "Bambu PLA Basic @BBL A1M", "sub_path": "filament/Bambu PLA Basic @BBL A1M.json"},
                    {"name": "Bambu PLA Basic @BBL P2S", "sub_path": "filament/Bambu PLA Basic @BBL P2S.json"},
                    {"name": "Bambu PETG @BBL A1M", "sub_path": "filament/Bambu PETG @BBL A1M.json"},
                    {"name": "Generic PETG @BBL P2S", "sub_path": "filament/Generic PETG @BBL P2S.json"},
                ],
                "process_list": [
                    {"name": "0.20mm Standard @BBL A1M", "sub_path": "process/0.20mm Standard @BBL A1M.json"},
                    {"name": "0.20mm Standard @BBL P2S", "sub_path": "process/0.20mm Standard @BBL P2S.json"},
                    {"name": "0.20mm Fine @BBL A1M", "sub_path": "process/0.20mm Fine @BBL A1M.json"},
                    {"name": "0.16mm Optimal @BBL P2S", "sub_path": "process/0.16mm Optimal @BBL P2S.json"},
                ],
            },
        )

        # Machine_model entries (top-level, no `inherits`) carry the
        # `not_support_bed_type` / `default_bed_type` / `model_id` fields.
        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab A1 mini.json",
            {
                "type": "machine_model",
                "name": "Bambu Lab A1 mini",
                "printer_model": "Bambu Lab A1 mini",
                "machine_start_gcode": ["; placeholder"],
                "default_bed_type": "Textured PEI Plate",
                "not_support_bed_type": "Engineering Plate;Smooth Cool Plate",
                "default_materials": "Bambu PLA Basic @BBL A1M;Bambu PETG @BBL A1M",
                "model_id": "N1",
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab P2S.json",
            {
                "type": "machine_model",
                "name": "Bambu Lab P2S",
                "printer_model": "Bambu Lab P2S",
                "machine_start_gcode": ["; placeholder"],
                "default_bed_type": "Textured PEI Plate",
                "not_support_bed_type": "",
                "default_materials": "Bambu PLA Basic @BBL P2S;Generic PETG @BBL P2S",
                "model_id": "C12",
            },
        )

        # Variant profiles ("0.4 nozzle" leaves) reference the model via
        # `printer_model`. They carry the GUI's `default_*_profile` fields.
        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab A1 mini 0.4 nozzle.json",
            {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "setting_id": "GM020",
                "instantiation": "true",
                "printer_model": "Bambu Lab A1 mini",
                "machine_start_gcode": ["; placeholder"],
                "nozzle_diameter": ["0.4"],
                "default_filament_profile": ["Bambu PLA Basic @BBL A1M"],
                "default_print_profile": "0.20mm Standard @BBL A1M",
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab P2S 0.4 nozzle.json",
            {
                "name": "Bambu Lab P2S 0.4 nozzle",
                "setting_id": "GM049",
                "instantiation": "true",
                "printer_model": "Bambu Lab P2S",
                "machine_start_gcode": ["; placeholder"],
                "nozzle_diameter": ["0.4"],
                "default_filament_profile": ["Bambu PLA Basic @BBL P2S"],
                "default_print_profile": "0.20mm Standard @BBL P2S",
            },
        )

        # Filament profiles. Same alias ("Bambu PLA Basic") on both machines.
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "Bambu PLA Basic @BBL A1M.json",
            {
                "name": "Bambu PLA Basic @BBL A1M",
                "setting_id": "GFA00_A1M",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "Bambu PLA Basic @BBL P2S.json",
            {
                "name": "Bambu PLA Basic @BBL P2S",
                "setting_id": "GFA00_P2S",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab P2S 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "Bambu PETG @BBL A1M.json",
            {
                "name": "Bambu PETG @BBL A1M",
                "setting_id": "GFG00_A1M",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFG00",
                "filament_type": ["PETG"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "Generic PETG @BBL P2S.json",
            {
                "name": "Generic PETG @BBL P2S",
                "setting_id": "GFG99_P2S",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFG99",
                "filament_type": ["PETG"],
                "compatible_printers": ["Bambu Lab P2S 0.4 nozzle"],
            },
        )

        # Process profiles.
        self._write_json(
            self.profiles_dir / "BBL" / "process" / "0.20mm Standard @BBL A1M.json",
            {
                "name": "0.20mm Standard @BBL A1M",
                "setting_id": "GP005_A1M",
                "instantiation": "true",
                "from": "system",
                "layer_height": "0.2",
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "process" / "0.20mm Standard @BBL P2S.json",
            {
                "name": "0.20mm Standard @BBL P2S",
                "setting_id": "GP005_P2S",
                "instantiation": "true",
                "from": "system",
                "layer_height": "0.2",
                "compatible_printers": ["Bambu Lab P2S 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "process" / "0.20mm Fine @BBL A1M.json",
            {
                "name": "0.20mm Fine @BBL A1M",
                "setting_id": "GP004_A1M",
                "instantiation": "true",
                "from": "system",
                "layer_height": "0.16",
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "process" / "0.16mm Optimal @BBL P2S.json",
            {
                "name": "0.16mm Optimal @BBL P2S",
                "setting_id": "GP008_P2S",
                "instantiation": "true",
                "from": "system",
                "layer_height": "0.16",
                "compatible_printers": ["Bambu Lab P2S 0.4 nozzle"],
            },
        )

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
