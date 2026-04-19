# Slice Parity TODO

Findings from comparing `POST /api/print-preview` output (via bambu-gateway) against an OrcaSlicer GUI save of the same model, same machine (A1 mini / GM020), same process (`0.16mm High Quality @BBL A1M` / GP109), same filaments (`Bambu PLA Basic @BBL A1M` + imported `eSUN PLA-Basic @BBL A1M`), plate 2, `textured_pei_plate`.

Source model: `dripping-deck-box-v7-60-cards-basic-2.3mf` (originally saved for a P2S printer).

Methodology: diff `Metadata/project_settings.config` from each 3MF. 57 real value differences observed; 15 key-set differences.

## Done

- [x] **Smart settings transfer over-reach.** Our overlay copied 151 keys from the 3MF's project_settings onto the target process, while the GUI only applies the 4 keys declared in `different_settings_to_system[0]`. Fixed: `_overlay_3mf_settings` now takes an allowlist sourced from the 3MF fingerprint; transfers nothing when the fingerprint is empty or absent. See `app/slicer.py` and the updated `test_slicer_settings_transfer.py`.
- [x] **`print_extruder_variant` leak** (ours emitted `["Direct Drive Standard", "Direct Drive High Flow"]` vs GUI's `["Direct Drive Standard"]`) resolved as a side effect of the transfer fix — the extra variant was being injected through the over-transfer path.
- [x] **Per-filament transfer** (`different_settings_to_system[2+]`). Each filament slot's declared keys are overlaid onto the loaded filament profile only when that slot's selected filament matches the 3MF's original (`filament_settings_id[slot]`). When the user swaps in a different filament, customizations are discarded and reported via a new `X-Filament-Settings-Transferred` header (process-side header is unchanged). bambu-gateway surfaces the header into its own responses and the web UI shows both "applied" and "discarded" messages.
- [x] **Per-filament vector-length normalization.** Ports `Preset::normalize`'s per-filament expansion (`src/libslic3r/Preset.cpp` 370-415). Root cause: 38 filament-indexed keys (e.g. `pressure_advance`, `filament_cooling_*`, `textured_cool_plate_temp`) live only in `FullPrintConfig::defaults()` at length 1 — no vendor JSON overrides them. The GUI expands them to `len(filament_diameter)` via `Preset::normalize` on the combined config; the `orca-slicer` CLI does not re-run that step, so slot 1+ silently fell back to slot-0 values on multi-filament prints. Fixed in `app/normalize.py`: before writing the process JSON, resize these keys to `n_filaments`, padding length-1 vectors by repeat and injecting missing keys from a hard-coded defaults table (extracted from `PrintConfig.cpp` v2.3.2). See `docs/normalization-research.md` for the derivation.

## Pending

_(empty — slice parity with the GUI for single/multi-filament 3MFs is now tracked in `tests/test_normalize.py` and `test_slicer_settings_transfer.py`.)_

## Known, intentional divergences (not bugs)

- `layer_change_gcode` is prepended with `G92 E0` — Bambu relative-extrusion workaround (documented in CLAUDE.md).
- User-imported filaments get a generated `filament_id` of form `P<md5-prefix>` instead of inheriting the parent's id. Prevents ambiguity between system and user filaments.
- `inherits_group[i]` empty for imported filaments (we flatten on import; GUI keeps lineage label).
- `wipe_tower_x/y` differ — each slicer runs its own auto-arrange for the prime tower.
- 14 GUI-only metadata keys (`bbl_use_printhost`, `host_type`, `printhost_*`, `printer_agent`, `default_bed_type`, `thumbnails_format`, `filament_colour_type`, `filament_multi_colour`, `pellet_flow_coefficient`, `pellet_modded_printer`) — desktop-GUI state that the CLI slicer doesn't emit.
