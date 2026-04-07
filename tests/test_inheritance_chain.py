import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


class InheritanceChainTests(unittest.TestCase):
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

    def test_returns_dict_with_resolved_and_inheritance_chain(self) -> None:
        detail = profiles.get_profile_detail("filament", "GFSNLS03_07")
        self.assertIn("resolved", detail)
        self.assertIn("inheritance_chain", detail)

    def test_chain_levels_have_name_vendor_own_fields(self) -> None:
        detail = profiles.get_profile_detail("filament", "GFSNLS03_07")
        chain = detail["inheritance_chain"]
        for level in chain:
            self.assertIn("name", level)
            self.assertIn("vendor", level)
            self.assertIn("own_fields", level)

    def test_first_chain_level_is_the_profile_itself(self) -> None:
        detail = profiles.get_profile_detail("filament", "GFSNLS03_07")
        chain = detail["inheritance_chain"]
        self.assertGreater(len(chain), 0)
        self.assertEqual(chain[0]["name"], "SUNLU PLA+ @BBL A1M")

    def test_detail_includes_top_level_metadata(self) -> None:
        detail = profiles.get_profile_detail("filament", "GFSNLS03_07")
        self.assertEqual(detail["setting_id"], "GFSNLS03_07")
        self.assertEqual(detail["name"], "SUNLU PLA+ @BBL A1M")
        self.assertEqual(detail["vendor"], "BBL")

    def test_nonexistent_profile_raises_profile_not_found(self) -> None:
        with self.assertRaises(profiles.ProfileNotFoundError):
            profiles.get_profile_detail("filament", "NONEXISTENT")

    def test_chain_walks_full_inheritance(self) -> None:
        detail = profiles.get_profile_detail("filament", "GFSNLS03_07")
        chain = detail["inheritance_chain"]
        chain_names = [level["name"] for level in chain]
        # Leaf -> base -> root
        self.assertEqual(chain_names[0], "SUNLU PLA+ @BBL A1M")
        self.assertEqual(chain_names[1], "SUNLU PLA+ @base")
        self.assertEqual(chain_names[2], "fdm_filament_pla")

    def test_own_fields_excludes_inherits_and_instantiation(self) -> None:
        detail = profiles.get_profile_detail("filament", "GFSNLS03_07")
        chain = detail["inheritance_chain"]
        for level in chain:
            self.assertNotIn("inherits", level["own_fields"])
            self.assertNotIn("instantiation", level["own_fields"])

    def test_resolved_profile_is_cleaned(self) -> None:
        detail = profiles.get_profile_detail("filament", "GFSNLS03_07")
        resolved = detail["resolved"]
        self.assertNotIn("inherits", resolved)
        self.assertNotIn("instantiation", resolved)
        # Should have inherited filament_type from base
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
                "filament_list": [],
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
            self.profiles_dir / "BBL" / "filament" / "fdm_filament_pla.json",
            {
                "name": "fdm_filament_pla",
                "instantiation": "false",
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

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
