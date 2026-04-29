import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


def _reset_profiles_state() -> None:
    profiles._raw_profiles.clear()
    profiles._type_map.clear()
    profiles._vendor_map.clear()
    profiles._name_index.clear()
    profiles._resolved_cache.clear()
    profiles._setting_id_index.clear()


class ResolveChainForPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_profiles_state()
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-chainpayload-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

        # A vendor parent that the payload will inherit from.
        profiles._index_profile(
            "BBL::Bambu PLA Basic @BBL A1M",
            {
                "name": "Bambu PLA Basic @BBL A1M",
                "setting_id": "GFA00_A1M",
                "instantiation": "true",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
                "nozzle_temperature": ["220"],
            },
            "filament",
            "BBL",
        )

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        _reset_profiles_state()
        shutil.rmtree(self.tempdir)

    def test_returns_payload_when_no_inherits(self) -> None:
        payload = {"name": "Standalone", "filament_type": ["PLA"], "filament_id": "X1"}

        merged = profiles._resolve_chain_for_payload(payload, category="filament")

        self.assertEqual(merged, payload)
        # Original input is not mutated.
        self.assertNotIn("from", payload)

    def test_overlays_payload_on_resolved_parent(self) -> None:
        payload = {
            "name": "My Custom",
            "inherits": "Bambu PLA Basic @BBL A1M",
            "nozzle_temperature": ["230"],
        }

        merged = profiles._resolve_chain_for_payload(payload, category="filament")

        # Inherited from parent.
        self.assertEqual(merged["filament_type"], ["PLA"])
        self.assertEqual(merged["filament_id"], "GFA00")
        self.assertEqual(
            merged["compatible_printers"], ["Bambu Lab A1 mini 0.4 nozzle"]
        )
        # Overlaid by payload.
        self.assertEqual(merged["nozzle_temperature"], ["230"])
        self.assertEqual(merged["name"], "My Custom")

    def test_raises_when_parent_is_unknown(self) -> None:
        payload = {"name": "Orphan", "inherits": "No Such Parent"}

        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles._resolve_chain_for_payload(payload, category="filament")

        self.assertIn("No Such Parent", str(ctx.exception))
