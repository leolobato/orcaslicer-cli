# How OrcaSlicer CLI Loads Profiles

Notes on `orca-slicer` v2.3.2 behavior, verified against the upstream source at commit `c724a3f5f51c52336624b689e846c8fbc943a912`.

## TL;DR

- `--load-settings` and `--load-filaments` accept **already-resolved** profile JSONs.
- The CLI does **NOT** walk the `inherits` chain. There is no inheritance resolver in the CLI code path.
- If a profile JSON contains only the keys it overrides (typical for vendor child profiles), only those keys are applied; every other key silently falls through to whatever the input 3MF embeds.
- This is why `app/profiles.py` pre-resolves every profile before we write temp JSONs for the CLI.

## CLI Flags

From `src/libslic3r/PrintConfig.cpp:10151-10161`:

| Flag | Type | Purpose |
| --- | --- | --- |
| `--load-settings "m.json;p.json"` | coStrings | One machine JSON and one process JSON (`;`-separated). The CLI errors with `"duplicate machine config file"` / `"duplicate process config file"` if two of the same type are passed. |
| `--load-filaments "f0.json;f1.json;..."` | coStrings | One filament JSON per extruder/AMS slot. |

Help text explicitly declares the override priority (`OrcaSlicer.cpp:7168-7171`):

```
1) command-line values           (highest)
2) --load-settings / --load-filaments
3) values loaded from the 3MF    (lowest)
```

This is a **flat, key-by-key overlay**. There is no tree walk.

## What Happens When a File Is Loaded

`ConfigBase::load_from_json` (`src/libslic3r/Config.cpp:811-1112`) opens one file, parses JSON, and iterates its top-level keys. No file lookup for parents. No recursion. No merge of multiple files of the same type.

The `inherits` value, when present, is captured purely as a label for metadata:

- For machine/process profiles it is stored into `new_printer_system_name` / `new_process_system_name` and used only for the downstream "is this process compatible with this printer?" check (`OrcaSlicer.cpp:1892-1898`).
- For all profile types, `inherits` is explicitly skipped during the overlay merge (`OrcaSlicer.cpp:2706`).

The `from` key must **not** be stripped from the JSON the CLI receives — it is read by the CLI and used alongside other metadata. Our resolver leaves it intact.

## Overlay Onto the 3MF

`update_full_config` (`OrcaSlicer.cpp:2657-2720`) is the only merge step for loaded profiles:

1. The 3MF's embedded settings are parsed into a `full_config` (this is where most keys come from).
2. For each loaded profile, the CLI iterates `config.keys()` — i.e. **only keys actually present in the loaded JSON** — and copies each value onto `full_config`.
3. Metadata keys are skipped: `inherits`, `compatible_prints`, `compatible_printers`, `model_id`, `dev_model_name`, `name`, `from`, `type`, `version`, `setting_id`, `instantiation`.

A child profile with only three overridden keys overrides three keys. Everything else comes from the 3MF.

## Why Manual Resolution Is Required

If we passed a raw BBL filament JSON like this directly:

```json
{
  "type": "filament",
  "name": "Bambu PLA Basic @BBL X1C",
  "inherits": "Generic PLA @BBL X1C",
  "filament_max_volumetric_speed": ["12"],
  "nozzle_temperature": ["220"]
}
```

the CLI would apply only `filament_max_volumetric_speed` and `nozzle_temperature`. Every other parameter that `Generic PLA @BBL X1C` (and its own ancestors) provide — fan speeds, cooling curves, retraction, flow ratio — would silently fall through to the 3MF's embedded defaults. The result would slice, but with subtly wrong settings and no error.

The `resolve_profile_by_name` function in `app/profiles.py` walks the chain recursively, merging child over parent with `dict.update()`, and the resulting flat JSON is what we hand to the CLI.

## The Dead `*_full` Fallback

There is a code path (`OrcaSlicer.cpp:2278, 2319, 2389`) that, when no `--load-settings` is provided and the 3MF references a system preset by name, looks for `resources/profiles/BBL/{machine,process,filament}_full/<name>.json`. Those `*_full` directories **do not exist** in stock OrcaSlicer v2.3.2. It is carryover from Bambu Studio, where pre-resolved "full" profiles ship. It is name-based lookup only — still not a recursive inheritance walk.

## Where Inheritance Is Actually Resolved Upstream

`PresetBundle::load_vendor_configs_from_json` in `src/libslic3r/PresetBundle.cpp:3754-4284` is the GUI-side loader. It is what Orca's desktop app uses to populate the preset tree at startup. The CLI never calls it. Summary of its behavior (for reference; not reimplemented here):

- Reads a vendor's top-level index (`<vendor>.json`) and iterates machine → process → filament subfiles in that order.
- Stores each flattened config into a `std::map<name, DynamicPrintConfig>` keyed by preset name.
- For each subfile, looks up `inherits` in that map (same-vendor). Falls back to the `OrcaFilamentLibrary` bundle only — cross-vendor inheritance to arbitrary other vendors is not allowed.
- Merges parent into child via typed `DynamicPrintConfig::apply` (semantically equivalent to `dict.update`, but type-aware).
- Post-processes: drops keys not in the defaults (`Preset::remove_invalid_keys`), resizes per-extruder/per-filament vector options to match `nozzle_diameter` / `filament_diameter` / `*_extruder_variant` length (`extend_default_config_length` + `Preset::normalize`).
- `instantiation: "false"` presets remain in the map as ancestors but are not user-selectable.

## References

All line numbers against OrcaSlicer tag `v2.3.2`, commit `c724a3f5f51c52336624b689e846c8fbc943a912`.

- `src/libslic3r/PrintConfig.cpp:10151-10161` — flag definitions
- `src/libslic3r/Config.cpp:811-1112` — `ConfigBase::load_from_json`
- `src/OrcaSlicer.cpp:1860-1927` — `load_config_file` lambda (single-file load)
- `src/OrcaSlicer.cpp:1931-2017` — `--load-settings` handling, duplicate-type rejection
- `src/OrcaSlicer.cpp:2069-2123` — `--load-filaments` handling
- `src/OrcaSlicer.cpp:2657-2720` — `update_full_config` (flat overlay, metadata skip)
- `src/OrcaSlicer.cpp:7168-7171` — help text declaring priority order
- `src/libslic3r/PresetBundle.cpp:3754-4284` — GUI-side loader (not used by CLI)
