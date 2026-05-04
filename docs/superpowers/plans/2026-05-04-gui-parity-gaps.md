# Phase 6: GUI-parity gaps in `slice_mode.cpp` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the remaining points in `cpp/src/slice_mode.cpp` where our headless slice path diverges from OrcaSlicer's GUI. We landed two big GUI-faithful fixes today — `s_project_options` whitelist on `threemf_config` (commit just before this plan was written), and the cross-machine filament/process/plate-type resolver — but a careful audit of the `slice_mode.cpp` flow against `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp` flagged six remaining gaps. Each is a place where the GUI does *something* the binary doesn't, and that "something" can either crash the binary in production or silently produce wrong output for a 3MF the GUI handles cleanly.

**Why this matters:** the user is migrating production traffic to `/slice/v2`. Each gap below has been observed (1, 2) or is a latent risk (3-7) for a real 3MF the user might upload. The fixes are small individually; the value is closing them as a batch before the next class of mystery `exit -11` lands.

**Tech Stack:** C++17 (libslic3r in-tree), Python 3.12 (FastAPI orchestration). Builds via `scripts/build-and-ship.sh`.

**Where this plan lives, where it executes:** plan committed to `orcaslicer-cli/docs/superpowers/plans/`. All execution lives in this repo on `feat/orca-headless`.

**Repo conventions:**
- Each gap fix lands as its own commit so we can bisect.
- Cite the GUI source location (`vendor/OrcaSlicer/src/libslic3r/...`) in code comments — the rule "reuse GUI code, don't reimplement" means every divergence needs a paper trail to the upstream behaviour we're matching.
- Build/ship via `scripts/build-and-ship.sh`. C++ rebuilds are 25-30 min at `-j2`; the watchdog catches host swap-thrash hangs.

