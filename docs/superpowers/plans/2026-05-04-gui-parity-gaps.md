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

## Session Log — 2026-05-04 (post-plan)

Three GUI-parity issues were diagnosed and fixed in the same session as this plan was written. They aren't from the six gaps above (those are still pending) but are recorded here so future audits don't re-investigate.

### Fixed: `plate_type` not honored on `/slice/v2` (orcaslicer-cli `13e6ff9`)

**Symptom:** the gateway's plate-type dropdown ("Textured PEI Plate" etc.) had no effect end-to-end. The slicer always emitted `curr_bed_type = Cool Plate` for an A1 mini regardless of the user's pick.

**Root cause:** the `SliceTokenRequest` schema and both v2 handlers (`/slice/v2`, `/slice-stream/v2`) had no `plate_type` field — FastAPI silently dropped it. The C++ binary then carried `curr_bed_type` over from the input 3MF; on a P-series-authored project sliced for A1 mini, libslic3r's machine-compat reset collapsed `Supertack Plate` to the printer's `default_bed_type` (= `Cool Plate`).

**Fix:** added `plate_type: str | None` to `SliceTokenRequest`; resolve via `resolve_plate_type_for_machine` and forward the OrcaSlicer label to the binary. Binary applies it via `set_deserialize("curr_bed_type", …)` AFTER the 3MF carry-over so the override wins. Mirrors `Plater::on_change_bed_type`. Bad values surface as `invalid_plate_type`.

**Lesson:** when adding a new project-level slice control, the schema needs to thread through `SliceTokenRequest` → C++ `SliceRequest` → `final_cfg` mutation, and the override must apply after the 3MF carry-over (line 392 of `slice_mode.cpp`).

### Fixed: `filament_map` corrupted by AMS-tray semantics (orcaslicer-cli `5e53050` + bambu-gateway `7fa0edb`)

**Symptom:** prime-tower auto-disable wasn't firing for single-filament-actually-used plates. Output had `enable_prime_tower = 1` and a wipe-tower position; GUI's same input emitted `enable_prime_tower = 0`. Cost: ~+1% time, +0.5g filament per print.

**Initial hypothesis:** libslic3r's prime-tower auto-disable was GUI-only. **Wrong.** It lives in `DynamicPrintConfig::normalize_fdm_2` (PrintConfig.cpp:8081-8087) and is called by `Print::apply` itself — both GUI and headless flows hit it. The branch fires only when `extruders().size() == 1` (or `ByObject + multi-object`), gated off by smooth-timelapse and wrapping-detection.

**Actual root cause:** the gateway's `tray_slot` UI was repurposing libslic3r's `filament_map` field for AMS-tray-slot semantics. `slicer_client._normalize_filament_selection` computed `filament_map = [tray_slots.get(i, i+1) for i in range(N)]` — for an A1 mini with one tray-slot override, this produced values like `[0, 2]`. libslic3r interpreted that as "filaments use extruders 0 and 2", `extruders().size()` returned 2, and the auto-disable branch never fired.

The two concepts are different:
- **`filament_map` (libslic3r)**: per-filament extruder index (1-based), `1..nozzle_count`. For A1 mini it's always `[1, 1, …]`.
- **AMS tray slot**: which physical tray loads each filament. Print-time concern, sent via MQTT `ams_mapping`. The gateway already had `build_ams_mapping` building this independently from the same `tray_slot` payload.

**Fix:**
- (Gateway) `_normalize_filament_selection` always returns `(filament_ids, None)`. `tray_slot` continues flowing to `build_ams_mapping` at print time.
- (orcaslicer-cli, defense-in-depth) v2 handlers reject any `filament_map[i]` outside `1..nozzle_count` with HTTP 400 `invalid_filament_map`.

**Lessons:**
1. Two distinct concepts shared the name `filament_map` across the stack (libslic3r per-extruder-index vs the gateway's AMS-tray-slot map). The naming collision masked the bug for months. **When the slicer field name and the UI concept aren't the same thing, keep them separate at every layer.**
2. The "GUI does X that we don't" diagnosis was wrong twice in a row before instrumentation pinned the right answer. Lesson reinforced: **gather evidence at every component boundary before proposing fixes** — a direct curl to `/slice/v2` with explicit `filament_map=[1, 1]` would have flipped the prime tower in 30 seconds and skipped two hours of vendor-source archaeology.
3. The v2 handlers' validation is now a paper trail for any future client that confuses the two: a 400 + clear message catches the mistake at the API boundary, before it reaches libslic3r.

### Implication for the six gaps above

None of the gaps as originally written address the AMS-tray confusion — they're about `slice_mode.cpp`'s preset composition, not the API contract. **Gap 2** ("per-filament-slot overrides go flat, not indexed") still stands, though it's worth re-checking whether the corrupted-`filament_map` symptom was masking any per-slot override misbehavior we'd otherwise have noticed.

The prime-tower auto-disable behaviour now works without further changes — it was a symptom, not a separate gap. Don't add a Gap 7 for it.

---

## Self-Review

**Spec coverage:** the six gaps were identified by reading `slice_mode.cpp` end-to-end and diffing against `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp`'s `load_config_file_config` (3423+) and `full_fff_config` (3039+). Each gap has a GUI source citation.

**Execution order:** gaps are written in priority order (1 = real crash class, 2-3 = real wrong-output classes, 4-6 = latent / cleanup). Implement top-down; can stop after any gap if budget runs out.

**Build cost:** every C++ gap is ~25 min via `scripts/build-and-ship.sh`. Gaps 5 (Python only) is a few seconds. Total: ~2 hours of build time + ~2 hours of editing + testing if we do all six.

**Risks:**
- Gap 2 needs careful index handling; off-by-one would silently flip values across slots. The integration fixture catches this.
- Gap 4 changes the size of every filament_cfg before merge; could surface latent issues (e.g. unused defaults overriding system process values). Run the fidelity test suite in `tests/integration/` after.
- Gap 1 is the only "must fix soon" item — the others are technical debt with bounded blast radius.
