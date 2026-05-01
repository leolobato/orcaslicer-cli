import json
import unittest

from app import profiles
from tests._profile_test_helpers import reset_profiles_state


class SafeFilenameTests(unittest.TestCase):
    def test_lowercases_and_replaces_unsafe_chars(self):
        self.assertEqual(
            profiles._safe_filename("Eryone Matte Imported", fallback="x"),
            "eryone_matte_imported.json",
        )

    def test_preserves_at_sign_replacement(self):
        # @ is not in [a-z0-9._-], so it gets replaced with _.
        result = profiles._safe_filename(
            "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
            fallback="x",
        )
        self.assertEqual(
            result,
            "eryone_matte_imported_bambu_lab_a1_mini_0.4_nozzle.json",
        )

    def test_collapses_runs_of_underscores(self):
        self.assertEqual(
            profiles._safe_filename("a !! b", fallback="x"),
            "a_b.json",
        )

    def test_trims_edges(self):
        self.assertEqual(
            profiles._safe_filename("  hello  ", fallback="x"),
            "hello.json",
        )

    def test_falls_back_when_empty(self):
        self.assertEqual(profiles._safe_filename("???", fallback="GFXX01"), "gfxx01.json")
        self.assertEqual(profiles._safe_filename("", fallback="GFXX01"), "gfxx01.json")

    def test_falls_back_to_literal_profile_when_both_empty(self):
        self.assertEqual(profiles._safe_filename("???", fallback="!!!"), "profile.json")


class FilamentAliasTests(unittest.TestCase):
    def test_strips_at_suffix(self):
        self.assertEqual(
            profiles._filament_alias("Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle"),
            "Eryone Matte Imported",
        )

    def test_returns_unchanged_when_no_at(self):
        self.assertEqual(
            profiles._filament_alias("Eryone Matte Imported"),
            "Eryone Matte Imported",
        )

    def test_strips_only_at_first_at(self):
        self.assertEqual(
            profiles._filament_alias("foo @bar @baz"),
            "foo",
        )

    def test_idempotent_on_already_stripped(self):
        # Re-export should not double-strip.
        once = profiles._filament_alias("PLA @Printer A")
        twice = profiles._filament_alias(once)
        self.assertEqual(once, twice)
        self.assertEqual(once, "PLA")


class PrinterVariantCountTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_profiles_state()

    def tearDown(self) -> None:
        reset_profiles_state()

    def _index_machine(self, name: str, raw: dict) -> None:
        # Mimic _index_profile but in-place to avoid disk loading.
        key = profiles._profile_key("BBL", name)
        profiles._raw_profiles[key] = {**raw, "name": name}
        profiles._type_map[key] = "machine"
        profiles._vendor_map[key] = "BBL"
        profiles._name_index.setdefault(name, []).append(key)

    def test_returns_length_of_printer_extruder_variant(self):
        self._index_machine(
            "Bambu Lab P1P 0.4 nozzle",
            {
                "printer_extruder_variant": ["Direct Drive Standard", "Direct Drive High Flow"],
            },
        )
        self.assertEqual(profiles._printer_variant_count("Bambu Lab P1P 0.4 nozzle"), 2)

    def test_returns_one_for_single_variant_printer(self):
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {
                "printer_extruder_variant": ["Direct Drive Standard"],
            },
        )
        self.assertEqual(profiles._printer_variant_count("Bambu Lab A1 mini 0.4 nozzle"), 1)

    def test_inherits_from_parent_when_field_absent(self):
        self._index_machine(
            "fdm_bbl_3dp_001_common",
            {
                "printer_extruder_variant": ["Direct Drive Standard", "Direct Drive High Flow"],
            },
        )
        self._index_machine(
            "Bambu Lab P1P 0.4 nozzle",
            {"inherits": "fdm_bbl_3dp_001_common"},
        )
        self.assertEqual(profiles._printer_variant_count("Bambu Lab P1P 0.4 nozzle"), 2)

    def test_returns_one_when_printer_unknown(self):
        self.assertEqual(profiles._printer_variant_count("Unknown Printer"), 1)

    def test_returns_one_when_field_missing_after_resolution(self):
        self._index_machine("Plain Printer", {})
        self.assertEqual(profiles._printer_variant_count("Plain Printer"), 1)


