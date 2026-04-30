# Export user filaments for OrcaSlicer GUI import — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `flattened`/`thin` export of user filament profiles, exposed via two REST endpoints and a web-UI modal, so a user can re-import their custom filaments into the OrcaSlicer GUI as AMS-assignable profiles.

**Architecture:** Read-only over the existing in-memory profile index. New helpers in `app/profiles.py` resolve and reshape user filaments into the OrcaSlicer GUI's `user/<profile>/filament/base/` shape (flattened) or pass them through (thin). Two new endpoints in `app/main.py` package the output as JSON or zip. A new modal in `app/web/` drives the user-facing flow.

**Tech Stack:** FastAPI, Python 3 (`zipfile`, `json`), Tailwind+Alpine.js front-end (existing patterns), pytest with `fastapi.testclient.TestClient` (existing harness).

**Spec:** `docs/superpowers/specs/2026-04-30-export-flattened-user-filament-design.md`

**Key references in existing code (read these before starting):**

- `app/profiles.py:37` — `ProfileNotFoundError` (we add a subclass).
- `app/profiles.py:144` — `_logical_filament_name` (existing helper, we'll reuse the alias-stripping concept).
- `app/profiles.py:766` — `resolve_profile_by_name`.
- `app/profiles.py:854` — `_resolve_by_slug` (the user-only check piggybacks on the existing setting_id index).
- `app/profiles.py:881` — `get_profile`.
- `app/normalize.py:38` — `_DEFAULTS` set: the canonical per-filament-vector key set used by the slicer. We curate the export's per-variant key set against this.
- `app/main.py:447` — `import_filament_profile`: existing endpoint pattern (Request, JSONResponse for errors, `load_all_profiles()` after writes).
- `app/web/index.html:214-360` — existing import modal pattern we mirror.
- `tests/test_import_endpoints.py:15` — `_ProfileEndpointTestBase` test harness with isolated tmpdir + `TestClient`.
- `tests/_profile_test_helpers.py` — module-level state cleanup helper.

**Test command (Docker-only project):** `docker compose run --rm orcaslicer-cli pytest tests/<file>::<test> -v`

**Per-variant convention:** OrcaSlicer's "extruder variant" tracks hot-end SKUs (e.g. "Direct Drive Standard", "Direct Drive High Flow") for a single physical extruder. A printer's variant count comes from the resolved machine profile's `printer_extruder_variant` list length.

---

## Task 1: Bump API revision + add `UnresolvedChainError`

**Why first:** Smallest possible isolated change; verifies the worktree's test harness boots before any structural work. The exception subclass unblocks Task 6.

**Files:**
- Modify: `app/config.py`
- Modify: `app/profiles.py:37`
- Test: `tests/test_strict_resolution.py` (existing)

- [ ] **Step 1: Bump API_REVISION**

Edit `app/config.py:4`:

```python
API_REVISION = "15"
```

- [ ] **Step 2: Add the exception subclass**

Edit `app/profiles.py:37` — replace the existing single-line class with:

```python
class ProfileNotFoundError(Exception):
    pass


class UnresolvedChainError(ProfileNotFoundError):
    """Raised when a profile's inheritance chain cannot be resolved
    during a flatten/export operation. Subclasses ProfileNotFoundError
    so existing call sites that catch the parent class still work; new
    call sites can distinguish chain failures from "profile not found"
    via this subclass."""
    pass
```

- [ ] **Step 3: Verify existing tests still pass**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_strict_resolution.py -v`

Expected: PASS (no behavior change yet — `UnresolvedChainError` is unused so far).

- [ ] **Step 4: Commit**

```bash
git add app/config.py app/profiles.py
git commit -m "$(cat <<'EOF'
Bump API revision to 15 and add UnresolvedChainError

- Adds an `UnresolvedChainError(ProfileNotFoundError)` subclass so the upcoming export path can distinguish chain-resolution failures
  from plain "profile not found" without breaking callers that catch the parent class.
- Bumps `API_REVISION` to track the new export endpoints landing in this branch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Filename + alias helpers

**What:** Two private helpers that convert a profile's `name` into safe filename and alias forms. Used by the flatten path and (in tests) verified independently.

**Files:**
- Modify: `app/profiles.py` (append new helpers near `_logical_filament_name` at line 144)
- Test: `tests/test_export_helpers.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_export_helpers.py`:

```python
import unittest

from app import profiles


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expected to fail**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py -v`

Expected: FAIL with `AttributeError: module 'app.profiles' has no attribute '_safe_filename'`.

- [ ] **Step 3: Implement the helpers**

Append to `app/profiles.py` (after `_logical_filament_name` near line 148):

```python
def _filament_alias(name: str) -> str:
    """Return the alias portion of a filament name (substring before ' @').

    Used to strip per-printer suffixes from previously-exported filenames
    so re-exports are idempotent.
    """
    base, _, _ = name.partition(" @")
    return base.strip() if base else name.strip()


_SAFE_FILENAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def _safe_filename(name: str, *, fallback: str) -> str:
    """Convert a profile name into a safe `.json` filename.

    Rules:
    - Lowercase.
    - Replace any character not in [a-z0-9._-] with '_'.
    - Collapse runs of '_' to a single '_'.
    - Strip leading/trailing '_'.
    - If the result is empty, use `fallback` (also sanitized).
    - Append '.json'.
    """
    def _sanitize(value: str) -> str:
        out_chars = []
        for ch in value.lower():
            out_chars.append(ch if ch in _SAFE_FILENAME_ALLOWED else "_")
        # Collapse runs of '_' and trim.
        result_parts = "".join(out_chars).split("_")
        return "_".join(p for p in result_parts if p)

    base = _sanitize(name)
    if not base:
        base = _sanitize(fallback)
    if not base:
        base = "profile"
    return f"{base}.json"
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py -v`

Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_export_helpers.py
git commit -m "$(cat <<'EOF'
Add `_filament_alias` and `_safe_filename` helpers

- `_filament_alias` strips the ' @<printer>' suffix from a filament name so re-exports of an already-exported file land on the same alias
  rather than stacking suffixes.
- `_safe_filename` normalizes a name to a `.json` filename with [a-z0-9._-] only, falling back to a caller-supplied fallback (typically
  the setting_id) when the input sanitizes to empty.
- Both helpers underpin the upcoming export endpoints; verified in isolation with `tests/test_export_helpers.py`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Printer variant-count helper

**What:** Look up a printer's `printer_extruder_variant` list length via its resolved machine profile, with a graceful fallback to 1 when the printer is unknown.

**Files:**
- Modify: `app/profiles.py` (append a new helper near `_machine_names_for_slug` at line 836)
- Test: `tests/test_export_helpers.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_export_helpers.py`:

```python
from tests._profile_test_helpers import reset_profiles_state


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
```

- [ ] **Step 2: Run tests — expected to fail**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::PrinterVariantCountTests -v`

Expected: FAIL with `AttributeError: ... no attribute '_printer_variant_count'`.

- [ ] **Step 3: Implement the helper**

Append to `app/profiles.py` (after `_machine_names_for_slug`, before `_resolve_by_slug` at line 854):

```python
def _printer_variant_count(printer_name: str) -> int:
    """Return the number of extruder variants for a printer profile.

    Resolves the machine profile by name and reads its
    `printer_extruder_variant` list length. Returns 1 when the
    printer is unknown or the field is absent — degrades gracefully
    rather than failing the export.
    """
    profile_key = _select_profile_key_by_name(printer_name, category="machine")
    if profile_key is None:
        return 1
    try:
        resolved = resolve_profile_by_name(profile_key)
    except ProfileNotFoundError:
        return 1
    if not resolved:
        return 1
    variants = resolved.get("printer_extruder_variant")
    if isinstance(variants, list) and variants:
        return len(variants)
    return 1
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::PrinterVariantCountTests -v`

Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_export_helpers.py
git commit -m "$(cat <<'EOF'
Add `_printer_variant_count` helper for export-time list padding

- Resolves a machine profile by name and returns the length of `printer_extruder_variant` (the field that drives per-variant filament
  list lengths in the OrcaSlicer GUI's user filament library).
- Falls back to 1 when the printer is unknown, the chain fails, or the field is absent — keeps the export path robust against partial
  catalogs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Per-variant key set + padding helper

**What:** Curate the set of filament keys that get padded per extruder variant on export, and add a helper that applies the padding.

**Files:**
- Modify: `app/profiles.py` (append new constant + helper)
- Test: `tests/test_export_helpers.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_export_helpers.py`:

```python
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
```

- [ ] **Step 2: Run tests — expected to fail**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::PadPerVariantKeysTests -v`

Expected: FAIL with `AttributeError: ... no attribute '_pad_per_variant_keys'`.

- [ ] **Step 3: Implement the constant + helper**

Append to `app/profiles.py` (after `_printer_variant_count`):

```python
# Filament keys whose value is a per-extruder-variant list in OrcaSlicer's
# GUI user filament shape. Derived from `app/normalize.py`'s _DEFAULTS set
# (the canonical per-filament-vector set the slicer normalizes) plus the
# additional per-variant keys observed in working GUI-native exports
# (temperatures, retraction, fan, scarf seam, cooling, plate temps).
#
# This list is intentionally explicit rather than dynamic: per-variant
# detection from the resolved profile alone is unreliable because some
# length-1 list keys are NOT per-variant (e.g. compatible_printers,
# filament_settings_id).
_FILAMENT_PER_VARIANT_KEYS: frozenset[str] = frozenset({
    # From normalize._DEFAULTS (the slicer's per-filament-vector set):
    "activate_chamber_temp_control",
    "adaptive_pressure_advance",
    "adaptive_pressure_advance_bridges",
    "adaptive_pressure_advance_model",
    "adaptive_pressure_advance_overhangs",
    "default_filament_colour",
    "dont_slow_down_outer_wall",
    "enable_overhang_bridge_fan",
    "enable_pressure_advance",
    "filament_colour",
    "filament_cooling_final_speed",
    "filament_cooling_initial_speed",
    "filament_cooling_moves",
    "filament_extruder_variant",
    "filament_ironing_flow",
    "filament_ironing_inset",
    "filament_ironing_spacing",
    "filament_ironing_speed",
    "filament_loading_speed",
    "filament_loading_speed_start",
    "filament_map",
    "filament_multitool_ramming",
    "filament_multitool_ramming_flow",
    "filament_multitool_ramming_volume",
    "filament_notes",
    "filament_ramming_parameters",
    "filament_shrinkage_compensation_z",
    "filament_stamping_distance",
    "filament_stamping_loading_speed",
    "filament_toolchange_delay",
    "filament_unloading_speed",
    "filament_unloading_speed_start",
    "idle_temperature",
    "internal_bridge_fan_speed",
    "ironing_fan_speed",
    "pressure_advance",
    "support_material_interface_fan_speed",
    "textured_cool_plate_temp",
    "textured_cool_plate_temp_initial_layer",
    # Additional per-variant keys observed in working GUI-native exports:
    "nozzle_temperature",
    "nozzle_temperature_initial_layer",
    "filament_max_volumetric_speed",
    "filament_flow_ratio",
    "filament_flush_temp",
    "filament_flush_volumetric_speed",
    "filament_long_retractions_when_cut",
    "filament_retract_before_wipe",
    "filament_retract_lift_above",
    "filament_retract_lift_below",
    "filament_retract_lift_enforce",
    "filament_retract_restart_extra",
    "filament_retract_when_changing_layer",
    "filament_retraction_distances_when_cut",
    "filament_retraction_length",
    "filament_retraction_minimum_travel",
    "filament_retraction_speed",
    "filament_deretraction_speed",
    "filament_wipe",
    "filament_wipe_distance",
    "filament_z_hop",
    "filament_z_hop_types",
    "long_retractions_when_ec",
    "retraction_distances_when_ec",
    "filament_adaptive_volumetric_speed",
    "volumetric_speed_coefficients",
})


def _pad_per_variant_keys(
    profile: dict[str, Any],
    *,
    variant_count: int,
    variant_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Pad every per-variant list key in `profile` to `variant_count`.

    For each key in `_FILAMENT_PER_VARIANT_KEYS` whose current value is
    a list shorter than `variant_count`, extend it by repeating the
    last value (or replicating the only value if length 1). If the
    list already meets or exceeds the target length, leave it.

    `filament_extruder_variant` is special-cased: its slot values are
    variant *labels* (e.g. "Direct Drive Standard"), so when
    `variant_labels` is provided and the key needs padding, the
    label list is substituted whole rather than replicated.

    Mutates `profile` in place and returns it.
    """
    if variant_count <= 1:
        return profile

    for key in _FILAMENT_PER_VARIANT_KEYS:
        value = profile.get(key)
        if not isinstance(value, list):
            continue
        if len(value) >= variant_count:
            continue
        if key == "filament_extruder_variant" and variant_labels is not None:
            profile[key] = list(variant_labels[:variant_count])
            continue
        # Pad by repeating the last value to reach variant_count.
        last = value[-1] if value else ""
        padded = list(value) + [last] * (variant_count - len(value))
        profile[key] = padded
    return profile
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::PadPerVariantKeysTests -v`

Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_export_helpers.py
git commit -m "$(cat <<'EOF'
Add per-variant key set and padding helper for export

- Curates `_FILAMENT_PER_VARIANT_KEYS` from `normalize._DEFAULTS` plus the per-variant keys observed in working OrcaSlicer GUI-native
  user filament exports (temperatures, retraction, fan, plate temps, etc.). Explicit rather than dynamic so non-per-variant length-1
  keys like `compatible_printers` are not over-padded.
- `_pad_per_variant_keys` replicates the last existing value to extend per-variant list keys to a target length, leaving non-list and
  already-long-enough values untouched. Special-cases `filament_extruder_variant` to substitute the printer's variant labels rather
  than replicate the slot-0 label.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Per-printer flatten function

**What:** A helper that takes a resolved filament dict + a target printer name and produces the GUI-shaped flattened JSON for that one printer (single output entry of the eventual export).

**Files:**
- Modify: `app/profiles.py`
- Test: `tests/test_export_helpers.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_export_helpers.py`:

```python
class FlattenForPrinterTests(unittest.TestCase):
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
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {"printer_extruder_variant": ["Direct Drive Standard"]},
        )
        out = profiles._flatten_user_filament_for_printer(
            self._resolved_filament(), printer_name="Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertNotIn("type", out)
        self.assertNotIn("instantiation", out)
        self.assertNotIn("setting_id", out)
        self.assertNotIn("base_id", out)
        self.assertEqual(out["inherits"], "")

    def test_renames_to_alias_at_printer(self):
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {"printer_extruder_variant": ["Direct Drive Standard"]},
        )
        out = profiles._flatten_user_filament_for_printer(
            self._resolved_filament(), printer_name="Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(
            out["name"], "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(
            out["filament_settings_id"],
            ["Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle"],
        )

    def test_scopes_compatible_printers_to_one(self):
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {"printer_extruder_variant": ["Direct Drive Standard"]},
        )
        out = profiles._flatten_user_filament_for_printer(
            self._resolved_filament(), printer_name="Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(out["compatible_printers"], ["Bambu Lab A1 mini 0.4 nozzle"])

    def test_adds_empty_default_compat_fields(self):
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {"printer_extruder_variant": ["Direct Drive Standard"]},
        )
        out = profiles._flatten_user_filament_for_printer(
            self._resolved_filament(), printer_name="Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(out["compatible_printers_condition"], "")
        self.assertEqual(out["compatible_prints"], [])
        self.assertEqual(out["compatible_prints_condition"], "")

    def test_pads_per_variant_keys_against_two_variant_printer(self):
        self._index_machine(
            "Bambu Lab P1P 0.4 nozzle",
            {
                "printer_extruder_variant": [
                    "Direct Drive Standard", "Direct Drive High Flow",
                ],
            },
        )
        out = profiles._flatten_user_filament_for_printer(
            self._resolved_filament(), printer_name="Bambu Lab P1P 0.4 nozzle",
        )
        self.assertEqual(out["nozzle_temperature"], ["210", "210"])
        self.assertEqual(
            out["filament_extruder_variant"],
            ["Direct Drive Standard", "Direct Drive High Flow"],
        )

    def test_idempotent_re_export(self):
        # Already has @<old printer> in name — should not stack suffixes.
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {"printer_extruder_variant": ["Direct Drive Standard"]},
        )
        resolved = self._resolved_filament()
        resolved["name"] = "Eryone Matte Imported @Some Other Printer"
        out = profiles._flatten_user_filament_for_printer(
            resolved, printer_name="Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(
            out["name"], "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
        )

    def test_does_not_mutate_input(self):
        self._index_machine(
            "Bambu Lab A1 mini 0.4 nozzle",
            {"printer_extruder_variant": ["Direct Drive Standard"]},
        )
        resolved = self._resolved_filament()
        snapshot = json.loads(json.dumps(resolved))
        profiles._flatten_user_filament_for_printer(
            resolved, printer_name="Bambu Lab A1 mini 0.4 nozzle",
        )
        self.assertEqual(resolved, snapshot)
```

Add `import json` to the top of `tests/test_export_helpers.py` if it's not already there.

- [ ] **Step 2: Run tests — expected to fail**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::FlattenForPrinterTests -v`

Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement the helper**

Append to `app/profiles.py` (after `_pad_per_variant_keys`):

```python
# Vendor-profile marker keys stripped on flatten — they identify the
# input as a vendor preset, which OrcaSlicer's GUI uses to gate user
# editing and AMS assignment. User filaments do not carry these.
_FLATTEN_STRIP_KEYS = frozenset({
    "inherits", "base_id", "type", "instantiation", "setting_id",
})


def _flatten_user_filament_for_printer(
    resolved: dict[str, Any],
    *,
    printer_name: str,
) -> dict[str, Any]:
    """Reshape a resolved user filament dict for one target printer.

    Returns a new dict (does not mutate `resolved`) with the
    OrcaSlicer-GUI-shaped flattened layout described in the spec.
    The output is suitable for writing as a `.json` file under
    `~/Library/Application Support/OrcaSlicer/user/<profile>/filament/base/`.
    """
    out = {k: v for k, v in resolved.items() if k not in _FLATTEN_STRIP_KEYS}
    out["inherits"] = ""

    alias = _filament_alias(str(resolved.get("name", "")))
    new_name = f"{alias} @{printer_name}"
    out["name"] = new_name
    out["filament_settings_id"] = [new_name]

    out["compatible_printers"] = [printer_name]
    out.setdefault("compatible_printers_condition", "")
    out.setdefault("compatible_prints", [])
    out.setdefault("compatible_prints_condition", "")

    # Look up the printer's variant labels and count via the indexed
    # machine profile. Falls back to single-variant when unknown.
    variant_count = _printer_variant_count(printer_name)
    variant_labels = None
    if variant_count > 1:
        machine_key = _select_profile_key_by_name(printer_name, category="machine")
        if machine_key is not None:
            try:
                machine_resolved = resolve_profile_by_name(machine_key)
            except ProfileNotFoundError:
                machine_resolved = None
            if machine_resolved:
                labels = machine_resolved.get("printer_extruder_variant")
                if isinstance(labels, list):
                    variant_labels = list(labels)

    out = _pad_per_variant_keys(
        out, variant_count=variant_count, variant_labels=variant_labels,
    )
    return out
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::FlattenForPrinterTests -v`

Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_export_helpers.py
git commit -m "$(cat <<'EOF'
Add per-printer flatten function for user filament export

- `_flatten_user_filament_for_printer` reshapes a resolved user filament dict into the OrcaSlicer GUI's `user/<profile>/filament/base/`
  shape for one target printer: strips vendor markers, sets `inherits=""`, rewrites name/filament_settings_id to `<alias> @<printer>`,
  scopes `compatible_printers`, and pads per-variant list keys via `_pad_per_variant_keys`.
- Idempotent on re-export — `_filament_alias` strips any pre-existing `@<printer>` suffix before re-applying.
- Does not mutate the input dict; callers can reuse the same resolved view across multiple printers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `export_user_filament` public orchestrator

**What:** The public helper called by both endpoints. Validates user-only scope, dispatches `thin` vs `flattened`, and for `flattened` expands across compatible printers.

**Files:**
- Modify: `app/profiles.py`
- Test: `tests/test_export_helpers.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_export_helpers.py`:

```python
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

    def test_flattened_expands_per_compatible_printer(self):
        self._scaffold()
        entries = profiles.export_user_filament("Eryone Matte Imported", shape="flattened")
        # 2 compatible printers → 2 entries.
        self.assertEqual(len(entries), 2)
        names = sorted(e[1]["name"] for e in entries)
        self.assertEqual(names, [
            "Eryone Matte Imported @Bambu Lab A1 mini 0.4 nozzle",
            "Eryone Matte Imported @Bambu Lab A1 mini 0.6 nozzle",
        ])
        # Each entry has the GUI-shaped structure.
        for filename, data in entries:
            self.assertEqual(data["inherits"], "")
            self.assertNotIn("setting_id", data)
            self.assertEqual(len(data["compatible_printers"]), 1)

    def test_flattened_filename_includes_printer(self):
        self._scaffold()
        entries = profiles.export_user_filament("Eryone Matte Imported", shape="flattened")
        filenames = sorted(e[0] for e in entries)
        self.assertEqual(filenames, [
            "eryone_matte_imported_bambu_lab_a1_mini_0.4_nozzle.json",
            "eryone_matte_imported_bambu_lab_a1_mini_0.6_nozzle.json",
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
```

- [ ] **Step 2: Run tests — expected to fail**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::ExportUserFilamentTests -v`

Expected: FAIL with `AttributeError: ... no attribute 'export_user_filament'`.

- [ ] **Step 3: Implement the orchestrator**

Append to `app/profiles.py`:

```python
def export_user_filament(
    setting_id: str,
    *,
    shape: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Export a user filament as one or more (filename, dict) entries.

    `shape` must be 'thin' or 'flattened'.

    `thin` → one entry containing the saved file as-is (with `inherits`
    preserved). Does not resolve the chain.

    `flattened` → one entry per compatible printer of the resolved
    filament. Each entry is the OrcaSlicer-GUI-shaped flattened JSON
    for that printer, suitable for AMS assignment.

    Raises:
    - `ProfileNotFoundError` if `setting_id` is unknown or does not
      identify a user-vendor filament.
    - `UnresolvedChainError` if `shape='flattened'` and the inheritance
      chain cannot be resolved.
    - `ValueError` if `shape='flattened'` and the resolved filament
      has empty `compatible_printers`.
    """
    if shape not in ("thin", "flattened"):
        raise ValueError(f"Invalid shape: {shape!r} (expected 'thin' or 'flattened')")

    keys = _setting_id_index.get(setting_id)
    if not keys:
        raise ProfileNotFoundError(
            f"filament profile with id '{setting_id}' not found"
        )

    profile_key = None
    for k in keys:
        if _type_map.get(k) == "filament" and _vendor_map.get(k) == "User":
            profile_key = k
            break
    if profile_key is None:
        raise ProfileNotFoundError(
            f"filament '{setting_id}' is not a user filament"
        )

    raw = _raw_profiles.get(profile_key, {})
    name = str(raw.get("name", setting_id))

    if shape == "thin":
        filename = _safe_filename(name, fallback=setting_id)
        return [(filename, dict(raw))]

    # shape == "flattened"
    try:
        resolved = resolve_profile_by_name(profile_key)
    except ProfileNotFoundError as e:
        raise UnresolvedChainError(str(e)) from e
    if resolved is None:
        raise UnresolvedChainError(
            f"filament '{setting_id}' resolved to None"
        )

    printers = resolved.get("compatible_printers")
    if not isinstance(printers, list) or not printers:
        raise ValueError(
            f"filament '{setting_id}' has empty compatible_printers; "
            f"cannot flatten for GUI export"
        )

    alias = _filament_alias(name)
    entries: list[tuple[str, dict[str, Any]]] = []
    for printer_name in printers:
        flat = _flatten_user_filament_for_printer(resolved, printer_name=printer_name)
        full_name = flat["name"]  # already "<alias> @<printer>"
        filename = _safe_filename(full_name, fallback=alias or setting_id)
        entries.append((filename, flat))
    return entries
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py::ExportUserFilamentTests -v`

Expected: PASS (8 tests).

- [ ] **Step 5: Run the full export_helpers test file**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_helpers.py -v`

Expected: PASS (~36 tests across all classes).

- [ ] **Step 6: Commit**

```bash
git add app/profiles.py tests/test_export_helpers.py
git commit -m "$(cat <<'EOF'
Add `export_user_filament` orchestrator

- Public entry point that returns one (filename, dict) entry for thin export and one entry per compatible printer for flattened.
- Validates user-vendor scope (rejects vendor profiles with `not a user filament`), maps chain-resolution failures to
  `UnresolvedChainError`, and raises `ValueError` when a flattened export has no compatible printers to scope to.
- Thin reads `_raw_profiles` directly so it stays useful even when the inheritance chain is broken; flattened goes through the
  resolver and the per-printer flatten helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `GET /profiles/filaments/{setting_id}/export` endpoint

**What:** HTTP surface for the single-filament export. Returns JSON for `thin`, zip for `flattened`.

**Files:**
- Modify: `app/main.py` (add new endpoint near `get_filament_detail` at line 575)
- Test: `tests/test_export_endpoints.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_export_endpoints.py`:

```python
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

    def test_flattened_returns_zip_with_one_entry_per_printer(self):
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expected to fail**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_endpoints.py::GetExportEndpointTests -v`

Expected: FAIL — endpoint not registered, returns 404 from FastAPI's default router.

- [ ] **Step 3: Implement the endpoint**

Add to `app/main.py` (after `get_filament_detail` at line 581, before `_collect_process_overrides`):

```python
@app.get(
    "/profiles/filaments/{setting_id}/export",
    tags=["Profiles"],
    responses={
        200: {
            "content": {
                "application/json": {},
                "application/zip": {},
            },
            "description": "User filament export — JSON for thin, zip for flattened.",
        },
        400: {"description": "Invalid shape parameter."},
        404: {"description": "User filament not found."},
        500: {"description": "Inheritance chain could not be resolved."},
    },
)
async def export_filament_profile(setting_id: str, shape: str = "flattened"):
    """Export a user filament for OrcaSlicer GUI import.

    `shape=flattened` (default) returns a zip with one JSON entry per
    compatible printer, each shaped for the GUI's
    `user/<profile>/filament/base/` directory and AMS-assignable on
    import.

    `shape=thin` returns the saved file as-is (with `inherits`
    preserved). The recipient OrcaSlicer install must already have the
    parent profile, and the imported result is not AMS-assignable.
    """
    if shape not in ("thin", "flattened"):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid shape '{shape}'; expected 'thin' or 'flattened'."},
        )

    try:
        entries = profiles.export_user_filament(setting_id, shape=shape)
    except profiles.UnresolvedChainError as e:
        logger.warning("Export of '%s' failed: %s", setting_id, e)
        return JSONResponse(status_code=500, content={"error": str(e)})
    except profiles.ProfileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    if shape == "thin":
        filename, data = entries[0]
        return Response(
            content=json.dumps(data, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    # flattened — package as zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, data in entries:
            zf.writestr(filename, json.dumps(data, indent=2))
    alias = profiles._filament_alias(str(entries[0][1].get("name", setting_id)))
    zip_filename = profiles._safe_filename(
        alias or setting_id, fallback=setting_id,
    ).replace(".json", ".zip")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
        },
    )
```

Add the necessary imports at the top of `app/main.py` (preserving alphabetical order if present):

```python
import io
import zipfile
```

(`json` and `Response` should already be imported; if `Response` isn't, add `from fastapi import Response`.)

- [ ] **Step 4: Run tests — expect PASS**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_endpoints.py::GetExportEndpointTests -v`

Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_export_endpoints.py
git commit -m "$(cat <<'EOF'
Add GET /profiles/filaments/{setting_id}/export endpoint

- `shape=thin` returns the saved user-filament JSON unchanged with `Content-Disposition: attachment`. `shape=flattened` (default)
  returns a zip with one entry per compatible printer, each shaped for the OrcaSlicer GUI's filament/base/ directory and
  AMS-assignable on import.
- Maps `ProfileNotFoundError` → 404, `UnresolvedChainError` → 500, `ValueError` (no compatible printers) → 400. Vendor
  (non-user) setting_ids return 404 by design — only user filaments are exportable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `POST /profiles/filaments/export-batch` endpoint

**What:** Batch endpoint that accepts a list of `setting_ids` and a `shape`, returns one zip with all successfully-exported entries plus an `X-Export-Skipped` header for failures.

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_export_endpoints.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_export_endpoints.py`:

```python
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
        # 2 filaments × 2 printers each = 4 entries.
        self.assertEqual(len(zf.namelist()), 4)

    def test_default_shape_is_flattened(self):
        r = self.client.post(
            "/profiles/filaments/export-batch",
            json={"setting_ids": ["Eryone Matte Imported"]},
        )
        self.assertEqual(r.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(len(zf.namelist()), 2)  # 2 compatible printers

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
        # Break only Eryone's chain.
        user_key = profiles._profile_key("User", "Eryone Matte Imported")
        profiles._raw_profiles[user_key]["inherits"] = "Nonexistent Parent"
        profiles._resolved_cache.clear()
        self._add_second_user_filament()
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
        # Other PLA × 2 printers = 2 entries; Eryone skipped.
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
```

- [ ] **Step 2: Run tests — expected to fail**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_endpoints.py::PostBatchExportTests -v`

Expected: FAIL — endpoint not registered.

- [ ] **Step 3: Implement the endpoint**

Add to `app/main.py` (after `export_filament_profile`):

```python
@app.post(
    "/profiles/filaments/export-batch",
    tags=["Profiles"],
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": (
                "Zip of exported user filaments. The `X-Export-Skipped` "
                "header (when present) is a JSON-encoded object mapping "
                "skipped setting_ids to reasons "
                "(`not_found`, `unresolved_chain`, `no_compatible_printers`)."
            ),
        },
        400: {"description": "Invalid request body or shape."},
    },
)
async def export_filaments_batch(request: Request):
    """Batch-export a list of user filaments as a zip.

    Request body: `{"setting_ids": [...], "shape": "thin" | "flattened"}`.
    `shape` defaults to `"flattened"`.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "Body must be a JSON object."})

    setting_ids = body.get("setting_ids")
    shape = body.get("shape", "flattened")

    if not isinstance(setting_ids, list) or not setting_ids:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing or empty 'setting_ids' (must be a non-empty list)."},
        )

    if shape not in ("thin", "flattened"):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid shape '{shape}'; expected 'thin' or 'flattened'."},
        )

    skipped: dict[str, str] = {}
    success_entries: list[tuple[str, dict]] = []

    for sid in setting_ids:
        if not isinstance(sid, str):
            skipped[str(sid)] = "not_found"
            continue
        try:
            entries = profiles.export_user_filament(sid, shape=shape)
        except profiles.UnresolvedChainError:
            skipped[sid] = "unresolved_chain"
            continue
        except profiles.ProfileNotFoundError:
            skipped[sid] = "not_found"
            continue
        except ValueError:
            skipped[sid] = "no_compatible_printers"
            continue
        success_entries.extend(entries)

    # Deduplicate filenames within the zip with `-2`, `-3`, ... suffixes.
    seen_names: dict[str, int] = {}
    deduped: list[tuple[str, dict]] = []
    for filename, data in success_entries:
        if filename not in seen_names:
            seen_names[filename] = 1
            deduped.append((filename, data))
            continue
        seen_names[filename] += 1
        stem, dot, ext = filename.rpartition(".")
        new_name = f"{stem}-{seen_names[filename]}.{ext}" if dot else f"{filename}-{seen_names[filename]}"
        deduped.append((new_name, data))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, data in deduped:
            zf.writestr(filename, json.dumps(data, indent=2))

    headers = {
        "Content-Disposition": (
            f'attachment; filename="user-filaments-'
            f'{datetime.utcnow().strftime("%Y%m%d-%H%M%S")}.zip"'
        ),
    }
    if skipped:
        headers["X-Export-Skipped"] = json.dumps(skipped)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers=headers,
    )
```

Ensure these imports are at the top of `app/main.py`:

```python
from datetime import datetime
```

(`io`, `zipfile`, `json`, `Request`, `Response`, `JSONResponse` should already be there.)

- [ ] **Step 4: Run tests — expect PASS**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_endpoints.py::PostBatchExportTests -v`

Expected: PASS (11 tests).

- [ ] **Step 5: Run the full export endpoints test file**

Run: `docker compose run --rm orcaslicer-cli pytest tests/test_export_endpoints.py -v`

Expected: PASS (~19 tests).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_export_endpoints.py
git commit -m "$(cat <<'EOF'
Add POST /profiles/filaments/export-batch endpoint

- Accepts `{"setting_ids": [...], "shape": "thin" | "flattened"}` and returns a zip with one entry per (filament × printer) for
  flattened or one per filament for thin.
- Per-filament failures land in `X-Export-Skipped` (JSON-encoded `{setting_id: reason}` map) with reasons `not_found`,
  `unresolved_chain`, `no_compatible_printers`. The header is omitted entirely when nothing was skipped.
- Filename collisions inside the zip get `-2`, `-3`, ... suffixes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Web UI — Export modal

**What:** Add the user-facing modal to `app/web/index.html` and `app/web/app.js`. No automated UI tests in this project — verification is manual smoke against the running container.

**Files:**
- Modify: `app/web/index.html` (around the user-filaments section near line 214 where the import modal lives)
- Modify: `app/web/app.js` (add an `exporter` Alpine.js scope)

- [ ] **Step 1: Read the existing import-modal pattern**

Read `app/web/index.html:214-360` and `app/web/app.js` (find the `importer` Alpine scope) to understand the modal/list/loading/error patterns the project uses. The new modal should mirror that structure for visual consistency.

- [ ] **Step 2: Add the "Export…" button**

In `app/web/index.html`, find the filaments section header (look for the existing "Import" button near the filaments list). Add a sibling button:

```html
<button
  class="rounded-md border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm hover:bg-slate-700 disabled:opacity-50"
  :disabled="userFilaments.length === 0"
  @click="exporter.open(userFilaments)"
  x-show="!loading">
  Export...
</button>
```

Where `userFilaments` is the existing filtered list of `vendor === 'User'` filaments already rendered in the page (verify the exact computed-property name in `app.js`).

- [ ] **Step 3: Add the export modal markup**

In `app/web/index.html`, add a new modal block near the existing import modal (mirror the same wrapper/overlay/sizing classes):

```html
<!-- Export modal -->
<div x-show="exporter.open"
     x-cloak
     class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
  <div class="w-full max-w-xl rounded-lg border border-slate-700 bg-slate-900 p-6 shadow-xl">
    <h2 class="text-lg font-semibold">Export user filaments</h2>
    <p class="mt-1 text-sm text-slate-400">
      Download your custom filaments to import into OrcaSlicer's GUI.
    </p>

    <!-- Shape picker -->
    <div class="mt-4 rounded-md border border-slate-700 bg-slate-800/40 p-3">
      <label class="flex items-start gap-2 text-sm cursor-pointer">
        <input type="radio" x-model="exporter.shape" value="flattened" class="mt-0.5"/>
        <span>
          <span class="font-medium">Flattened (recommended)</span>
          <span class="block text-xs text-slate-400">
            Self-contained profile. AMS-assignable when imported into OrcaSlicer.
            Each filament is exported once per compatible printer.
          </span>
        </span>
      </label>
      <label class="mt-2 flex items-start gap-2 text-sm cursor-pointer">
        <input type="radio" x-model="exporter.shape" value="thin" class="mt-0.5"/>
        <span>
          <span class="font-medium">Thin</span>
          <span class="block text-xs text-slate-400">
            Preserves the inheritance link. Smaller, one file per filament.
            The receiving OrcaSlicer install must already have the parent
            profile, and the imported result is not AMS-assignable.
          </span>
        </span>
      </label>
    </div>

    <!-- Selection list -->
    <div class="mt-4 max-h-64 overflow-y-auto rounded-md border border-slate-700">
      <div class="flex items-center justify-between border-b border-slate-700 bg-slate-800/40 px-3 py-2 text-xs text-slate-300">
        <button class="hover:text-white" @click="exporter.selectAll()">Select all</button>
        <span x-text="exporter.selectedCount + ' of ' + exporter.items.length + ' selected'"></span>
        <button class="hover:text-white" @click="exporter.deselectAll()">Deselect all</button>
      </div>
      <ul class="divide-y divide-slate-800">
        <template x-for="item in exporter.items" :key="item.setting_id">
          <li class="flex items-start gap-2 px-3 py-2">
            <input type="checkbox" :value="item.setting_id"
                   x-model="exporter.selected" class="mt-1"/>
            <div class="text-sm">
              <p x-text="item.name"></p>
              <p class="text-xs text-slate-500"
                 x-text="'Compatible with ' + (item.compatible_printers?.length || 0) + ' printer(s)'"></p>
            </div>
          </li>
        </template>
      </ul>
    </div>

    <!-- Result/skip notice -->
    <div x-show="exporter.skippedNotice" class="mt-3 rounded-md border border-amber-700/60 bg-amber-900/30 px-3 py-2 text-sm text-amber-200"
         x-text="exporter.skippedNotice"></div>

    <!-- Footer -->
    <div class="mt-5 flex justify-end gap-2">
      <button class="rounded-md border border-slate-700 px-3 py-1.5 text-sm hover:bg-slate-800"
              @click="exporter.close()">Cancel</button>
      <button class="rounded-md border border-emerald-600 bg-emerald-600/90 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
              :disabled="exporter.selectedCount === 0 || exporter.busy"
              @click="exporter.download()"
              x-text="exporter.downloadLabel"></button>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Add the `exporter` Alpine scope to `app/web/app.js`**

Find the main Alpine `data` object (the one that contains `importer`, `userFilaments`, etc.) and add an `exporter` field next to `importer`. Reference the existing `importer` scope for naming conventions:

```javascript
exporter: {
  open: false,
  shape: "flattened",
  items: [],
  selected: [],
  busy: false,
  skippedNotice: "",

  get selectedCount() { return this.selected.length; },

  get downloadLabel() {
    if (this.shape === "thin" && this.selected.length === 1) return "Download JSON";
    return "Download zip";
  },

  open(filaments) {
    this.items = filaments.slice();
    this.selected = filaments.map(f => f.setting_id);
    this.shape = "flattened";
    this.skippedNotice = "";
    this.open = true;
  },

  close() { this.open = false; this.busy = false; },

  selectAll() { this.selected = this.items.map(f => f.setting_id); },
  deselectAll() { this.selected = []; },

  async download() {
    if (this.selected.length === 0 || this.busy) return;
    this.busy = true;
    this.skippedNotice = "";
    try {
      let response, suggestedFilename;
      if (this.shape === "thin" && this.selected.length === 1) {
        const sid = this.selected[0];
        response = await fetch(
          `/profiles/filaments/${encodeURIComponent(sid)}/export?shape=thin`,
        );
        suggestedFilename = this._filenameFromResponse(response, `${sid}.json`);
      } else {
        response = await fetch("/profiles/filaments/export-batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            setting_ids: this.selected,
            shape: this.shape,
          }),
        });
        suggestedFilename = this._filenameFromResponse(response, "user-filaments.zip");
      }

      if (!response.ok) {
        const err = await response.json().catch(() => ({ error: response.statusText }));
        this.skippedNotice = `Export failed: ${err.error || response.statusText}`;
        return;
      }

      const skippedHeader = response.headers.get("X-Export-Skipped");
      if (skippedHeader) {
        const skipped = JSON.parse(skippedHeader);
        const names = Object.keys(skipped);
        if (names.length > 0) {
          this.skippedNotice = `Skipped ${names.length}: ${names.join(", ")}`;
        }
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = suggestedFilename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      this.skippedNotice = `Export failed: ${e.message}`;
    } finally {
      this.busy = false;
    }
  },

  _filenameFromResponse(response, fallback) {
    const cd = response.headers.get("Content-Disposition") || "";
    const match = cd.match(/filename="?([^"]+)"?/);
    return match ? match[1] : fallback;
  },
},
```

- [ ] **Step 5: Manual smoke test**

```bash
docker compose up --build -d
# Wait for the container to come up.
curl -sf http://localhost:8000/health
# Open http://localhost:8000/ in a browser. Confirm:
# 1. The "Export..." button appears next to the user filaments list.
# 2. Clicking it opens the modal with all user filaments pre-selected,
#    "Flattened" radio pre-selected, and the per-row compatible-printer
#    count visible.
# 3. With one filament selected and shape=Thin, "Download JSON" delivers
#    one JSON file.
# 4. With one filament selected and shape=Flattened, "Download zip"
#    delivers a zip with N entries (one per compatible printer).
# 5. With multiple filaments selected, "Download zip" delivers a zip
#    with all of their entries (expanded per printer for flattened).
docker compose down
```

- [ ] **Step 6: Commit**

```bash
git add app/web/index.html app/web/app.js
git commit -m "$(cat <<'EOF'
Add Export… modal to the user-filaments web UI

- New "Export..." button next to the user filaments list opens a modal that lists every user filament (all pre-selected) with a
  shape picker (Flattened recommended; Thin alternative).
- Selecting one filament with Thin downloads a single JSON; everything else downloads a zip via the export-batch endpoint.
- Per-row "Compatible with N printer(s)" caption makes the per-printer expansion visible before download. Skip notices from
  `X-Export-Skipped` are surfaced inline after the download completes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: End-to-end smoke test

**What:** Verify the working flow against a real OrcaSlicer GUI install.

**Files:** None modified. This is a verification step that closes the open question from the spec.

- [ ] **Step 1: Start the container**

```bash
docker compose up --build -d
curl -sf http://localhost:8000/health
```

- [ ] **Step 2: Confirm a user filament is loaded**

```bash
curl -s http://localhost:8000/profiles/filaments | jq '[.[] | select(.vendor == "User")] | length'
```

Expected: at least 1 user filament in `/data`.

- [ ] **Step 3: Export one user filament flattened**

Pick a real user filament setting_id from step 2 (replace `<SID>` below), then:

```bash
curl -s -o /tmp/export.zip \
  "http://localhost:8000/profiles/filaments/<SID>/export?shape=flattened"
unzip -l /tmp/export.zip
unzip -o /tmp/export.zip -d /tmp/orca-export-test
```

- [ ] **Step 4: Inspect a flattened entry**

```bash
ls /tmp/orca-export-test/
# Open one entry; verify it has: name with @<printer>, inherits="",
# no `type`/`instantiation`/`setting_id`, non-empty filament_id,
# compatible_printers length 1.
python3 -c "import json; d=json.load(open('/tmp/orca-export-test/<one-of-the-entries>.json')); \
  print('name:', d['name']); print('inherits:', repr(d['inherits'])); \
  print('filament_id:', d['filament_id']); \
  print('compatible_printers:', d['compatible_printers']); \
  print('vendor markers:', [k for k in ('type','instantiation','setting_id') if k in d])"
```

Expected:
- `inherits: ''`
- `filament_id` non-empty
- `compatible_printers` is a length-1 list
- vendor markers list is empty: `[]`

- [ ] **Step 5: Import into OrcaSlicer GUI and verify AMS-assignment**

1. Quit OrcaSlicer if running.
2. Copy one flattened entry into `~/Library/Application Support/OrcaSlicer/user/default/filament/base/` (filename matches `name`).
3. Start OrcaSlicer.
4. Open Filament Settings (gear icon) — confirm the imported filament appears in the user filaments list and does not freeze the manager.
5. Open the AMS slot mapping picker for the matching printer — confirm the imported filament appears as AMS-assignable.

If steps 4 or 5 fail, capture the difference vs a working GUI-native file (`diff -u` against `~/Library/Application Support/OrcaSlicer/user/default/filament/base/<a-working-one>.json`) and adjust the algorithm. The flatten algorithm has been validated end-to-end in the spec, so failures here likely indicate edge cases (e.g. a printer not in the bundled OrcaSlicer 2.3.2 catalog).

- [ ] **Step 6: Stop the container**

```bash
docker compose down
```

- [ ] **Step 7: No-op commit**

No code change; this task is verification only. If everything passes, do nothing. If you found and fixed a real issue, commit that fix as a separate task with its own TDD pass.

---

## Self-review notes

(Filled in by the plan author after the body was complete.)

**Spec coverage check:**

- Behavior § Export shapes (flattened) — covered by Tasks 4, 5, 6.
- Behavior § Export shapes (thin) — covered by Task 6.
- Behavior § Per-printer expansion — covered by Task 6 + Task 7 tests.
- Behavior § GET /…/export — Task 7.
- Behavior § POST /…/export-batch — Task 8.
- Behavior § Web UI — Task 9.
- Component design § profiles.py helpers — Tasks 2-6.
- Component design § main.py endpoints — Tasks 7, 8.
- Component design § config.py revision bump — Task 1.
- Tests § Single endpoint cases — Task 7.
- Tests § Batch endpoint cases — Task 8.
- Tests § Algorithm-level — Tasks 2-6 (sub-test classes in `test_export_helpers.py`).
- Risks § per-variant key set drift — mitigated by explicit curated set in Task 4 + Task 10 smoke test.
- Risks § variant-count lookup miss — Task 3 covers the fallback to 1.

**Type/name consistency check:** `export_user_filament` returns `list[tuple[str, dict[str, Any]]]` everywhere. `_flatten_user_filament_for_printer` takes a `printer_name=` kwarg consistently. `UnresolvedChainError` is the subclass name across all references. `_FILAMENT_PER_VARIANT_KEYS` is the constant name; `_pad_per_variant_keys` is the helper. `shape` literal values are exactly `"thin"` and `"flattened"` everywhere. `X-Export-Skipped` reasons are exactly `"not_found"`, `"unresolved_chain"`, `"no_compatible_printers"`.
