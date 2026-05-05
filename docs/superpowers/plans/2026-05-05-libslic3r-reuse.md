# Phase 7: Reuse libslic3r/GUI logic instead of porting it — Implementation Plan

> **Status (as of 2026-05-05):** Option 2 selected. Working on `feat/presetbundle-refactor` branched off `feat/orca-headless`. Snapshot of pre-refactor `/profiles/*` API responses captured in `tests/_snapshots/pre-presetbundle/` (4530 filaments / 829 machines / 2564 processes — diff target for Phase 4-equivalent work).

**Goal:** Eliminate the places where our wrapper still ports libslic3r/GUI logic into Python or into our C++ shim. The standing principle is "reuse GUI code, don't reimplement" — every parallel implementation is a drift surface, and several of them already required hotfixes (e.g. the `s_printer_slot_blocklist` we hand-curated to dodge SIGSEGVs that `PresetBundle` would have prevented for free).

**Why this matters:** we just shipped fixture 07 + the `Print::validate()` fix. The remaining mismatches against the GUI are no longer in the slice path itself — they're in the *setup* path (config resolution, override application, custom-filament import). Each port is a thing that can quietly diverge when OrcaSlicer ships an upstream change.

**Tech Stack:** C++17 (libslic3r in-tree under `vendor/OrcaSlicer/src/libslic3r`), Python 3.12 (FastAPI orchestration). Build via `scripts/build-and-ship.sh`.

**Where this plan lives, where it executes:** plan committed to `orcaslicer-cli/docs/superpowers/plans/`. All execution lives in this repo on `feat/orca-headless`.

**Repo conventions:**
- One phase = one commit (or a tight cluster), so we can bisect if a regression slips in.
- Each phase finishes with the fidelity suite green: `scripts/run-fidelity.sh` against the live image. We are at 6/6 today.
- Cite the GUI source line we're now calling (`vendor/OrcaSlicer/src/libslic3r/...`) in code comments. When deleting a parallel implementation, the deletion commit body names the upstream function that replaces it.
- Build cycle is ~25–30 min at `-j2`. Plan accordingly — batch C++ changes within a phase if possible.

