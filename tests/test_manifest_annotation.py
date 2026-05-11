"""Unit tests for ``app.manifest.annotate_profile_cache``.

These cover the bug where ``orca-headless dump-profiles`` emits
manifest entries for user-imported filaments with empty ``setting_id``
and (when the user profile inherits) the parent's ``filament_id``
instead of the leaf's stamped one. The annotator must repair both from
the on-disk ``_raw_profiles`` entry so the listing endpoints agree
with the detail endpoint and ``ams_assignable`` reflects reality.

See the bug report in the conversation history for the two patterns:
    Pattern A — base user profile, no ``inherits``: setting_id dropped.
    Pattern B — user profile with ``inherits``: setting_id dropped AND
                filament_id bleeds from parent.

The C++ binary intentionally mirrors GUI behavior (Bambu Studio's
``Preset.cpp::load_presets`` doesn't read setting_id from user JSON
and overwrites filament_id with the inherited value when an inherits
chain exists). Our wrapper's import convention (stamping both fields
into user JSON) is enforced in Python because that is where it was
authored. Keep the binary GUI-aligned; the annotator reconciles.
"""

from __future__ import annotations

import unittest

from app import manifest as manifest_mod
from app import profiles


class AnnotateProfileCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()

    def tearDown(self) -> None:
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()

    def _index_raw_filament(
        self,
        *,
        vendor: str,
        name: str,
        setting_id: str,
        filament_id: str,
        inherits: str = "",
    ) -> None:
        raw: dict[str, object] = {
            "name":          name,
            "setting_id":    setting_id,
            "filament_id":   filament_id,
            "instantiation": "true",
            "from":          "User",
        }
        if inherits:
            raw["inherits"] = inherits
        profiles._index_profile(
            profiles._profile_key(vendor, name), raw, "filament", vendor,
        )

    def test_repairs_setting_id_for_base_user_filament(self) -> None:
        # Pattern A: user-imported filament with no inherits. The disk
        # JSON has setting_id; the manifest emits "" (libslic3r's user
        # load path doesn't read it). Annotator must repair it and
        # flip ams_assignable to True.
        self._index_raw_filament(
            vendor="User",
            name="SUNLU PETG HS Matte - Balanced @Bambu Lab A1 mini 0.4 nozzle",
            setting_id="SUNLU PETG HS Matte - Balanced @Bambu Lab A1 mini 0.4 nozzle",
            filament_id="S3794182",
        )
        manifest = {
            "machines":  [],
            "processes": [],
            "filaments": [{
                "setting_id":          "",
                "filament_id":         "S3794182",
                "name":                "SUNLU PETG HS Matte - Balanced @Bambu Lab A1 mini 0.4 nozzle",
                "vendor":              "User",
                "compatible_printers": ["GM020"],
                "filament_type":       "PETG",
                "ams_assignable":      False,
            }],
        }

        manifest_mod.annotate_profile_cache(manifest)

        entry = manifest["filaments"][0]
        self.assertEqual(
            entry["setting_id"],
            "SUNLU PETG HS Matte - Balanced @Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(entry["filament_id"], "S3794182")
        self.assertTrue(entry["ams_assignable"])

    def test_repairs_filament_id_for_inheriting_user_filament(self) -> None:
        # Pattern B: user-imported filament with inherits. The disk
        # JSON has the stamped Pxxxxxxx id; the manifest carries the
        # parent's id because Preset.cpp:1314 overwrites it during
        # JSON load. Annotator must restore the leaf's stamped id so
        # multiple clones of the same parent don't collide on AMS
        # identity.
        self._index_raw_filament(
            vendor="User",
            name="DEEPLEE Wood Imported",
            setting_id="DEEPLEE Wood Imported",
            filament_id="P0f56d26",
            inherits="Bambu PLA Basic @BBL A1M",
        )
        manifest = {
            "machines":  [],
            "processes": [],
            "filaments": [{
                "setting_id":          "",
                "filament_id":         "GFA16",
                "name":                "DEEPLEE Wood Imported",
                "vendor":              "User",
                "compatible_printers": ["GM020"],
                "filament_type":       "PLA",
                "ams_assignable":      False,
            }],
        }

        manifest_mod.annotate_profile_cache(manifest)

        entry = manifest["filaments"][0]
        self.assertEqual(entry["setting_id"], "DEEPLEE Wood Imported")
        self.assertEqual(entry["filament_id"], "P0f56d26")
        self.assertTrue(entry["ams_assignable"])

    def test_does_not_disturb_system_filament_with_matching_values(self) -> None:
        # System (vendor) filaments already have correct setting_id /
        # filament_id from the bundle index. Annotator's repair logic
        # must be a no-op for them.
        self._index_raw_filament(
            vendor="BBL",
            name="Bambu PLA Basic @BBL A1M",
            setting_id="GFSA00",
            filament_id="GFA00",
            inherits="fdm_filament_pla",
        )
        manifest = {
            "machines":  [],
            "processes": [],
            "filaments": [{
                "setting_id":          "GFSA00",
                "filament_id":         "GFA00",
                "name":                "Bambu PLA Basic @BBL A1M",
                "vendor":              "BBL",
                "compatible_printers": ["GM020"],
                "filament_type":       "PLA",
                "ams_assignable":      True,
            }],
        }

        manifest_mod.annotate_profile_cache(manifest)

        entry = manifest["filaments"][0]
        self.assertEqual(entry["setting_id"], "GFSA00")
        self.assertEqual(entry["filament_id"], "GFA00")
        self.assertTrue(entry["ams_assignable"])

    def test_repairs_user_filament_when_manifest_vendor_is_empty(self) -> None:
        # Reality check from a deployed instance: libslic3r leaves
        # ``preset.vendor`` null for user-imported profiles, so the
        # manifest emits ``vendor=""``. The legacy walker indexes user
        # filaments under ``_profile_key("User", name)``. A naive
        # ``_profile_key(entry.vendor, entry.name)`` lookup misses,
        # the entry falls through to the synthesis branch, and the
        # empty manifest ``setting_id`` is copied verbatim — defeating
        # the whole repair. The annotator must fall back to the
        # "User" vendor (or a name-only lookup) before synthesizing.
        self._index_raw_filament(
            vendor="User",
            name="DEEPLEE Wood Imported",
            setting_id="DEEPLEE Wood Imported",
            filament_id="P0f56d26",
            inherits="Bambu PLA Basic @BBL A1M",
        )
        manifest = {
            "machines":  [],
            "processes": [],
            "filaments": [{
                "setting_id":          "",
                "filament_id":         "GFA16",
                "name":                "DEEPLEE Wood Imported",
                "vendor":              "",  # <-- libslic3r emits "" for user profiles
                "compatible_printers": ["GM020"],
                "filament_type":       "PLA",
                "ams_assignable":      False,
            }],
        }

        manifest_mod.annotate_profile_cache(manifest)

        entry = manifest["filaments"][0]
        self.assertEqual(entry["setting_id"], "DEEPLEE Wood Imported")
        self.assertEqual(entry["filament_id"], "P0f56d26")
        self.assertTrue(entry["ams_assignable"])

    def test_stamps_user_vendor_when_manifest_vendor_is_empty(self) -> None:
        # libslic3r emits ``vendor=""`` for user-imported presets, but the
        # webui filters with ``vendor === "User"`` and would otherwise miss
        # them in the User view. The annotator restores the vendor from
        # the legacy walker's ``_vendor_map`` so the listing shape is
        # self-describing.
        self._index_raw_filament(
            vendor="User",
            name="DEEPLEE Wood Imported",
            setting_id="DEEPLEE Wood Imported",
            filament_id="P0f56d26",
            inherits="Bambu PLA Basic @BBL A1M",
        )
        manifest = {
            "machines":  [],
            "processes": [],
            "filaments": [{
                "setting_id":          "",
                "filament_id":         "GFA16",
                "name":                "DEEPLEE Wood Imported",
                "vendor":              "",
                "compatible_printers": ["GM020"],
                "filament_type":       "PLA",
                "ams_assignable":      False,
            }],
        }

        manifest_mod.annotate_profile_cache(manifest)

        self.assertEqual(manifest["filaments"][0]["vendor"], "User")

    def test_does_not_overwrite_existing_vendor(self) -> None:
        # BBL system profiles arrive with a populated vendor — the
        # User-vendor repair must not clobber it.
        self._index_raw_filament(
            vendor="BBL",
            name="Bambu PLA Basic @BBL A1M",
            setting_id="GFSA00",
            filament_id="GFA00",
        )
        manifest = {
            "machines":  [],
            "processes": [],
            "filaments": [{
                "setting_id":          "GFSA00",
                "filament_id":         "GFA00",
                "name":                "Bambu PLA Basic @BBL A1M",
                "vendor":              "BBL",
                "compatible_printers": ["GM020"],
                "filament_type":       "PLA",
                "ams_assignable":      True,
            }],
        }

        manifest_mod.annotate_profile_cache(manifest)

        self.assertEqual(manifest["filaments"][0]["vendor"], "BBL")

    def test_keeps_ams_assignable_false_when_raw_lacks_filament_id(self) -> None:
        # A user filament whose on-disk JSON also lacks a filament_id
        # (degenerate, but possible) must stay non-assignable even
        # after annotation. The repair logic only fills from raw when
        # raw has the value.
        profiles._index_profile(
            profiles._profile_key("User", "Half-imported"),
            {
                "name":          "Half-imported",
                "setting_id":    "Half-imported",
                "instantiation": "true",
                "from":          "User",
            },
            "filament",
            "User",
        )
        manifest = {
            "machines":  [],
            "processes": [],
            "filaments": [{
                "setting_id":          "",
                "filament_id":         "",
                "name":                "Half-imported",
                "vendor":              "User",
                "compatible_printers": ["GM020"],
                "filament_type":       "PLA",
                "ams_assignable":      False,
            }],
        }

        manifest_mod.annotate_profile_cache(manifest)

        entry = manifest["filaments"][0]
        self.assertEqual(entry["setting_id"], "Half-imported")
        self.assertEqual(entry["filament_id"], "")
        self.assertFalse(entry["ams_assignable"])


if __name__ == "__main__":
    unittest.main()
