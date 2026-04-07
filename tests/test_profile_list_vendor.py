"""Tests verifying the vendor field is present on profile list results."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


class ProfileListVendorTests(unittest.TestCase):
    """Verify that get_machine_profiles, get_process_profiles, and
    get_filament_profiles include a ``vendor`` field."""

    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-test-vendor-")
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

    # ------------------------------------------------------------------
    # Machine profiles
    # ------------------------------------------------------------------

    def test_machine_profiles_include_vendor(self) -> None:
        results = profiles.get_machine_profiles()
        self.assertTrue(len(results) > 0, "Expected at least one machine profile")
        for item in results:
            self.assertIn("vendor", item)

    def test_machine_profile_vendor_value(self) -> None:
        results = profiles.get_machine_profiles()
        by_id = {item["setting_id"]: item for item in results}
        self.assertIn("GM020", by_id)
        self.assertEqual(by_id["GM020"]["vendor"], "BBL")

    # ------------------------------------------------------------------
    # Process profiles
    # ------------------------------------------------------------------

    def test_process_profiles_include_vendor(self) -> None:
        results = profiles.get_process_profiles()
        self.assertTrue(len(results) > 0, "Expected at least one process profile")
        for item in results:
            self.assertIn("vendor", item)

    def test_process_profile_vendor_value(self) -> None:
        results = profiles.get_process_profiles()
        by_id = {item["setting_id"]: item for item in results}
        self.assertIn("GP004", by_id)
        self.assertEqual(by_id["GP004"]["vendor"], "BBL")

    # ------------------------------------------------------------------
    # Filament profiles
    # ------------------------------------------------------------------

    def test_filament_profiles_include_vendor(self) -> None:
        results = profiles.get_filament_profiles()
        self.assertTrue(len(results) > 0, "Expected at least one filament profile")
        for item in results:
            self.assertIn("vendor", item)

    def test_filament_profile_vendor_value(self) -> None:
        results = profiles.get_filament_profiles()
        by_id = {item["setting_id"]: item for item in results}
        self.assertIn("GFPLA01_07", by_id)
        self.assertEqual(by_id["GFPLA01_07"]["vendor"], "BBL")

    # ------------------------------------------------------------------
    # Fixture
    # ------------------------------------------------------------------

    def _write_fixture(self) -> None:
        # BBL vendor JSON (top-level index)
        self._write_json(
            self.profiles_dir / "BBL.json",
            {
                "machine_list": [
                    {"name": "Bambu Lab A1 mini 0.4 nozzle", "sub_path": "machine/Bambu Lab A1 mini 0.4 nozzle.json"},
                ],
                "process_list": [
                    {"name": "0.20mm Standard @BBL A1M", "sub_path": "process/0.20mm Standard @BBL A1M.json"},
                ],
                "filament_list": [
                    {"name": "Bambu PLA Basic @BBL A1M", "sub_path": "filament/Bambu PLA Basic @BBL A1M.json"},
                ],
            },
        )

        # Machine
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

        # Process base (non-instantiated)
        self._write_json(
            self.profiles_dir / "BBL" / "process" / "fdm_process_common.json",
            {
                "name": "fdm_process_common",
                "instantiation": "false",
                "layer_height": "0.2",
            },
        )

        # Process leaf
        self._write_json(
            self.profiles_dir / "BBL" / "process" / "0.20mm Standard @BBL A1M.json",
            {
                "name": "0.20mm Standard @BBL A1M",
                "setting_id": "GP004",
                "inherits": "fdm_process_common",
                "instantiation": "true",
                "from": "system",
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
                "layer_height": "0.2",
            },
        )

        # Filament base (non-instantiated)
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "fdm_filament_pla.json",
            {
                "name": "fdm_filament_pla",
                "instantiation": "false",
                "filament_type": ["PLA"],
            },
        )

        # Filament leaf
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "Bambu PLA Basic @BBL A1M.json",
            {
                "name": "Bambu PLA Basic @BBL A1M",
                "setting_id": "GFPLA01_07",
                "inherits": "fdm_filament_pla",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
        )

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
