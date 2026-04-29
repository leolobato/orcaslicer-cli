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

    def test_materialize_is_deterministic_across_two_calls(self) -> None:
        payload = {
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
        }
        first = profiles.materialize_filament_import(dict(payload))
        second = profiles.materialize_filament_import(dict(payload))
        self.assertEqual(first, second)

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
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
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


class MaterializeFilamentImportRawFormTests(FilamentVendorResolutionTests):
    """The new raw-form behavior of materialize_filament_import.

    Subclasses FilamentVendorResolutionTests so it inherits the same
    fixture (SUNLU PLA+ profiles, A1M machine, fdm_filament_pla base).
    The inherited tests run a second time as part of this class — that's
    harmless duplication but documents that the existing assertions
    still hold.
    """

    def test_preserves_inherits_and_does_not_merge_parent(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "compatible_printers": ["My Custom Printer"],
        })

        self.assertEqual(result["name"], "My SUNLU copy")
        self.assertEqual(result["inherits"], "SUNLU PLA+ @BBL A1M")
        # Parent values must NOT be merged into the result.
        self.assertNotIn("filament_type", result)

    def test_synthesizes_setting_id_from_name_when_missing(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "compatible_printers": ["My Custom Printer"],
        })

        self.assertEqual(result["setting_id"], "My SUNLU copy")

    def test_keeps_directly_supplied_setting_id(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "setting_id": "MYSUNLU",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "compatible_printers": ["My Custom Printer"],
        })

        self.assertEqual(result["setting_id"], "MYSUNLU")

    def test_stamps_instantiation_true_when_missing(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "compatible_printers": ["My Custom Printer"],
        })

        self.assertEqual(result["instantiation"], "true")

    def test_keeps_directly_supplied_filament_id_when_disjoint(self) -> None:
        # The parent SUNLU PLA+ @BBL A1M is compatible with A1 mini.
        # We claim a disjoint printer set so the uniqueness check passes.
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "filament_id": "PCUSTOM1",
            "compatible_printers": ["My Custom Printer"],
        })

        self.assertEqual(result["filament_id"], "PCUSTOM1")

    def test_generates_filament_id_when_caller_does_not_supply_one(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "compatible_printers": ["My Custom Printer"],
        })

        fid = result["filament_id"]
        self.assertTrue(fid.startswith("P"))
        self.assertEqual(len(fid), 8)

    def test_raises_when_inherits_parent_is_unknown(self) -> None:
        with self.assertRaises(profiles.ProfileNotFoundError):
            profiles.materialize_filament_import({
                "name": "My orphan",
                "inherits": "Does Not Exist",
            })

    def test_rejects_directly_supplied_filament_id_on_overlapping_printers(self) -> None:
        # GFSNL03 is the parent's filament_id (SUNLU PLA+ @base), and
        # the parent's compatible_printers chain ends at @BBL A1M which
        # targets "Bambu Lab A1 mini 0.4 nozzle". Pasting GFSNL03 into a
        # new import that ALSO targets A1 mini collides on AMS scope.
        with self.assertRaises(ValueError) as ctx:
            profiles.materialize_filament_import({
                "name": "Pretender",
                "inherits": "SUNLU PLA+ @BBL A1M",
                "filament_id": "GFSNL03",
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            })

        msg = str(ctx.exception)
        self.assertIn("GFSNL03", msg)

    def test_allows_directly_supplied_filament_id_on_disjoint_printers(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "Disjoint Sibling",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "filament_id": "GFSNL03",
            "compatible_printers": ["My Custom Printer"],
        })
        self.assertEqual(result["filament_id"], "GFSNL03")

    def test_does_not_stamp_filament_settings_id(self) -> None:
        # The old materializer always stamped filament_settings_id=[name].
        # The new materializer leaves it alone — if the GUI export had
        # one, it survives; if it didn't, the import doesn't grow one.
        result = profiles.materialize_filament_import({
            "name": "No Stamp",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "compatible_printers": ["My Custom Printer"],
        })
        self.assertNotIn("filament_settings_id", result)

    def test_does_not_stamp_from_field(self) -> None:
        # The old materializer stamped from=User. The new one preserves
        # whatever the input had (typically OrcaSlicer GUI exports
        # already include from=User).
        result = profiles.materialize_filament_import({
            "name": "No From Stamp",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "compatible_printers": ["My Custom Printer"],
        })
        self.assertNotIn("from", result)

    def test_preserves_caller_supplied_from_field(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "Has From",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "from": "User",
            "compatible_printers": ["My Custom Printer"],
        })
        self.assertEqual(result["from"], "User")


if __name__ == "__main__":
    unittest.main()
