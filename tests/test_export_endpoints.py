import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, profiles


class _ExportTestBase(unittest.TestCase):
    """Isolated tmpdir + indexed BBL parent + A1 mini machine + user filament."""

    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-export-test-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)
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
        # Vendor index + parent filament + two machines (one single-variant,
        # one two-variant) + a user filament that inherits from the vendor.
        self._write_json(self.profiles_dir / "BBL.json", {
            "filament_list": [
                {"name": "Bambu PLA Matte @BBL A1M",
                 "sub_path": "filament/Bambu PLA Matte @BBL A1M.json"},
            ],
        })
        self._write_json(
            self.profiles_dir / "BBL" / "filament" / "Bambu PLA Matte @BBL A1M.json",
            {
                "name": "Bambu PLA Matte @BBL A1M",
                "setting_id": "GFSA01_02",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFA01",
                "filament_type": ["PLA"],
                "compatible_printers": [
                    "Bambu Lab A1 mini 0.4 nozzle",
                    "Bambu Lab P1P 0.4 nozzle",
                ],
                "nozzle_temperature": ["220"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab A1 mini 0.4 nozzle.json",
            {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "setting_id": "GM_A1MINI04",
                "instantiation": "true",
                "from": "system",
                "printer_extruder_variant": ["Direct Drive Standard"],
            },
        )
        self._write_json(
            self.profiles_dir / "BBL" / "machine" / "Bambu Lab P1P 0.4 nozzle.json",
            {
                "name": "Bambu Lab P1P 0.4 nozzle",
                "setting_id": "GM_P1P04",
                "instantiation": "true",
                "from": "system",
                "printer_extruder_variant": [
                    "Direct Drive Standard", "Direct Drive High Flow",
                ],
            },
        )
        # User filament that inherits the vendor parent (and so picks up
        # both compatible printers).
        self._write_json(
            self.user_dir / "filament" / "Eryone Matte Imported.json",
            {
                "name": "Eryone Matte Imported",
                "setting_id": "Eryone Matte Imported",
                "instantiation": "true",
                "from": "User",
                "filament_id": "Pfd5d97d",
                "inherits": "Bambu PLA Matte @BBL A1M",
                "nozzle_temperature": ["210"],
                "filament_extruder_variant": ["Direct Drive Standard"],
                "version": "1.9.0.21",
            },
        )

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)


class GetExportEndpointTests(_ExportTestBase):
    def test_thin_returns_json(self):
        r = self.client.get(
            "/profiles/filaments/Eryone Matte Imported/export?shape=thin",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/json", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        body = r.json()
        self.assertEqual(body["inherits"], "Bambu PLA Matte @BBL A1M")
        self.assertEqual(body["filament_id"], "Pfd5d97d")

    def test_flattened_consolidates_compatible_printers_with_common_prefix(self):
        # A1 mini (variant_count=1) and P1P (variant_count=2) land in
        # different variant-count groups. Each group has exactly one printer,
        # so no consolidation occurs — still 2 files.
        r = self.client.get(
            "/profiles/filaments/Eryone Matte Imported/export?shape=flattened",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = sorted(zf.namelist())
        self.assertEqual(names, [
            "eryone_matte_imported_bambu_lab_a1_mini_0.4_nozzle.json",
            "eryone_matte_imported_bambu_lab_p1p_0.4_nozzle.json",
        ])

    def test_flattened_entry_shape(self):
        r = self.client.get(
            "/profiles/filaments/Eryone Matte Imported/export?shape=flattened",
        )
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        a1 = json.loads(zf.read(
            "eryone_matte_imported_bambu_lab_a1_mini_0.4_nozzle.json"
        ))
        self.assertEqual(a1["inherits"], "")
        self.assertEqual(a1["compatible_printers"], ["Bambu Lab A1 mini 0.4 nozzle"])
        self.assertNotIn("type", a1)
        self.assertNotIn("instantiation", a1)
        self.assertNotIn("setting_id", a1)
        self.assertEqual(a1["filament_id"], "Pfd5d97d")
        # A1 mini = single variant → no padding.
        self.assertEqual(a1["nozzle_temperature"], ["210"])

        p1p = json.loads(zf.read(
            "eryone_matte_imported_bambu_lab_p1p_0.4_nozzle.json"
        ))
        # P1P = two variants → padding.
        self.assertEqual(p1p["nozzle_temperature"], ["210", "210"])
        self.assertEqual(
            p1p["filament_extruder_variant"],
            ["Direct Drive Standard", "Direct Drive High Flow"],
        )

    def test_default_shape_is_flattened(self):
        r = self.client.get("/profiles/filaments/Eryone Matte Imported/export")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")

    def test_invalid_shape_returns_400(self):
        r = self.client.get(
            "/profiles/filaments/Eryone Matte Imported/export?shape=bogus",
        )
        self.assertEqual(r.status_code, 400)

    def test_unknown_setting_id_returns_404(self):
        r = self.client.get("/profiles/filaments/nonexistent/export")
        self.assertEqual(r.status_code, 404)

    def test_vendor_setting_id_returns_404(self):
        r = self.client.get("/profiles/filaments/GFSA01_02/export")
        self.assertEqual(r.status_code, 404)

    def test_unresolved_chain_returns_500(self):
        # Break the parent in-memory.
        user_key = profiles._profile_key("User", "Eryone Matte Imported")
        profiles._raw_profiles[user_key]["inherits"] = "Nonexistent Parent"
        profiles._resolved_cache.clear()
        r = self.client.get(
            "/profiles/filaments/Eryone Matte Imported/export?shape=flattened",
        )
        self.assertEqual(r.status_code, 500)


class PostBatchExportTests(_ExportTestBase):
    def _add_second_user_filament(self) -> None:
        self._write_json(
            self.user_dir / "filament" / "Other PLA.json",
            {
                "name": "Other PLA",
                "setting_id": "Other PLA",
                "instantiation": "true",
                "from": "User",
                "filament_id": "Pother01",
                "inherits": "Bambu PLA Matte @BBL A1M",
                "nozzle_temperature": ["205"],
                "version": "1.9.0.21",
            },
        )
        profiles.load_all_profiles()

    def test_thin_batch_returns_zip(self):
        self._add_second_user_filament()
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={
                "setting_ids": ["Eryone Matte Imported", "Other PLA"],
                "shape": "thin",
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(len(zf.namelist()), 2)
        self.assertNotIn("x-export-skipped", {h.lower() for h in r.headers.keys()})

    def test_flattened_batch_expands_per_printer_per_filament(self):
        self._add_second_user_filament()
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={
                "setting_ids": ["Eryone Matte Imported", "Other PLA"],
                "shape": "flattened",
            },
        )
        self.assertEqual(r.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        # 2 filaments × 2 variant-count groups (A1 mini vc=1, P1P vc=2) = 4 entries.
        # Each group has one printer so no consolidation within groups.
        self.assertEqual(len(zf.namelist()), 4)

    def test_default_shape_is_flattened(self):
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={"setting_ids": ["Eryone Matte Imported"]},
        )
        self.assertEqual(r.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(len(zf.namelist()), 2)  # 2 variant-count groups

    def test_partial_not_found_reported_in_header(self):
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={
                "setting_ids": [
                    "Eryone Matte Imported",
                    "nonexistent",
                    "GFSA01_02",  # vendor → not_found in user scope
                ],
                "shape": "thin",
            },
        )
        self.assertEqual(r.status_code, 200)
        skipped = json.loads(r.headers["x-export-skipped"])
        self.assertEqual(skipped["nonexistent"], "not_found")
        self.assertEqual(skipped["GFSA01_02"], "not_found")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(len(zf.namelist()), 1)

    def test_unresolved_chain_skipped_in_flattened(self):
        self._add_second_user_filament()
        # Break only Eryone's chain (after the reload).
        user_key = profiles._profile_key("User", "Eryone Matte Imported")
        profiles._raw_profiles[user_key]["inherits"] = "Nonexistent Parent"
        profiles._resolved_cache.clear()
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={
                "setting_ids": ["Eryone Matte Imported", "Other PLA"],
                "shape": "flattened",
            },
        )
        self.assertEqual(r.status_code, 200)
        skipped = json.loads(r.headers["x-export-skipped"])
        self.assertEqual(skipped["Eryone Matte Imported"], "unresolved_chain")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        # Other PLA × 2 variant-count groups = 2 entries; Eryone skipped.
        self.assertEqual(len(zf.namelist()), 2)

    def test_no_compatible_printers_reported(self):
        # Add a user filament with empty compatible_printers.
        self._write_json(
            self.user_dir / "filament" / "Lonely.json",
            {
                "name": "Lonely",
                "setting_id": "Lonely",
                "instantiation": "true",
                "from": "User",
                "filament_id": "Plonely1",
                "compatible_printers": [],
                "version": "1.9.0.21",
            },
        )
        profiles.load_all_profiles()
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={"setting_ids": ["Lonely"], "shape": "flattened"},
        )
        self.assertEqual(r.status_code, 200)
        skipped = json.loads(r.headers["x-export-skipped"])
        self.assertEqual(skipped["Lonely"], "no_compatible_printers")

    def test_filename_collisions_get_suffix(self):
        # Two user filaments whose flattened names happen to sanitize
        # the same way after we tweak names to collide.
        self._write_json(
            self.user_dir / "filament" / "PLA-A.json",
            {
                "name": "PLA A",
                "setting_id": "PLA-A",
                "instantiation": "true",
                "from": "User",
                "filament_id": "Ppl_a___",
                "inherits": "Bambu PLA Matte @BBL A1M",
                "version": "1.9.0.21",
            },
        )
        self._write_json(
            self.user_dir / "filament" / "PLA-B.json",
            {
                "name": "pla a",  # sanitizes the same as "PLA A"
                "setting_id": "PLA-B",
                "instantiation": "true",
                "from": "User",
                "filament_id": "Ppl_b___",
                "inherits": "Bambu PLA Matte @BBL A1M",
                "version": "1.9.0.21",
            },
        )
        profiles.load_all_profiles()
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={"setting_ids": ["PLA-A", "PLA-B"], "shape": "thin"},
        )
        self.assertEqual(r.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = sorted(zf.namelist())
        # Both files made it in, second got a -2 suffix.
        self.assertEqual(len(names), 2)
        self.assertTrue(any("-2.json" in n for n in names))

    def test_empty_setting_ids_returns_400(self):
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={"setting_ids": [], "shape": "thin"},
        )
        self.assertEqual(r.status_code, 400)

    def test_missing_setting_ids_returns_400(self):
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={"shape": "thin"},
        )
        self.assertEqual(r.status_code, 400)

    def test_invalid_shape_returns_400(self):
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={"setting_ids": ["Eryone Matte Imported"], "shape": "bogus"},
        )
        self.assertEqual(r.status_code, 400)

    def test_invalid_json_returns_400(self):
        r = self.client.post(
            "/profiles/filaments/export-batch",
            data="not json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
