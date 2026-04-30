import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, profiles


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "import_examples"


class _ProfileEndpointTestBase(unittest.TestCase):
    """Shared setup: isolated profile dirs with a minimal process + filament parent."""

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

        # main.USER_PROFILES_DIR is imported by value at module load — override it too.
        self._old_main_user_profiles_dir = main.USER_PROFILES_DIR
        main.USER_PROFILES_DIR = str(self.user_dir)

        self._write_fixture()
        profiles.load_all_profiles()

        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        main.USER_PROFILES_DIR = self._old_main_user_profiles_dir
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()
        shutil.rmtree(self.tempdir)

    def _write_fixture(self) -> None:
        self._write_json(
            self.profiles_dir / "BBL.json",
            {
                "filament_list": [
                    {"name": "Bambu PLA Basic @BBL A1M", "sub_path": "filament/Bambu PLA Basic @BBL A1M.json"}
                ],
                "process_list": [
                    {"name": "0.20mm Standard @BBL A1M", "sub_path": "process/0.20mm Standard @BBL A1M.json"}
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


class ProcessResolveImportEndpointTests(_ProfileEndpointTestBase):
    def test_resolve_import_returns_preview_with_resolved_profile(self) -> None:
        body = json.loads((FIXTURE_DIR / "process_esun_pla_basic_a1m.json").read_text())
        resp = self.client.post("/profiles/processes/resolve-import", json=body)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["name"], "eSUN PLA-Basic @BBL A1M Process")
        self.assertEqual(data["setting_id"], "eSUN PLA-Basic @BBL A1M Process")
        self.assertEqual(data["inherits_resolved"], "0.20mm Standard @BBL A1M")
        payload = data["resolved_profile"]
        self.assertEqual(payload["from"], "User")
        self.assertEqual(payload["outer_wall_speed"], ["150"])
        self.assertEqual(payload["layer_height"], ["0.2"])

    def test_resolve_import_missing_name_returns_400(self) -> None:
        resp = self.client.post(
            "/profiles/processes/resolve-import",
            json={"inherits": "0.20mm Standard @BBL A1M"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json()["error"].lower())

    def test_resolve_import_unknown_parent_returns_400(self) -> None:
        resp = self.client.post(
            "/profiles/processes/resolve-import",
            json={"name": "X", "inherits": "No Such Parent"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No Such Parent", resp.json()["error"])

    def test_resolve_import_non_object_body_returns_400(self) -> None:
        resp = self.client.post(
            "/profiles/processes/resolve-import",
            json=["list", "not", "object"],
        )
        self.assertEqual(resp.status_code, 400)


class ProcessSaveEndpointTests(_ProfileEndpointTestBase):
    def test_save_writes_file_and_lists_under_user_filter(self) -> None:
        body = json.loads((FIXTURE_DIR / "process_esun_pla_basic_a1m.json").read_text())
        preview = self.client.post(
            "/profiles/processes/resolve-import", json=body
        ).json()

        resp = self.client.post(
            "/profiles/processes",
            json=body,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["setting_id"], "eSUN PLA-Basic @BBL A1M Process")

        setting_id = preview["setting_id"]
        written_path = self.user_dir / f"{setting_id}.json"
        self.assertTrue(written_path.is_file())
        on_disk = json.loads(written_path.read_text())
        # The fixture supplies these and the new materializer preserves them.
        self.assertEqual(on_disk["from"], "User")
        self.assertEqual(on_disk["print_settings_id"], preview["name"])
        # The new contract: inherits is preserved on disk.
        self.assertEqual(on_disk["inherits"], "0.20mm Standard @BBL A1M")

        listing = self.client.get("/profiles/processes").json()
        names = {p["name"] for p in listing}
        self.assertIn(preview["name"], names)

    def test_save_returns_400_on_missing_name(self) -> None:
        resp = self.client.post(
            "/profiles/processes",
            json={"inherits": "0.20mm Standard @BBL A1M"},
        )
        self.assertEqual(resp.status_code, 400)


class CollisionSemanticsTests(_ProfileEndpointTestBase):
    def _import_process_once(self) -> tuple[dict, dict]:
        body = json.loads((FIXTURE_DIR / "process_esun_pla_basic_a1m.json").read_text())
        preview = self.client.post(
            "/profiles/processes/resolve-import", json=body
        ).json()
        resp = self.client.post("/profiles/processes", json=body)
        self.assertEqual(resp.status_code, 201)
        return preview, body

    def test_process_second_import_without_replace_returns_409(self) -> None:
        _preview, body = self._import_process_once()
        resp = self.client.post("/profiles/processes", json=body)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already exists", resp.json()["error"].lower())

    def test_process_second_import_with_replace_true_returns_200(self) -> None:
        preview, body = self._import_process_once()
        modified = dict(body)
        modified["outer_wall_speed"] = ["123"]
        resp = self.client.post(
            "/profiles/processes?replace=true", json=modified
        )
        self.assertEqual(resp.status_code, 200)
        on_disk = json.loads(
            (self.user_dir / f"{preview['setting_id']}.json").read_text()
        )
        self.assertEqual(on_disk["outer_wall_speed"], ["123"])

    def _import_filament_once(self) -> tuple[dict, dict]:
        body = json.loads((FIXTURE_DIR / "filament_esun_pla_basic_a1m.json").read_text())
        preview = self.client.post(
            "/profiles/filaments/resolve-import", json=body
        ).json()
        resp = self.client.post("/profiles/filaments", json=body)
        self.assertEqual(resp.status_code, 201)
        return preview, body

    def test_filament_second_import_without_replace_returns_409(self) -> None:
        _preview, body = self._import_filament_once()
        resp = self.client.post("/profiles/filaments", json=body)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already exists", resp.json()["error"].lower())

    def test_filament_second_import_with_replace_true_returns_200(self) -> None:
        preview, body = self._import_filament_once()
        modified = dict(body)
        modified["nozzle_temperature"] = ["245"]
        resp = self.client.post(
            "/profiles/filaments?replace=true", json=modified
        )
        self.assertEqual(resp.status_code, 200)
        on_disk = json.loads(
            (self.user_dir / f"{preview['setting_id']}.json").read_text()
        )
        self.assertEqual(on_disk["nozzle_temperature"], ["245"])


class ProcessDeleteEndpointTests(_ProfileEndpointTestBase):
    def test_delete_user_process_removes_file_and_unlists(self) -> None:
        body = json.loads((FIXTURE_DIR / "process_esun_pla_basic_a1m.json").read_text())
        preview = self.client.post(
            "/profiles/processes/resolve-import", json=body
        ).json()
        self.client.post("/profiles/processes", json=body)
        setting_id = preview["setting_id"]
        self.assertTrue((self.user_dir / f"{setting_id}.json").is_file())

        resp = self.client.delete(f"/profiles/processes/{setting_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse((self.user_dir / f"{setting_id}.json").is_file())

        listing = self.client.get("/profiles/processes").json()
        names = {p["name"] for p in listing}
        self.assertNotIn(preview["name"], names)

    def test_delete_vendor_process_returns_404(self) -> None:
        # GP004 is the vendor-side setting_id from the shared fixture
        resp = self.client.delete("/profiles/processes/GP004")
        self.assertEqual(resp.status_code, 404)
        # File on disk at vendor path must not be deleted
        self.assertTrue(
            (self.profiles_dir / "BBL" / "process" / "0.20mm Standard @BBL A1M.json").is_file()
        )

    def test_delete_unknown_returns_404(self) -> None:
        resp = self.client.delete("/profiles/processes/does-not-exist")
        self.assertEqual(resp.status_code, 404)


class UnsafeSettingIdTests(_ProfileEndpointTestBase):
    def test_save_process_rejects_path_traversal_setting_id(self) -> None:
        payload = {
            "name": "Malicious",
            "setting_id": "../../etc/passwd",
            "inherits": "0.20mm Standard @BBL A1M",
        }
        resp = self.client.post("/profiles/processes", json=payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unsafe", resp.json()["error"].lower())
        # No file should have been written outside the user dir
        self.assertFalse((self.tempdir_path() / "etc" / "passwd.json").exists())

    def test_save_process_rejects_slash_in_setting_id(self) -> None:
        payload = {
            "name": "A",
            "setting_id": "nested/dir",
            "inherits": "0.20mm Standard @BBL A1M",
        }
        resp = self.client.post("/profiles/processes", json=payload)
        self.assertEqual(resp.status_code, 400)

    def test_delete_process_rejects_parent_ref_segment(self) -> None:
        # Framework blocks URL-encoded slashes at routing; the guard catches
        # single-segment path-traversal vectors that *do* reach the handler.
        resp = self.client.delete("/profiles/processes/..escape")
        self.assertEqual(resp.status_code, 400)

    def test_save_filament_rejects_path_traversal_setting_id(self) -> None:
        payload = {
            "name": "Malicious",
            "setting_id": "../escape",
            "inherits": "Bambu PLA Basic @BBL A1M",
        }
        resp = self.client.post("/profiles/filaments", json=payload)
        self.assertEqual(resp.status_code, 400)

    def tempdir_path(self) -> Path:
        return Path(self.tempdir)


if __name__ == "__main__":
    unittest.main()
