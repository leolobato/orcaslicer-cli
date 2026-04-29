import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


class StrictResolveProfileByNameTests(unittest.TestCase):
    """`resolve_profile_by_name` raises on broken inherits chains."""

    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-strict-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

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

    def _index_profile(self, vendor: str, data: dict, category: str) -> str:
        key = profiles._profile_key(vendor, str(data["name"]))
        profiles._index_profile(key, data, category, vendor)
        return key

    def test_raises_when_inherits_parent_is_unknown(self) -> None:
        self._index_profile(
            "User",
            {"name": "Child", "inherits": "Missing Parent"},
            "filament",
        )

        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles.resolve_profile_by_name("User::Child")

        msg = str(ctx.exception)
        self.assertIn("Child", msg)
        self.assertIn("Missing Parent", msg)

    def test_raises_when_intermediate_parent_in_chain_is_missing(self) -> None:
        self._index_profile(
            "User",
            {"name": "Leaf", "inherits": "Middle"},
            "filament",
        )
        self._index_profile(
            "User",
            {"name": "Middle", "inherits": "Root Gone"},
            "filament",
        )

        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles.resolve_profile_by_name("User::Leaf")

        msg = str(ctx.exception)
        self.assertIn("Root Gone", msg)

    def test_returns_merged_dict_on_successful_chain(self) -> None:
        self._index_profile(
            "User",
            {"name": "Parent", "filament_type": ["PLA"], "filament_id": "X1"},
            "filament",
        )
        self._index_profile(
            "User",
            {"name": "Child", "inherits": "Parent", "nozzle_temperature": ["230"]},
            "filament",
        )

        resolved = profiles.resolve_profile_by_name("User::Child")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["filament_type"], ["PLA"])
        self.assertEqual(resolved["filament_id"], "X1")
        self.assertEqual(resolved["nozzle_temperature"], ["230"])
        self.assertEqual(resolved["name"], "Child")
