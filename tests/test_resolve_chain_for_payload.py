import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles
from tests._profile_test_helpers import reset_profiles_state


class ResolveChainForPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_profiles_state()
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
        reset_profiles_state()
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

    def test_raises_value_error_for_unsupported_category(self) -> None:
        # The helper accepts only "filament" and "process". Anything else
        # is a programmer error, not a data error.
        with self.assertRaises(ValueError) as ctx:
            profiles._resolve_chain_for_payload(
                {"name": "X", "inherits": "Bambu PLA Basic @BBL A1M"},
                category="machine",
            )
        self.assertIn("machine", str(ctx.exception))

    def test_resolves_multi_level_chain(self) -> None:
        # Index a grandparent → parent → (payload) chain so the recursive
        # resolve_profile_by_name walk is exercised through the helper.
        profiles._index_profile(
            "BBL::Generic PLA Base",
            {
                "name": "Generic PLA Base",
                "filament_type": ["PLA"],
                "filament_id": "BASE1",
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
                "default_print_temperature": ["210"],
            },
            "filament",
            "BBL",
        )
        profiles._index_profile(
            "BBL::Mid PLA",
            {
                "name": "Mid PLA",
                "inherits": "Generic PLA Base",
                "nozzle_temperature": ["220"],
            },
            "filament",
            "BBL",
        )

        merged = profiles._resolve_chain_for_payload(
            {"name": "Leaf", "inherits": "Mid PLA", "nozzle_temperature_initial_layer": ["225"]},
            category="filament",
        )

        # Inherited from grandparent.
        self.assertEqual(merged["filament_type"], ["PLA"])
        self.assertEqual(merged["filament_id"], "BASE1")
        self.assertEqual(merged["default_print_temperature"], ["210"])
        # Inherited from intermediate parent.
        self.assertEqual(merged["nozzle_temperature"], ["220"])
        # Set on the payload.
        self.assertEqual(merged["nozzle_temperature_initial_layer"], ["225"])
        self.assertEqual(merged["name"], "Leaf")

    def test_resolves_process_category(self) -> None:
        # Mirror the filament happy path for category="process".
        profiles._index_profile(
            "BBL::0.20mm Standard",
            {
                "name": "0.20mm Standard",
                "setting_id": "GP004",
                "instantiation": "true",
                "layer_height": ["0.2"],
                "outer_wall_speed": ["200"],
            },
            "process",
            "BBL",
        )

        merged = profiles._resolve_chain_for_payload(
            {"name": "Custom Process", "inherits": "0.20mm Standard", "outer_wall_speed": ["150"]},
            category="process",
        )

        self.assertEqual(merged["layer_height"], ["0.2"])
        self.assertEqual(merged["outer_wall_speed"], ["150"])
        self.assertEqual(merged["name"], "Custom Process")
