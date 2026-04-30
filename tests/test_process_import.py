import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "import_examples"


class ProcessImportHappyPathTests(unittest.TestCase):
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

    def test_happy_path_resolves_parent_and_stamps_user_fields(self) -> None:
        raw = json.loads(
            (FIXTURE_DIR / "process_esun_pla_basic_a1m.json").read_text()
        )

        result = profiles.materialize_process_import(raw)

        # Identity + metadata
        self.assertEqual(result["name"], "eSUN PLA-Basic @BBL A1M Process")
        # `from` is preserved from the input (GUI exports include "from": "User"),
        # but the materializer no longer stamps it itself.
        self.assertEqual(result["from"], "User")
        self.assertEqual(result["instantiation"], "true")
        # setting_id defaults to name when caller doesn't supply one
        self.assertEqual(result["setting_id"], "eSUN PLA-Basic @BBL A1M Process")
        # print_settings_id is preserved from the input payload, not stamped.
        self.assertEqual(result["print_settings_id"], "eSUN PLA-Basic @BBL A1M Process")

        # Inheritance is preserved — chain is resolved at slice/listing time.
        self.assertEqual(result["inherits"], "0.20mm Standard @BBL A1M")

        # User overrides survive untouched.
        self.assertEqual(result["outer_wall_speed"], ["150"])
        self.assertEqual(result["inner_wall_speed"], ["250"])

        # Parent keys are NOT merged into the raw payload.
        self.assertNotIn("layer_height", result)

    def _write_fixture(self) -> None:
        self._write_json(
            self.profiles_dir / "BBL.json",
            {
                "filament_list": [],
                "process_list": [
                    {
                        "name": "0.20mm Standard @BBL A1M",
                        "sub_path": "process/0.20mm Standard @BBL A1M.json",
                    }
                ],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "process" / "0.20mm Standard @BBL A1M.json",
            {
                "name": "0.20mm Standard @BBL A1M",
                "setting_id": "GP004",
                "instantiation": "true",
                "from": "system",
                "layer_height": ["0.2"],
                "outer_wall_speed": ["200"],
                "inner_wall_speed": ["300"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab A1 mini 0.4 nozzle.json",
            {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "setting_id": "GM020",
                "instantiation": "true",
                "printer_model": "Bambu Lab A1 mini",
                "nozzle_diameter": ["0.4"],
            },
        )

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


class ProcessImportErrorPathTests(ProcessImportHappyPathTests):
    """Reuses the happy-path fixture; only the test methods differ."""

    def test_happy_path_resolves_parent_and_stamps_user_fields(self) -> None:  # type: ignore[override]
        # Override the parent class's happy-path test so this class only exercises error paths.
        self.skipTest("Covered by ProcessImportHappyPathTests")

    def test_missing_name_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            profiles.materialize_process_import({"inherits": "0.20mm Standard @BBL A1M"})

    def test_empty_name_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            profiles.materialize_process_import({"name": "   ", "inherits": "X"})

    def test_unknown_parent_raises_profile_not_found(self) -> None:
        payload = {
            "name": "My Process",
            "inherits": "No Such Parent @BBL A1M",
        }
        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles.materialize_process_import(payload)
        self.assertIn("No Such Parent @BBL A1M", str(ctx.exception))
        self.assertIn("Process parent", str(ctx.exception))

    def test_setting_id_defaults_to_name(self) -> None:
        payload = {
            "name": "My Process",
            "inherits": "0.20mm Standard @BBL A1M",
        }
        result = profiles.materialize_process_import(payload)
        self.assertEqual(result["setting_id"], "My Process")

    def test_user_supplied_setting_id_wins(self) -> None:
        payload = {
            "name": "My Process",
            "setting_id": "custom-id-42",
            "inherits": "0.20mm Standard @BBL A1M",
        }
        result = profiles.materialize_process_import(payload)
        self.assertEqual(result["setting_id"], "custom-id-42")

    def test_no_inherits_produces_root_profile(self) -> None:
        payload = {
            "name": "Standalone Process",
            "layer_height": ["0.3"],
        }
        result = profiles.materialize_process_import(payload)
        self.assertEqual(result["name"], "Standalone Process")
        self.assertEqual(result["layer_height"], ["0.3"])
        self.assertNotIn("inherits", result)


class MaterializeProcessImportRawFormTests(ProcessImportHappyPathTests):
    """The new raw-form behavior of `materialize_process_import`.

    Subclasses `ProcessImportHappyPathTests` to reuse the BBL fixture
    (`0.20mm Standard @BBL A1M` with `layer_height=0.2`,
    `outer_wall_speed=200`, `inner_wall_speed=300`). The inherited
    happy-path test runs again as part of this class — harmless.
    """

    def test_preserves_inherits_and_does_not_merge_parent(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
            "outer_wall_speed": ["150"],
        })

        self.assertEqual(result["inherits"], "0.20mm Standard @BBL A1M")
        self.assertEqual(result["outer_wall_speed"], ["150"])
        self.assertNotIn("inner_wall_speed", result)
        self.assertNotIn("layer_height", result)

    def test_synthesizes_setting_id_from_name_when_missing(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
        })

        self.assertEqual(result["setting_id"], "Custom Process")

    def test_keeps_directly_supplied_setting_id(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "setting_id": "MYPROC",
            "inherits": "0.20mm Standard @BBL A1M",
        })

        self.assertEqual(result["setting_id"], "MYPROC")

    def test_stamps_instantiation_true_when_missing(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
        })

        self.assertEqual(result["instantiation"], "true")

    def test_preserves_caller_supplied_instantiation_false(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Caller Says False",
            "inherits": "0.20mm Standard @BBL A1M",
            "instantiation": "false",
        })

        self.assertEqual(result["instantiation"], "false")

    def test_does_not_stamp_print_settings_id(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
        })
        self.assertNotIn("print_settings_id", result)

    def test_preserves_caller_supplied_print_settings_id(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
            "print_settings_id": "Some Other Name",
        })
        self.assertEqual(result["print_settings_id"], "Some Other Name")

    def test_does_not_stamp_from_field(self) -> None:
        result = profiles.materialize_process_import({
            "name": "No From Stamp",
            "inherits": "0.20mm Standard @BBL A1M",
        })
        self.assertNotIn("from", result)

    def test_preserves_caller_supplied_from_field(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Has From",
            "inherits": "0.20mm Standard @BBL A1M",
            "from": "User",
        })
        self.assertEqual(result["from"], "User")

    def test_bare_payload_without_inherits_succeeds(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Bare Bones Process",
            "outer_wall_speed": ["100"],
        })

        self.assertEqual(result["name"], "Bare Bones Process")
        self.assertEqual(result["setting_id"], "Bare Bones Process")
        self.assertEqual(result["instantiation"], "true")
        self.assertNotIn("inherits", result)


class ProcessImportTypeScopedLookupTests(unittest.TestCase):
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

        # Only a filament named "Ambiguous Name" exists; no process by that name.
        self._write_json(
            self.profiles_dir / "BBL.json",
            {
                "filament_list": [
                    {"name": "Ambiguous Name", "sub_path": "filament/Ambiguous Name.json"},
                ],
                "process_list": [],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "Ambiguous Name.json",
            {
                "name": "Ambiguous Name",
                "setting_id": "GFAMB01",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFAMB01",
                "filament_type": ["PLA"],
            },
        )
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

    def test_filament_name_does_not_satisfy_process_inherits(self) -> None:
        payload = {"name": "Imported Process", "inherits": "Ambiguous Name"}
        with self.assertRaises(profiles.ProfileNotFoundError):
            profiles.materialize_process_import(payload)

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
