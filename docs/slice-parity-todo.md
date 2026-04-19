# Slice Parity TODO

Findings from comparing `POST /api/print-preview` output (via bambu-gateway) against an OrcaSlicer GUI save of the same model, same machine (A1 mini / GM020), same process (`0.16mm High Quality @BBL A1M` / GP109), same filaments (`Bambu PLA Basic @BBL A1M` + imported `eSUN PLA-Basic @BBL A1M`), plate 2, `textured_pei_plate`.

Source model: `dripping-deck-box-v7-60-cards-basic-2.3mf` (originally saved for a P2S printer).

Methodology: diff `Metadata/project_settings.config` from each 3MF. 57 real value differences observed; 15 key-set differences.

## Done

- [x] **Smart settings transfer over-reach.** Our overlay copied 151 keys from the 3MF's project_settings onto the target process, while the GUI only applies the 4 keys declared in `different_settings_to_system[0]`. Fixed: `_overlay_3mf_settings` now takes an allowlist sourced from the 3MF fingerprint; transfers nothing when the fingerprint is empty or absent. See `app/slicer.py` and the updated `test_slicer_settings_transfer.py`.

## Pending

### Per-extruder / per-filament vector-length normalization

GUI runs `extend_default_config_length` + `Preset::normalize` (`src/libslic3r/Preset.cpp`) on each loaded preset, resizing per-extruder/per-filament vector options to match `nozzle_diameter` / `filament_diameter` / `*_extruder_variant`. Our resolver skips this.

Evidence: with 2 filament slots loaded, the GUI emits 2-element vectors for `filament_cooling_final_speed`, `filament_cooling_initial_speed`, `filament_cooling_moves`, `filament_ironing_flow`, `filament_ironing_inset`, `filament_ironing_spacing`, `filament_ironing_speed`, `filament_loading_speed`, `filament_loading_speed_start`, `filament_map`, `filament_multitool_ramming`, `filament_multitool_ramming_flow`, `filament_multitool_ramming_volume`, `filament_notes`, `filament_ramming_parameters`, `filament_shrinkage_compensation_z`, `filament_stamping_distance`, `filament_stamping_loading_speed`, `filament_toolchange_delay`, `filament_unloading_speed`, `filament_unloading_speed_start`, `filament_colour`, `default_filament_colour`, `adaptive_pressure_advance`, `adaptive_pressure_advance_bridges`, `adaptive_pressure_advance_overhangs`, `adaptive_pressure_advance_model`, `activate_chamber_temp_control`, `dont_slow_down_outer_wall`, `enable_overhang_bridge_fan`, `enable_pressure_advance`, `idle_temperature`, `pressure_advance`, `internal_bridge_fan_speed`, `ironing_fan_speed`, `support_material_interface_fan_speed`, `textured_cool_plate_temp`, `textured_cool_plate_temp_initial_layer`.

Ours emits 1-element vectors for those keys. No impact on single-filament prints; will matter for AMS multi-material.

Action when tackled: port the normalization step to `app/profiles.py` (or apply in `app/slicer.py` just before writing the profile JSONs). ~50 lines.

### `print_extruder_variant` picks up filament-scoped variant

Symptom: ours emits `print_extruder_variant = ["Direct Drive Standard", "Direct Drive High Flow"]`, GUI emits `["Direct Drive Standard"]`.

The imported eSUN filament JSON declares `filament_extruder_variant: ["Direct Drive Standard"]`, but our `materialize_filament_import` / merge path may be leaking the filament-side variant list into the process's `print_extruder_variant`. The A1 mini is single-extruder, so `print_extruder_variant` must be length 1.

Action: trace where `print_extruder_variant` is populated during slice prep; ensure it matches the machine profile's extruder count, not the filament count.

### Per-filament transfer (`different_settings_to_system[2+]`)

Slots 2+ of `different_settings_to_system` track per-filament customizations the user made in the GUI. We don't currently look at them — today we just pass the user-selected filament profiles as-is.

GUI applies them on top of the selected filament preset. If a user saves a 3MF with, say, a custom `nozzle_temperature` override on filament slot 1, our tool will currently ignore that customization unless the user also imports it as a custom filament.

Action: mirror the process-side transfer for each filament slot. Per-slot allowlist from `different_settings_to_system[slot + 2]`, overlay onto the corresponding loaded filament profile.

## Known, intentional divergences (not bugs)

- `layer_change_gcode` is prepended with `G92 E0` — Bambu relative-extrusion workaround (documented in CLAUDE.md).
- User-imported filaments get a generated `filament_id` of form `P<md5-prefix>` instead of inheriting the parent's id. Prevents ambiguity between system and user filaments.
- `inherits_group[i]` empty for imported filaments (we flatten on import; GUI keeps lineage label).
- `wipe_tower_x/y` differ — each slicer runs its own auto-arrange for the prime tower.
- 14 GUI-only metadata keys (`bbl_use_printhost`, `host_type`, `printhost_*`, `printer_agent`, `default_bed_type`, `thumbnails_format`, `filament_colour_type`, `filament_multi_colour`, `pellet_flow_coefficient`, `pellet_modded_printer`) — desktop-GUI state that the CLI slicer doesn't emit.