**Out of scope:**
- Reworking the slice flow into a single `full_fff_config` call. The current "build the configs ourselves, call `construct_full_config` directly" approach is intentional (we don't have a PresetBundle).
- Rewriting `apply_overrides_for_slot` into a proper per-slot indexed apply (gap 3 — that's a bigger change than this plan covers).

---

## Gaps, ordered by risk

### Gap 1 (high): printer-slot override re-applies extruder topology

`slice_mode.cpp` lines 377-380:

```cpp
printer_keys = apply_overrides_for_slot(
    final_cfg, threemf_config, fp->values.back(),
    /*exclude_filament_keys=*/false);
```

After `construct_full_config` produced a clean `final_cfg` from the printer + the *filtered* `project_config`, this re-reads the 3MF's `different_settings_to_system[printer_slot]` (a list of keys the user customized in the GUI's printer preset) and overlays them onto `final_cfg`. The list comes from the raw `threemf_config`. If the user customized any per-extruder vector field (`extruder_variant_list`, `printer_extruder_variant`, `printer_extruder_id`, `extruder_type`, `nozzle_volume_type`), those values bypass the `s_project_options` whitelist we just added and re-corrupt `final_cfg` — recreating the SIGSEGV class we fixed.

**Why this is risk-class 1:** the user already hit a SIGSEGV from this corruption pattern via the project-config path. The printer-slot path can recreate it for any 3MF with printer-level customizations.

**Fix:**

- [ ] **Define a printer-slot blocklist** mirroring the keys that should *only* come from the printer preset, never from a 3MF override. At minimum: `extruder_variant_list`, `printer_extruder_variant`, `printer_extruder_id`, `extruder_type`, `nozzle_volume_type`, `filament_extruder_variant`, `filament_self_index`, `extruder_ams_count`. Live with `s_project_options` near the top of `slice_mode.cpp` so the two whitelists/blocklists are read together.

- [ ] **Wire the blocklist** into `apply_overrides_for_slot` for the printer-slot call only. Either filter `fp->values.back()` (split on `;`, drop blocklisted keys, re-join) before passing in, or add an `excluded_keys` parameter to `apply_overrides_for_slot`. Latter is cleaner; the function already has the `exclude_filament_keys` shape.

- [ ] **Test:** capture a real 3MF with `different_settings_to_system[N+1]` containing a printer key, slice against a different machine, confirm no crash and the non-blocklisted printer keys still apply. Fixture for this can be hand-crafted from `_fixture/01` by editing `Metadata/project_settings.config` to add `extruder_variant_list` to the printer slot's diff list.

- [ ] **Commit:**
```
Block printer-extruder topology keys from 3MF printer-slot overrides
```

---

### Gap 2 (medium-high): per-filament-slot overrides go flat, not indexed

`slice_mode.cpp` lines 353-360 (the comment is the existing TODO):

```cpp
// Phase 1 limitation: overlays the keys flat onto final_cfg rather than
// into the per-slot vector index. For the single-customized-slot case
// this matches what PresetBundle does for filament_cfgs[0]; multi-slot
// per-key overlay is a Phase 4 follow-up.
```

When the GUI exports a 3MF with per-filament customizations (e.g. user bumped `nozzle_temperature` for slot 2 only), the binary's `apply_overrides_for_slot` writes the key flat onto `final_cfg`. After `construct_full_config` already built the per-filament vector at length N, our flat write puts the override at index 0 (or wherever `set_at` lands), not at the slot index. Slot 2 sees default temperature; slot 0 sees slot 2's override.

**Why this is risk-class 2 (silent wrong output, not crash):** for single-filament slices it's fine (slot 0 is the only slot). Multi-filament slices with per-slot user edits (rare but real) silently produce gcode with the wrong temperatures/speeds.

**Fix:**

- [ ] **Pivot the apply target:** instead of `apply_overrides_for_slot(final_cfg, ...)`, write a `apply_overrides_for_filament_slot(final_cfg, slot_index, threemf_config, key_list)` that, for each key in `key_list`, reads `threemf_config[key]` (a vector at slot N's value, since the 3MF stores per-filament keys as parallel vectors) and `set_at(slot_index)` on `final_cfg[key]`.

- [ ] **Reference implementation:** `vendor/OrcaSlicer/src/libslic3r/Preset.cpp:s_Preset_filament_options` defines the canonical per-filament keys. The GUI applies these per-slot inside `PresetBundle::load_config_file_config` and the per-key merge inside `construct_full_config`. We mirror the same set of keys.

- [ ] **Test:** the existing `_fixture/03` (`reference-benchy-with-filament-customizations.3mf`) was added for exactly this — currently the integration test asserts on a degraded output. Update the fixture's expected output to match GUI ground truth and assert per-slot keys land at the right index.

- [ ] **Commit:**
```
Apply per-filament 3MF customizations at correct slot index
```

---

### Gap 3 (medium): `flush_volumes_matrix` dimension drift on cross-printer slices

The new resolver swaps filament names across printer variants when the user retargets a 3MF (`/profiles/resolve-for-machine`). It does *not* change filament *count*. But the legacy gateway path used to truncate per-filament arrays when count differed; the v2 path lost that guard, and we restored just the project-config filter via `s_project_options` — `flush_volumes_matrix` is one of the keys that *passes through* the filter (it's project-level), so its size still matches the 3MF's authored filament count.

If a future flow ends up slicing with M filaments while the 3MF's `flush_volumes_matrix` is N×N (M ≠ N), GCode export aborts with `Flush volumes matrix do not match to the correct size!` (the message is at `vendor/OrcaSlicer/src/libslic3r/GCode.cpp:5394-5411`).

**Why risk-class 3:** today's flow doesn't change filament count, so this doesn't bite immediately. But the resolver could be extended (or the user could manually drop a filament) and the abort would land mid-slice. The legacy gateway code shipped a `_resize_flush_volumes` helper that handled exactly this; its history is in commit `155fc34`.

**Fix:**

- [ ] **Resize in C++** after the `s_project_options` filter and before `construct_full_config`. Compute `target_n = filament_presets.size()`, `nozzle_count = printer_extruder_id length`. Resize `flush_volumes_matrix` to `target_n²·nozzle_count`, `flush_volumes_vector` to `2·target_n`, `flush_multiplier` to `nozzle_count`. Preserve old entries where indices fit; fill new cells with 140 mm³ off-diagonal, 0 on-diagonal (OrcaSlicer defaults — see `_resize_flush_volumes` in commit `155fc34:app/slicer.py:580-620` for the exact formula).

- [ ] **GUI reference:** `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:4316-4354` is the GUI's resize implementation. Mirror its preserve-and-fill logic.

- [ ] **Test:** integration test with a 4-filament 3MF sliced with 2 filaments — assert no GCode error and a 2×2 matrix in the output.

- [ ] **Commit:**
```
Resize flush_volumes_matrix to match active filament count
```

---

### Gap 4 (low-medium): filament defaults filter uses `Preset::filament_options()` only

`slice_mode.cpp` lines ~262-275 — we pre-overlay each `filament_cfgs[i]` with defaults for keys in `Preset::filament_options()` to mitigate `construct_full_config`'s nullptr-deref on missing keys. The GUI populates *every* key on each filament Preset via the inheritance chain ending at `filaments.default_preset()`. If a 3MF carries an exotic per-filament key that's not in `Preset::filament_options()`, and `filament_cfgs[0]` has it but `filament_cfgs[1]` doesn't, the per-key merge in `construct_full_config` still nullptr-derefs.

**Why low-medium:** hasn't been observed in the wild. `Preset::filament_options()` covers the canonical set. But user-imported filament JSONs occasionally carry keys upstream Orca added in newer versions.

**Fix:**

- [ ] **Use the inheritance chain instead of a static filter.** After loading each filament JSON, walk up its `inherits` chain (the JSON Python writes already has this resolved into `from`/`inherits` metadata) and apply parent values for any key not in the leaf. Or simpler: just call `Preset::normalize` on each filament_cfg after loading (which uses `set_num_filaments` to pad each per-filament key from `FullPrintConfig::defaults`). We already call `Preset::normalize(final_cfg)` on the merged config; doing it earlier on each filament_cfg costs almost nothing and matches GUI inheritance semantics.

- [ ] **Reference:** `vendor/OrcaSlicer/src/libslic3r/Preset.cpp:370-415` (`Preset::normalize`).

- [ ] **Commit:**
```
Normalize each filament_cfg before construct_full_config
```

---

### Gap 5 (low): no `validate_presets` call

The GUI runs `PresetBundle::validate_presets` on every 3MF load (`vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:1260`). It checks each preset's inheritance chain against the system catalog and surfaces "preset not found" errors to the user. Our binary skips this because Python upstream resolves inherits before writing the temp JSONs.

**Why low:** Python's resolution catches most "preset not found" cases via `ProfileNotFoundError`. The remaining gap: if the *3MF* references a preset that no longer exists in our catalog (vendor renamed/removed), the binary loads the fallback values silently and slices with the wrong defaults.

**Fix:**

- [ ] **Have Python validate the 3MF's `printer_settings_id` / `print_settings_id` / `filament_settings_id` against the resolved catalog before invoking the binary.** Log warnings for mismatches; surface them in the slice response's `settings_transfer` block alongside the existing `filament_changed` reporting.

- [ ] **Commit:**
```
Validate 3MF preset references against current catalog
```

---

### Gap 6 (cleanup): `extruder_ams_count` not erased from `threemf_config`

The GUI explicitly `config.erase("extruder_ams_count")` after extracting it (`vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:3528-3529`). We don't. After today's `s_project_options` filter, `extruder_ams_count` is dropped from the `project_config` we pass to `construct_full_config`, so this is mostly cosmetic. But it remains in `threemf_config`, which is still read by `apply_overrides_for_slot` (lines 327-379). If a future code path ever consumes it from `threemf_config` directly, it sees stale data.

**Why cleanup-only:** no current code path is affected. Keeping for hygiene + parity.

**Fix:**

- [ ] **Erase `extruder_ams_count` from `threemf_config`** immediately after `Model::read_from_file` populates it, with a code comment citing the GUI line. One-line change.

- [ ] **Commit:**
```
Erase extruder_ams_count from threemf_config to match GUI cleanup
```

---

## Self-Review

**Spec coverage:** the six gaps were identified by reading `slice_mode.cpp` end-to-end and diffing against `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp`'s `load_config_file_config` (3423+) and `full_fff_config` (3039+). Each gap has a GUI source citation.

**Execution order:** gaps are written in priority order (1 = real crash class, 2-3 = real wrong-output classes, 4-6 = latent / cleanup). Implement top-down; can stop after any gap if budget runs out.

**Build cost:** every C++ gap is ~25 min via `scripts/build-and-ship.sh`. Gaps 5 (Python only) is a few seconds. Total: ~2 hours of build time + ~2 hours of editing + testing if we do all six.

**Risks:**
- Gap 2 needs careful index handling; off-by-one would silently flip values across slots. The integration fixture catches this.
- Gap 4 changes the size of every filament_cfg before merge; could surface latent issues (e.g. unused defaults overriding system process values). Run the fidelity test suite in `tests/integration/` after.
- Gap 1 is the only "must fix soon" item — the others are technical debt with bounded blast radius.
