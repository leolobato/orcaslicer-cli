import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


class FilamentVendorResolutionTests(unittest.TestCase):
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

    def test_machine_scoped_bbl_leaf_uses_bbl_filament_id(self) -> None:
        results = profiles.get_filament_profiles(machine_id="GM020", ams_assignable_only=True)
        by_setting_id = {item["setting_id"]: item for item in results}

        self.assertIn("GFSNLS03_07", by_setting_id)
        self.assertEqual(by_setting_id["GFSNLS03_07"]["filament_id"], "GFSNL03")

    def test_library_leaf_keeps_library_filament_id(self) -> None:
        resolved = profiles.get_profile("filament", "OSNLS03")
        self.assertEqual(resolved["filament_id"], "OGFSNL03")

    def test_bbl_leaf_detail_resolves_through_bbl_base(self) -> None:
        resolved = profiles.get_profile("filament", "GFSNLS03_07")
        self.assertEqual(resolved["filament_id"], "GFSNL03")
        self.assertEqual(resolved["filament_type"], ["PLA"])

    def _write_fixture(self) -> None:
        self._write_json(
            self.profiles_dir / "BBL.json",
            {
                "filament_list": [
                    {"name": "SUNLU PLA+ @BBL A1M", "sub_path": "filament/SUNLU PLA+ @BBL A1M.json"},
                ],
                "process_list": [],
            },
        )
        self._write_json(
            self.profiles_dir / "OrcaFilamentLibrary.json",
            {
                "filament_list": [
                    {"name": "SUNLU PLA+ @System", "sub_path": "filament/SUNLU PLA+ @System.json"},
                ],
                "process_list": [],
            },
        )

        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab A1 mini 0.4 nozzle.json",
            {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "setting_id": "GM020",
                "instantiation": "true",
                "printer_model": "Bambu Lab A1 mini",
                "machine_start_gcode": ["; test"],
                "nozzle_diameter": ["0.4"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "SUNLU PLA+ @BBL A1M.json",
            {
                "name": "SUNLU PLA+ @BBL A1M",
                "setting_id": "GFSNLS03_07",
                "inherits": "SUNLU PLA+ @base",
                "instantiation": "true",
                "from": "system",
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "SUNLU PLA+ @base.json",
            {
                "name": "SUNLU PLA+ @base",
                "inherits": "fdm_filament_pla",
                "from": "system",
                "instantiation": "false",
                "filament_id": "GFSNL03",
                "filament_type": ["PLA"],
            },
        )

        self._write_json(
            self.profiles_dir / "OrcaFilamentLibrary" / "filament" / "SUNLU PLA+ @System.json",
            {
                "name": "SUNLU PLA+ @System",
                "setting_id": "OSNLS03",
                "inherits": "SUNLU PLA+ @base",
                "instantiation": "true",
                "from": "system",
                "compatible_printers": [],
            },
        )
        self._write_json(
            self.profiles_dir / "OrcaFilamentLibrary" / "filament" / "SUNLU PLA+ @base.json",
            {
                "name": "SUNLU PLA+ @base",
                "inherits": "fdm_filament_pla",
                "from": "system",
                "instantiation": "false",
                "filament_id": "OGFSNL03",
                "filament_type": ["PLA"],
            },
        )
        self._write_json(
            self.profiles_dir / "OrcaFilamentLibrary" / "filament" / "fdm_filament_pla.json",
            {
                "name": "fdm_filament_pla",
                "instantiation": "false",
                "filament_type": ["PLA"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "fdm_filament_pla.json",
            {
                "name": "fdm_filament_pla",
                "instantiation": "false",
                "filament_type": ["PLA"],
            },
        )

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