class PadPerVariantKeysTests(unittest.TestCase):
    def test_no_padding_when_variant_count_is_one(self):
        profile = {
            "nozzle_temperature": ["210"],
            "compatible_printers": ["Printer A"],
        }
        out = profiles._pad_per_variant_keys(dict(profile), variant_count=1)
        # Length-1 list stays length-1 when target is 1.
        self.assertEqual(out["nozzle_temperature"], ["210"])
        # Non-per-variant keys untouched.
        self.assertEqual(out["compatible_printers"], ["Printer A"])

    def test_pads_known_per_variant_key_to_target_length(self):
        profile = {"nozzle_temperature": ["210"]}
        out = profiles._pad_per_variant_keys(dict(profile), variant_count=2)
        self.assertEqual(out["nozzle_temperature"], ["210", "210"])

    def test_does_not_pad_compatible_printers(self):
        profile = {
            "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            "nozzle_temperature": ["210"],
        }
        out = profiles._pad_per_variant_keys(dict(profile), variant_count=2)
        self.assertEqual(out["compatible_printers"], ["Bambu Lab A1 mini 0.4 nozzle"])
        self.assertEqual(out["nozzle_temperature"], ["210", "210"])

    def test_does_not_pad_unknown_keys(self):
        profile = {"some_random_key": ["a"]}
        out = profiles._pad_per_variant_keys(dict(profile), variant_count=2)
        self.assertEqual(out["some_random_key"], ["a"])

    def test_leaves_already_padded_keys_alone(self):
        profile = {"nozzle_temperature": ["210", "215"]}
        out = profiles._pad_per_variant_keys(dict(profile), variant_count=2)
        self.assertEqual(out["nozzle_temperature"], ["210", "215"])

    def test_extends_when_existing_length_below_target(self):
        # Length 2 input, target 3 — pad with last existing value.
        profile = {"nozzle_temperature": ["210", "215"]}
        out = profiles._pad_per_variant_keys(dict(profile), variant_count=3)
        self.assertEqual(out["nozzle_temperature"], ["210", "215", "215"])

    def test_overrides_filament_extruder_variant_when_padded(self):
        # filament_extruder_variant is special: its slot values are the
        # variant *labels*, not a replicable scalar. We replace it with
        # the printer's variant labels passed in.
        profile = {"filament_extruder_variant": ["Direct Drive Standard"]}
        out = profiles._pad_per_variant_keys(
            dict(profile),
            variant_count=2,
            variant_labels=["Direct Drive Standard", "Direct Drive High Flow"],
        )
        self.assertEqual(
            out["filament_extruder_variant"],
            ["Direct Drive Standard", "Direct Drive High Flow"],
        )

    def test_skips_non_list_values(self):
        # filament_id is a scalar string — must not be wrapped/padded.
        profile = {"filament_id": "P1234567", "nozzle_temperature": ["210"]}
        out = profiles._pad_per_variant_keys(dict(profile), variant_count=2)
        self.assertEqual(out["filament_id"], "P1234567")
        self.assertEqual(out["nozzle_temperature"], ["210", "210"])


class LongestWordPrefixTests(unittest.TestCase):
    def test_three_a1_mini_nozzles(self):
        result = profiles._longest_word_prefix([
            "Bambu Lab A1 mini 0.4 nozzle",
            "Bambu Lab A1 mini 0.6 nozzle",
            "Bambu Lab A1 mini 0.8 nozzle",
        ])
        self.assertEqual(result, "Bambu Lab A1 mini")

    def test_a1_mini_and_p1p(self):
        result = profiles._longest_word_prefix([
            "Bambu Lab A1 mini 0.4 nozzle",
            "Bambu Lab P1P 0.4 nozzle",
        ])
        self.assertEqual(result, "Bambu Lab")

    def test_different_first_words(self):
        result = profiles._longest_word_prefix(["Foo", "Bar"])
        self.assertEqual(result, "")

    def test_single_string_returns_empty(self):
        result = profiles._longest_word_prefix(["Only One"])
        self.assertEqual(result, "")

    def test_empty_list_returns_empty(self):
        result = profiles._longest_word_prefix([])
        self.assertEqual(result, "")

    def test_identical_strings_return_full_string(self):
        result = profiles._longest_word_prefix(["Same", "Same"])
        self.assertEqual(result, "Same")