**Out of scope:**
- Item 4 (3MF parsing) and item 6 (AMS-assignability indexing) are deferred to the end. Item 4 is large (would need a binary-side 3MF importer surface or a libslic3r FFI shim); item 6 is cosmetic (listing/selection only, doesn't affect slicing). Both are kept for after the slice-path items are clean.
- Reworking how the binary holds state (e.g. moving from "construct configs ourselves, call `construct_full_config`" to "stand up a real `PresetBundle` per slice"). Some phases here may *push us toward* a `PresetBundle`-shaped architecture, but landing that explicitly is not a goal of this plan — if a phase requires it, we'll stop and decide.

## Status summary

| Phase | Item | Scope | Status | Commit |
|---|---|---|---|---|
| 1 | #3 | Flush-volume / multi-material topology resize | 🌀 Subsumed by Option 2 (PresetBundle handles it) | — |
| 2 | #2 | 3MF project-config override application | 🌀 Subsumed by Option 2 (Approach B chosen) | — |
| 3 | #5 + #7 | Custom filament ID + import stamping | ⏳ Not started — independent | — |
| 4 | #1 | Profile inheritance resolution | 🌀 Subsumed by Option 2 (manifest from PresetBundle) | — |
| **Option 2** | #1+#2+#3 | **Stand up real `PresetBundle` in the binary; serve API from manifest** | 🚧 In progress | — |
| 5 (deferred) | #4 | 3MF ZIP/XML parsing → expose libslic3r's `_BBS_3MF_Importer` to Python | 🔒 Deferred | — |
| 6 (deferred) | #6 | AMS-assignability indexing → falls out of manifest for free | 🔒 Deferred | — |

---

## Phase 1 — Flush-volume topology resize via `PresetBundle::update_multi_material_filament_presets`

**Where we duplicate today:** `cpp/src/slice_mode.cpp:163-228` `resize_flush_volumes_for_topology`. Hand-rolled resize of `flush_volumes_matrix`, `flush_volumes_vector`, `flush_multiplier` to match `(extruder_count, filament_count)` whenever the requested machine has a different topology than the 3MF's source machine.

**Upstream equivalent:** `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:4316-4355` `update_multi_material_filament_presets`. Same matrix + vector + multiplier resize, called by `set_filament_preset` whenever the active filament set changes.

**Risk:** lowest of the bunch. The function is a pure config mutation — no PresetBundle-internal state required beyond the config it operates on.

### Steps

- [ ] **Read the upstream signature.** Confirm whether `update_multi_material_filament_presets` is callable without a full `PresetBundle` instance (it may use member fields like `printers.get_edited_preset()`). If it does, decide: (a) factor out the inner resize as a free function in libslic3r and call that, (b) stand up a minimal `PresetBundle` to satisfy it, (c) keep our resize but mark it "GUI parity verified at SHA xxx" with a unit test that fails if the upstream signature changes.
- [ ] **Make the call** at the same point in `slice_mode.cpp` where `resize_flush_volumes_for_topology` runs today (just after we know the target machine's extruder count and the project's filament count).
- [ ] **Delete `resize_flush_volumes_for_topology`** and the two static helpers it depends on.
- [ ] **Verify against fixture 06** (cross-printer / topology-mismatch fixture if we have one; otherwise hand-craft from fixture 01 by routing it to a 2-extruder machine).
- [ ] **Fidelity suite green** (`scripts/run-fidelity.sh`). Bump `API_REVISION`.
- [ ] **Commit:** `Use PresetBundle::update_multi_material_filament_presets directly`

**Success criterion:** `resize_flush_volumes_for_topology` is gone from the diff. Fixture 06 still produces matching `flush_volumes_*` in `slice_info.config`.

**Stop and ask if:** option (b) above (stand up a `PresetBundle`) is the only viable path. That's a phase-3-or-4-sized change, not a phase-1 change.

---

## Phase 2 — 3MF project-config override application via the GUI's load path

**Where we duplicate today:** `cpp/src/slice_mode.cpp:78-111` `apply_overrides_for_slot` + the `s_printer_slot_blocklist` (7 hand-curated keys: `extruder_variant_list`, `printer_extruder_variant`, `printer_extruder_id`, `extruder_type`, `nozzle_volume_type`, `filament_extruder_variant`, `filament_self_index`, `extruder_ams_count`). The blocklist exists because raw `different_settings_to_system[printer_slot]` overrides corrupted `final_cfg` and caused SIGSEGVs.

**Upstream equivalent:** `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp` — `load_3mf_file` + `_extract_project_config_from_archive`. The GUI loads a 3MF, extracts its project config, applies it onto the active preset stack, and never touches keys the preset stack doesn't expose. The blocklist is implicit in the GUI: it can't crash because it never writes those keys onto a printer preset whose extruder topology already has them as fixed-shape vectors.

**Risk:** medium. Two competing approaches:

- **Approach A — keep our pipeline, fix the blocklist correctly.** Replace `s_printer_slot_blocklist` with a derivation: take the *intersection* of the 3MF's slot keys with the keys the target printer preset already declares. Anything the printer preset doesn't declare is rejected. This matches the GUI's behavior without needing a `PresetBundle`.
- **Approach B — use `PresetBundle::load_3mf_file` directly.** Stand up a `PresetBundle`, point it at our resolved profile JSONs, hand it the 3MF, let it apply overrides. Largest scope change but the most "GUI-faithful."

We should start with **Approach A** unless we already need `PresetBundle` for Phase 4. Decide at the start of the phase.

### Steps

- [ ] **Decide Approach A vs B.** If Phase 4 (item #1) is still pending, A is fine — we can revisit when we do Phase 4. If we want one C++ landing instead of two, batch B into Phase 4.
- [ ] **Approach A:**
    - [ ] Compute the allowed-key set for the printer slot as `printer_preset.config.keys()`. Use that to filter `different_settings_to_system[printer_slot]` instead of `s_printer_slot_blocklist`.
    - [ ] Same shape for filament slot and process slot — derive allowed-keys from the resolved preset, not from a hardcoded list.
    - [ ] Delete `s_printer_slot_blocklist` and the `excluded_keys` param if no longer needed.
- [ ] **Approach B:** scoped under Phase 4.
- [ ] **Verify** against fixture 01 (single-filament), fixture 06 (multi-filament), fixture 07 (with `different_settings_to_system` populated).
- [ ] **Hand-craft a regression** for the SIGSEGV path: edit a fixture's `Metadata/project_settings.config` to put `extruder_variant_list` into the printer slot's diff list, slice against a different machine, confirm we filter (don't crash, don't write).
- [ ] **Fidelity suite green.** Bump `API_REVISION`.
- [ ] **Commit:** `Filter 3MF overrides by target preset's declared keys`

**Success criterion:** the hardcoded blocklist is gone. The hand-crafted regression slices cleanly. Fixture 07 still matches GUI on `different_settings_to_system`-derived overrides.

**Stop and ask if:** Approach A turns out to need data we don't have at the call site (e.g. the resolved presets aren't accessible as `keys()` until later in the pipeline).

---

## Phase 3 — Custom-filament ID generation + import stamping via the binary

**Where we duplicate today:**
- `app/profiles.py:277-297` `_generate_custom_filament_id` — `"P" + md5(name)[:7]` with a timestamp-seeded collision fallback.
- `app/profiles.py:311-374` `materialize_filament_import` — stamps `setting_id`, `instantiation="true"`, generated/provided `filament_id`; validates inheritance refs; checks AMS-scope uniqueness.

**Upstream equivalent:** `vendor/OrcaSlicer/src/slic3r/GUI/CreatePresetsDialog.cpp:530-548` (ID mint, identical recipe) plus the surrounding stamping logic in the same file. `calculate_md5` lives in `slic3r/GUI/` so it isn't directly libslic3r; we'll need to lift it (or its equivalent) to a place the binary can call.

**Risk:** medium. The endpoint `/profiles/filaments/import` is currently a synchronous Python operation that doesn't shell out. Routing it through the binary changes the latency profile and adds a subprocess to a previously cheap path.

### Steps

- [ ] **Investigate `calculate_md5`.** Determine whether it's a thin wrapper over `boost::uuids::detail::md5` or similar. If yes, we can call the same primitive directly; if it has GUI-only deps, we lift it into a shared header.
- [ ] **Add a binary subcommand:** `orca-headless mint-filament-import --in <payload.json> --out <result.json>`. Reads a candidate filament JSON + parent name + AMS-scope info; returns the stamped JSON (with `setting_id`, `filament_id`, `instantiation`) or a structured error. The stamping logic lives in C++ now, not Python.
- [ ] **Replace `_generate_custom_filament_id`** with a subprocess call to that subcommand. Cache nothing — collisions still bail with the timestamp seed, but now at the C++ layer where the GUI handles them.
- [ ] **Replace `materialize_filament_import`** body with the same subprocess call; keep the Python function as a thin wrapper that does the FastAPI request marshaling and the on-disk write to `USER_PROFILES_DIR`.
- [ ] **Test:** existing import endpoint tests (`tests/`-side; check for `test_filament_*` or write one if missing). Verify a round-trip: import → list → resolve → slice with the imported filament.
- [ ] **Fidelity suite green.** Bump `API_REVISION`.
- [ ] **Commit:** `Mint custom filament IDs via the binary, not Python md5`

**Success criterion:** Python no longer hashes anything to mint a filament_id. The import path produces the same IDs the GUI would produce for the same input.

**Stop and ask if:** the binary subcommand turns out to need the full `PresetBundle` setup just to validate `inherits` refs. If so, batch this into Phase 4.

---

## Phase 4 — Profile inheritance resolution via `PresetBundle::find_preset`

**Where we duplicate today:** `app/profiles.py` — the entire `_resolve_chain_for_payload` (`profiles.py:391-436`) plus the inheritance cascade at `profiles.py:109-142, 300-389`. We walk `inherits` chains in Python, prefer same-vendor parents, fall back to `OrcaFilamentLibrary`, and produce flattened preset JSONs that we then ship to the binary.

**Upstream equivalent:** `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp` and `Preset.cpp` — `Preset::load_from_file` walks the chain at load time; `PresetBundle::find_preset` resolves names; the GUI never sees a "flattened" preset because the bundle holds the chain in memory.

**Risk:** highest in this plan. This is the biggest behavioral surface in `app/profiles.py` and changing how it works affects every endpoint (`GET /profiles/{machines,processes,filaments}`, `/slice`, `/slice/v2`, `/profiles/reload`, both filament import endpoints).

**Approaches:**

- **Approach A — service-level cache populated by the binary at startup.** At service boot, run `orca-headless dump-profiles` once; binary stands up a `PresetBundle`, walks every preset, writes a single JSON manifest with all metadata + resolved chains. Python loads the manifest and serves `/profiles/*` from it. `/profiles/reload` re-runs the dump. Slicing keeps using whatever profile-passing contract we have today.
- **Approach B — per-slice resolution in the binary.** Drop pre-flattening entirely. Python passes `setting_id`s + raw vendor JSONs path; binary resolves at slice time via `PresetBundle::find_preset`. Largest behavior change but the "thinnest wrapper" outcome.
- **Approach C — pybind11 / FFI.** Link libslic3r as a shared library, call `PresetBundle::find_preset` from Python directly. Heaviest engineering but no subprocess overhead.

We start with **Approach A**: lowest risk to the API surface, gives us a manifest we can diff against the current Python output before flipping the switch.

### Steps

- [ ] **Snapshot current behavior.** Run today's `/profiles/machines`, `/profiles/processes`, `/profiles/filaments` against `2.3.2-32` and save the JSON. This is the diff target — anything Approach A produces should match this byte-for-byte (or with documented deltas).
- [ ] **Add `orca-headless dump-profiles --out <manifest.json>` subcommand.** Stands up a `PresetBundle` over `PROFILES_DIR` + `USER_PROFILES_DIR`, iterates `printers`, `filaments`, `prints`, emits the same shape Python emits today (machine/process/filament dicts with resolved `compatible_printers`, `setting_id` mappings, etc.).
- [ ] **Run dump at service startup** (in `app/main.py` lifespan). Cache the parsed manifest in memory. Wire `_raw_profiles`, `_type_map`, `_vendor_map`, `_name_index`, `_setting_id_index` to read from the manifest instead of from disk-walked JSONs.
- [ ] **Diff against snapshot.** Document any deltas; resolve them either by adjusting the binary's emitter or by updating downstream code to match the new (correct) output.
- [ ] **Wire `/profiles/reload`** to re-run the dump.
- [ ] **Delete** `_resolve_chain_for_payload` and the inheritance walk. Keep only the thin wrapper that exposes the manifest's contents through the existing endpoints.
- [ ] **Verify** the C++ `s_project_options` filtering still works — the binary already builds presets via `construct_full_config`, so the manifest is for the API layer, not the slice layer.
- [ ] **Fidelity suite green.** Bump `API_REVISION` (this is the one with real upgrade risk for the gateway — note in commit body).
- [ ] **Commit:** `Resolve profile inheritance via PresetBundle, not Python`

**Success criterion:** `app/profiles.py` no longer walks `inherits` chains in Python. The API responses match the pre-change snapshot (or document what changed and why). The binary is the only thing that knows what "inherits" means.

**Stop and ask if:** the manifest diff against the snapshot turns up systematic differences (e.g. resolution order, vendor preference). That's a sign the GUI does it differently than we always assumed, and we should agree on which version is "correct" before flipping the switch.

---

## Phase 5 (deferred) — 3MF parsing via libslic3r's importer

**Where we duplicate today:** `app/threemf.py:85-553`. Full Python reimplementation of vertex/transform parsing, component recursion, plate extraction, multi-plate grid math.

**Upstream equivalent:** `vendor/OrcaSlicer/src/libslic3r/Format/bbs_3mf.cpp:651-2254` (`_BBS_3MF_Importer`).

**Why deferred:** currently behaves correctly. Replacing it requires either a libslic3r FFI shim or running the binary as a 3MF inspection service — both larger than the slice-path items above. Revisit once Phases 1–4 are done.

---

## Phase 6 (deferred) — AMS-assignability via `Preset::is_user()`

**Where we duplicate today:** `app/profiles.py:212-236` `_is_ams_assignable_filament`.

**Why deferred:** affects listing / selection only. No slice-output impact. Picking it up is essentially a free side effect of Phase 4 (the manifest from `dump-profiles` can include the assignability flag computed by the binary). If Phase 4 lands, this collapses to "stop computing assignability in Python; read it from the manifest."

---

## Option 2 — unified PresetBundle refactor

### Why this scope

Investigation on 2026-05-05 (see chat log) found:
- Fixture 07's drift is **infill segmentation**: +362 FEATURE segments and +13098 LINE_WIDTH transitions, all in solid+sparse infill. Walls/bridges/top-surface match exactly, every visible CONFIG_BLOCK setting matches byte-for-byte.
- The 3MF embeds the project-local filament preset variant in `Metadata/filament_settings_1.config`. For fixture 07 specifically, that file only contains `nozzle_temperature: ['235']` (which we already overlay via `different_settings_to_system`), so "use project_presets verbatim" is **not** a sufficient fix on its own — the drift driver is something subtler that PresetBundle's full path handles end-to-end.
- We already call `Slic3r::Model::read_from_file` with `LoadConfig | LoadAuxiliary` (so `project_presets` are available in `slice_mode.cpp:339-351`), but we discard their configs and only use them for name resolution. The GUI calls `PresetBundle::load_project_embedded_presets(project_presets)` after this, then `bundle.full_fff_config()` — that's the load path we need to replicate.

### Architecture decisions

**Binary holds a `PresetBundle` per slice — not service-wide.** Loading all ~8000 vendor presets at every slice would be slow; loading them once and persisting would change deployment. Instead, Python passes the **inheritance closure** for the request (machine + its ancestors, process + its ancestors, each filament + its ancestors — typically <50 JSONs total) and the binary stands up a minimal bundle containing exactly those. Then `bundle.load_project_embedded_presets(project_presets)` absorbs the 3MF's variants on top, and `bundle.full_fff_config()` composes.

**Manifest for `/profiles/*` API.** Separate concern from slicing. A new `orca-headless dump-profiles` subcommand stands up a full bundle (loads everything in `PROFILES_DIR` + `USER_PROFILES_DIR`) and writes a JSON manifest. Python's lifespan calls it once at startup, and `/profiles/reload` re-runs it. Cost paid once at boot, not per-slice.

**API contract between Python and binary stays compatible** at the C++ subprocess layer. The slice request still takes paths to resolved JSONs; what changes is how the binary uses them (loads them into a real bundle instead of manual `Preset` construction). That keeps `app/binary_client.py` and the wire format stable.

**Bambu-gateway needs no changes.** The `/slice/v2` request/response shape stays identical; this is an internal-only refactor.

### Sub-phases (one commit each, fidelity green at each step)

#### Sub-phase A — Slice-path PresetBundle (closes fixture 07 drift)

Refactor `cpp/src/slice_mode.cpp` to use a real `PresetBundle` instead of manual `Preset` construction.

- [ ] **Inheritance closure in Python.** Extend `app/slicer.py::materialize_profiles_for_binary` to write each preset in the inheritance chain as its own JSON (e.g. `filament-0.json`, `filament-0-parent-1.json`, …) and pass the list of paths plus the *selected* leaf name. The binary loads all of them into the bundle so `bundle.filaments.find_preset(name)` works for the leaf and any reference from `inherits`.
- [ ] **Bundle setup in `slice_mode.cpp`.** Replace lines 467-563 (manual filament-defaults filling, `Preset` construction, `construct_full_config` call, `Preset::normalize`, manual `filament_ids` repopulation) with:
  ```
  PresetBundle bundle;
  for each (machine|process|filament) in request:
      bundle.{printers|prints|filaments}.add_preset_from_json(path)
  bundle.printers.select_preset_by_name(machine_name)
  bundle.prints.select_preset_by_name(process_name)
  bundle.filament_presets = filament_names_per_slot
  bundle.project_config = project_config  // filtered to s_project_options
  bundle.load_project_embedded_presets(project_presets)  // absorb 3MF variants
  final_cfg = bundle.full_fff_config(/*apply_extruder=*/true, /*filament_maps_new=*/std::nullopt)
  ```
- [ ] **Delete now-redundant code:** `apply_overrides_for_slot`, `s_printer_slot_blocklist`, `build_project_filament_inherits_map`, `resize_flush_volumes_for_topology`, the per-slot name guard, the manual `Preset::normalize` calls, the `filament_ids` repopulation block. PresetBundle does all of this.
- [ ] **Keep:** `Model::read_from_file` (geometry + project_presets discovery), `recenter_on_plate`, `Print::validate()` call, `Print::process()`, gcode export, response stat extraction, plate_type override, `s_project_options` whitelist (still applies to `bundle.project_config`).
- [ ] **Track `settings_transfer` semantics.** The current response includes `settings_transfer` headers detailing what was transferred from the 3MF. `bundle.full_fff_config()` doesn't expose this directly; we recover it by diffing the bundle's edited preset against its parent (via `PresetCollection::dirty_options_without_option_list`), which is exactly the same data source the GUI uses to populate `different_settings_to_system`. Mirror that into our response shape.
- [ ] **Build + ship.** `scripts/build-and-ship.sh`. ~30 min.
- [ ] **Verify fidelity:** all 6 fixtures pass. Fixture 07 drift should drop close to zero (target: under default 2%/1.5% tolerance — if it does, tighten its tolerance back in the same commit).
- [ ] **Bump `API_REVISION` → 33.**
- [ ] **Commit:** `Compose slice config via real PresetBundle (Phase 2 Approach B)`

**Stop and ask if:** PresetBundle setup needs initialization steps (e.g. `AppConfig`, vendor model loading) that are GUI-internal and can't be stood up cleanly headless. That's the signal the bundle was never designed to be used outside the GUI's lifecycle, and we'd need to rethink (e.g. extract a smaller helper from `full_fff_config`).

#### Sub-phase B — Manifest dump for `/profiles/*` API

Add `orca-headless dump-profiles` subcommand. Python loads from manifest at startup.

- [ ] **New subcommand:** `orca-headless dump-profiles --profiles-dir <path> --user-dir <path> --out <manifest.json>`. Stands up a full `PresetBundle`, calls `bundle.load_presets()` to walk both dirs, then iterates `bundle.printers/prints/filaments` and emits a manifest matching the shape we snapshotted in `tests/_snapshots/pre-presetbundle/`.
- [ ] **Lifespan integration:** `app/main.py::lifespan` calls the binary at startup, parses the manifest into the existing `_raw_profiles`/`_type_map`/`_vendor_map`/`_name_index`/`_setting_id_index` structures (or replaces them with a manifest-backed reader). `/profiles/reload` re-runs the dump.
- [ ] **Diff against snapshot.** Any deltas: either fix the binary's emitter to match Python's previous output, or document the delta as "Python was wrong, manifest is correct."
- [ ] **Delete `_resolve_chain_for_payload` and the inheritance walk** in `app/profiles.py`. Keep import endpoints (write to disk, then re-dump).
- [ ] **Verify fidelity** (no slicing change, but smoke-test the API endpoints).
- [ ] **Bump `API_REVISION` → 34.**
- [ ] **Commit:** `Serve /profiles/* from PresetBundle-generated manifest (Phase 4)`

**Stop and ask if:** the snapshot diff exposes systematic Python-vs-bundle differences in resolution order or vendor preference. Decide which is "correct" before flipping the switch.

#### Sub-phase C — Cleanup

- [ ] **`app/normalize.py`** — verify it's no longer needed on the slice path now that PresetBundle calls `Preset::normalize` itself. If unused, delete.
- [ ] **AMS-assignability** (Phase 6 / item #6) — collapse into manifest. Bundle knows what's user-facing; emit a flag.
- [ ] **Final fidelity run, tighten fixture 07 tolerance** if drift closed.
- [ ] **Commit:** `Remove parallel implementations subsumed by PresetBundle`

### Open questions to settle DURING Sub-phase A (not before)

1. **Does `PresetBundle::add_preset_from_json` exist?** If not, we need to find the right entry point (`load_external_config`, `load_config_file`, or extending `PresetCollection::load_preset`). First investigation step in Sub-phase A.
2. **What does `bundle.printers.select_preset_by_name` need beyond a loaded preset?** Some bundle methods touch `AppConfig`, vendor metadata, or wxWidgets via `wxGetApp()`. We need to identify those and stub them or skip them.
3. **`settings_transfer` reconstruction.** The current response shape uses `process_keys`, `printer_keys`, `filament_slots` arrays. Recovering this from a bundle requires diffing edited vs parent presets — needs a small helper on top of `PresetCollection::dirty_options_without_option_list`.

These are all "investigate during Sub-phase A, not in advance." If any blocks the sub-phase, we'll stop and rethink rather than push through.

## Walking through this

We'll do one phase at a time:

1. After each phase: confirm fidelity green, bump `API_REVISION`, commit, ship via `scripts/build-and-ship.sh`, recreate the container, re-run fidelity against the live image.
2. If a phase reveals that an earlier sequencing decision was wrong (e.g. Phase 2 needs a `PresetBundle` after all), we stop and rebatch — don't push through.
3. Don't push to remote without explicit instruction. Branch is `feat/orca-headless`.

**Open question to settle before starting Phase 1:** are there fidelity drifts in fixture 04/05/07 (the recent run showed ~4.5% time drift on those three) that we should chase first, or do they stay parked? If they're flaky-tolerance issues, the audit work is fine to start now. If they signal a real regression from the last batch, fix those first.