class FlattenForPrinterTests(unittest.TestCase):
    """Tests for `_flatten_user_filament_for_printers` (plural)."""

    def setUp(self) -> None:
        reset_profiles_state()

    def tearDown(self) -> None:
        reset_profiles_state()

    def _index_machine(self, name: str, raw: dict) -> None:
        key = profiles._profile_key("BBL", name)
        profiles._raw_profiles[key] = {**raw, "name": name}
        profiles._type_map[key] = "machine"
        profiles._vendor_map[key] = "BBL"
        profiles._name_index.setdefault(name, []).append(key)

    def _resolved_filament(self) -> dict:
        # Mimic the result of get_profile("filament", ...) for the
        # Eryone Matte Imported chain — keys we know matter for the
        # export shape.
        return {
            "type": "filament",
            "name": "Eryone Matte Imported",
            "from": "User",
            "instantiation": "true",
            "setting_id": "Eryone Matte Imported",
            "filament_id": "Pfd5d97d",
            "filament_type": ["PLA"],
            "filament_vendor": ["Bambu Lab"],
            "filament_settings_id": ["Eryone Matte Imported"],
            "nozzle_temperature": ["210"],
            "nozzle_temperature_initial_layer": ["210"],
            "filament_extruder_variant": ["Direct Drive Standard"],
            "compatible_printers": [
                "Bambu Lab A1 mini 0.4 nozzle",
                "Bambu Lab A1 mini 0.6 nozzle",
            ],
            "version": "1.9.0.21",
        }

    def test_strips_vendor_markers_and_sets_inherits_empty(self):
        out = profiles._flatten_user_filament_for_printers(
            self._resolved_filament(),
            printers=["Bambu Lab A1 mini 0.4 nozzle"],
            name_label="Bambu Lab A1 mini 0.4 nozzle",
            variant_count=1,
        )
        self.assertNotIn("type", out)
        self.assertNotIn("instantiation", out)
        self.assertNotIn("setting_id", out)
        self.assertNotIn("base_id", out)
        self.assertEqual(out["inherits"], "")

    def test_renames_to_alias_at_label(self):
        out = profiles._flatten_user_filament_for_printers(
            self._resolved_filament(),
            printers=["Bambu Lab A1 mini 0.4 nozzle"],
            name_label="Bambu Lab A1 mini 0.4 nozzle",
            variant_count=1,
        )
        self.assertEqual(
            out["name"], "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(
            out["filament_settings_id"],
            ["Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle"],
        )

    def test_sets_compatible_printers_to_given_list(self):
        out = profiles._flatten_user_filament_for_printers(
            self._resolved_filament(),
            printers=["Bambu Lab A1 mini 0.4 nozzle"],
            name_label="Bambu Lab A1 mini 0.4 nozzle",
            variant_count=1,
        )
        self.assertEqual(out["compatible_printers"], ["Bambu Lab A1 mini 0.4 nozzle"])

    def test_adds_empty_default_compat_fields(self):
        out = profiles._flatten_user_filament_for_printers(
            self._resolved_filament(),
            printers=["Bambu Lab A1 mini 0.4 nozzle"],
            name_label="Bambu Lab A1 mini 0.4 nozzle",
            variant_count=1,
        )
        self.assertEqual(out["compatible_printers_condition"], "")
        self.assertEqual(out["compatible_prints"], [])
        self.assertEqual(out["compatible_prints_condition"], "")

    def test_pads_per_variant_keys_against_two_variant_count(self):
        out = profiles._flatten_user_filament_for_printers(
            self._resolved_filament(),
            printers=["Bambu Lab P1P 0.4 nozzle"],
            name_label="Bambu Lab P1P 0.4 nozzle",
            variant_count=2,
            variant_labels=["Direct Drive Standard", "Direct Drive High Flow"],
        )
        self.assertEqual(out["nozzle_temperature"], ["210", "210"])
        self.assertEqual(
            out["filament_extruder_variant"],
            ["Direct Drive Standard", "Direct Drive High Flow"],
        )

    def test_idempotent_re_export(self):
        # Already has @<old printer> in name — should not stack suffixes.
        resolved = self._resolved_filament()
        resolved["name"] = "Eryone Matte Imported @Some Other Printer"
        out = profiles._flatten_user_filament_for_printers(
            resolved,
            printers=["Bambu Lab A1 mini 0.4 nozzle"],
            name_label="Bambu Lab A1 mini 0.4 nozzle",
            variant_count=1,
        )
        self.assertEqual(
            out["name"], "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
        )

    def test_does_not_mutate_input(self):
        resolved = self._resolved_filament()
        snapshot = json.loads(json.dumps(resolved))
        profiles._flatten_user_filament_for_printers(
            resolved,
            printers=["Bambu Lab A1 mini 0.4 nozzle"],
            name_label="Bambu Lab A1 mini 0.4 nozzle",
            variant_count=1,
        )
        self.assertEqual(resolved, snapshot)

    def test_multiple_printers_in_compatible_printers(self):
        # Consolidated group: name_label is the prefix, printers lists all.
        out = profiles._flatten_user_filament_for_printers(
            self._resolved_filament(),
            printers=[
                "Bambu Lab A1 mini 0.4 nozzle",
                "Bambu Lab A1 mini 0.6 nozzle",
            ],
            name_label="Bambu Lab A1 mini",
            variant_count=1,
        )
        self.assertEqual(out["name"], "Eryone Matte Imported @Bambu Lab A1 mini")
        self.assertEqual(out["compatible_printers"], [
            "Bambu Lab A1 mini 0.4 nozzle",
            "Bambu Lab A1 mini 0.6 nozzle",
        ])


class ExportUserFilamentTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_profiles_state()

    def tearDown(self) -> None:
        reset_profiles_state()

    def _index(self, vendor: str, name: str, category: str, raw: dict) -> None:
        key = profiles._profile_key(vendor, name)
        profiles._raw_profiles[key] = {**raw, "name": name}
        profiles._type_map[key] = category
        profiles._vendor_map[key] = vendor
        profiles._name_index.setdefault(name, []).append(key)
        sid = raw.get("setting_id")
        if sid:
            profiles._setting_id_index.setdefault(sid, []).append(key)

    def _scaffold(self) -> None:
        # Minimal: vendor parent filament, machine profile, user child.
        self._index(
            "BBL", "Bambu PLA Matte @BBL A1M", "filament",
            {
                "setting_id": "GFSA01_02",
                "instantiation": "true",
                "from": "system",
                "filament_id": "GFA01",
                "filament_type": ["PLA"],
                "compatible_printers": [
                    "Bambu Lab A1 mini 0.4 nozzle",
                    "Bambu Lab A1 mini 0.6 nozzle",
                ],
                "nozzle_temperature": ["220"],
            },
        )
        self._index(
            "BBL", "Bambu Lab A1 mini 0.4 nozzle", "machine",
            {
                "setting_id": "GM_A1MINI04",
                "instantiation": "true",
                "from": "system",
                "printer_extruder_variant": ["Direct Drive Standard"],
            },
        )
        self._index(
            "BBL", "Bambu Lab A1 mini 0.6 nozzle", "machine",
            {
                "setting_id": "GM_A1MINI06",
                "instantiation": "true",
                "from": "system",
                "printer_extruder_variant": ["Direct Drive Standard"],
            },
        )
        self._index(
            "User", "Eryone Matte Imported", "filament",
            {
                "setting_id": "Eryone Matte Imported",
                "instantiation": "true",
                "from": "User",
                "filament_id": "Pfd5d97d",
                "inherits": "Bambu PLA Matte @BBL A1M",
                "nozzle_temperature": ["210"],
                "version": "1.9.0.21",
            },
        )

    def test_thin_returns_one_entry_with_saved_file(self):
        self._scaffold()
        entries = profiles.export_user_filament("Eryone Matte Imported", shape="thin")
        self.assertEqual(len(entries), 1)
        filename, data = entries[0]
        self.assertEqual(filename, "eryone_matte_imported.json")
        self.assertEqual(data["inherits"], "Bambu PLA Matte @BBL A1M")
        self.assertEqual(data["filament_id"], "Pfd5d97d")
        # Thin returns the saved file — vendor markers preserved.
        self.assertEqual(data["instantiation"], "true")

    def test_flattened_consolidates_printers_with_common_prefix(self):
        # Both A1 mini nozzles share the prefix "Bambu Lab A1 mini" and
        # the same variant_count=1, so they consolidate into ONE entry.
        self._scaffold()
        entries = profiles.export_user_filament("Eryone Matte Imported", shape="flattened")
        self.assertEqual(len(entries), 1)
        filename, data = entries[0]
        self.assertEqual(data["name"], "Eryone Matte Imported @Bambu Lab A1 mini")
        self.assertEqual(data["compatible_printers"], [
            "Bambu Lab A1 mini 0.4 nozzle",
            "Bambu Lab A1 mini 0.6 nozzle",
        ])
        self.assertEqual(data["inherits"], "")
        self.assertNotIn("setting_id", data)

    def test_flattened_filename_uses_common_prefix(self):
        self._scaffold()
        entries = profiles.export_user_filament("Eryone Matte Imported", shape="flattened")
        self.assertEqual(len(entries), 1)
        filename, _ = entries[0]
        self.assertEqual(filename, "eryone_matte_imported_bambu_lab_a1_mini.json")

    def test_flattened_no_common_prefix_falls_back_to_per_printer(self):
        # Two printers with different first words → no shared prefix → two files.
        self._scaffold()
        self._index(
            "BBL", "Prusa MK4", "machine",
            {
                "setting_id": "GM_PRUSA_MK4",
                "instantiation": "true",
                "from": "system",
                "printer_extruder_variant": ["Direct Drive Standard"],
            },
        )
        # Reindex the BBL parent to include the Prusa printer.
        bbl_key = profiles._profile_key("BBL", "Bambu PLA Matte @BBL A1M")
        profiles._raw_profiles[bbl_key]["compatible_printers"] = [
            "Bambu Lab A1 mini 0.4 nozzle",
            "Prusa MK4",
        ]
        profiles._resolved_cache.clear()

        entries = profiles.export_user_filament("Eryone Matte Imported", shape="flattened")
        # "Bambu Lab A1 mini 0.4 nozzle" and "Prusa MK4" share no prefix.
        self.assertEqual(len(entries), 2)
        names = sorted(e[1]["name"] for e in entries)
        self.assertEqual(names, [
            "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
            "Eryone Matte Imported @Prusa MK4",
        ])

    def test_flattened_groups_by_variant_count(self):
        # Add a two-variant printer to the parent's compatible_printers.
        self._scaffold()
        self._index(
            "BBL", "Bambu Lab P1P 0.4 nozzle", "machine",
            {
                "setting_id": "GM_P1P04",
                "instantiation": "true",
                "from": "system",
                "printer_extruder_variant": [
                    "Direct Drive Standard", "Direct Drive High Flow",
                ],
            },
        )
        bbl_key = profiles._profile_key("BBL", "Bambu PLA Matte @BBL A1M")
        profiles._raw_profiles[bbl_key]["compatible_printers"] = [
            "Bambu Lab A1 mini 0.4 nozzle",
            "Bambu Lab A1 mini 0.6 nozzle",
            "Bambu Lab P1P 0.4 nozzle",
        ]
        profiles._resolved_cache.clear()

        entries = profiles.export_user_filament("Eryone Matte Imported", shape="flattened")
        # variant_count=1 group: A1 mini 0.4 + 0.6 → consolidated (1 entry).
        # variant_count=2 group: P1P 0.4 → single printer (1 entry).
        self.assertEqual(len(entries), 2)
        names = sorted(e[1]["name"] for e in entries)
        self.assertEqual(names, [
            "Eryone Matte Imported @Bambu Lab A1 mini",
            "Eryone Matte Imported @Bambu Lab P1P 0.4 nozzle",
        ])

    def test_unknown_setting_id_raises(self):
        self._scaffold()
        with self.assertRaises(profiles.ProfileNotFoundError):
            profiles.export_user_filament("nonexistent", shape="thin")

    def test_vendor_setting_id_rejected_as_not_user(self):
        self._scaffold()
        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles.export_user_filament("GFSA01_02", shape="thin")
        self.assertIn("not a user filament", str(ctx.exception))

    def test_flattened_chain_failure_raises_unresolved(self):
        self._scaffold()
        # Break the parent: rewrite the user profile's inherits to a
        # name that won't resolve.
        user_key = profiles._profile_key("User", "Eryone Matte Imported")
        profiles._raw_profiles[user_key]["inherits"] = "Nonexistent Parent"
        profiles._resolved_cache.clear()
        with self.assertRaises(profiles.UnresolvedChainError):
            profiles.export_user_filament("Eryone Matte Imported", shape="flattened")

    def test_thin_unaffected_by_chain_failure(self):
        self._scaffold()
        user_key = profiles._profile_key("User", "Eryone Matte Imported")
        profiles._raw_profiles[user_key]["inherits"] = "Nonexistent Parent"
        profiles._resolved_cache.clear()
        # Thin reads the raw saved file; no resolution happens.
        entries = profiles.export_user_filament("Eryone Matte Imported", shape="thin")
        self.assertEqual(len(entries), 1)

    def test_flattened_no_compatible_printers_raises_value_error(self):
        self._index(
            "User", "Lonely Filament", "filament",
            {
                "setting_id": "Lonely Filament",
                "instantiation": "true",
                "from": "User",
                "filament_id": "Plonely1",
                "compatible_printers": [],
            },
        )
        with self.assertRaises(ValueError) as ctx:
            profiles.export_user_filament("Lonely Filament", shape="flattened")
        self.assertIn("compatible_printers", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
