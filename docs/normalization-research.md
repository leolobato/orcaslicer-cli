# OrcaSlicer Preset Normalization — Port Reference

## Executive summary

OrcaSlicer resolves a preset JSON via `<default_config>.apply(raw_json)`, then calls in order: `extend_default_config_length` (resizes per-variant vector keys, replaces `nil` with defaults), `Preset::normalize` (pads per-filament vector keys to match `filament_diameter` size and appends `filament_settings_id`), `Preset::remove_invalid_keys` (drops unknown keys). Our CLI wrapper already hands JSON to `orca-slicer`, which re-runs this pipeline itself. What we need to port is only: (1) resize per-filament vector keys in a **process profile** to `n_filaments` when a 3MF supplies multiple filaments; (2) replace `"nil"` entries on per-variant keys. Both need `FullPrintConfig::defaults()` values, which are NOT derivable from vendor JSON — they live only in `PrintConfig.cpp`. Practical port: hard-code the 15 target process keys with their C++ defaults; skip SLA; skip multi-extruder variant expansion; skip `printer_options_with_variant_*` (Bambu is single-extruder).

## 1. `extend_default_config_length`

`src/libslic3r/Preset.cpp` 157-204:

```cpp
void extend_default_config_length(DynamicPrintConfig& config, const bool set_nil_to_default, const DynamicPrintConfig& defaults)
{
    constexpr int default_param_length = 1;
    int filament_variant_length = default_param_length;
    int process_variant_length  = default_param_length;
    int machine_variant_length  = default_param_length;

    if (config.has("nozzle_diameter")) {
        auto* nozzle_diameter = dynamic_cast<const ConfigOptionFloats*>(config.option("nozzle_diameter"));
        machine_variant_length = nozzle_diameter->values.size();
    }
    if (config.has("filament_extruder_variant"))
        filament_variant_length = config.option<ConfigOptionStrings>("filament_extruder_variant")->size();
    if (config.has("print_extruder_variant"))
        process_variant_length  = config.option<ConfigOptionStrings>("print_extruder_variant")->size();
    if (config.has("printer_extruder_variant"))
        machine_variant_length  = config.option<ConfigOptionStrings>("printer_extruder_variant")->size();

    auto replace_nil_and_resize = [&](const std::string& key, int length){
        ConfigOption* raw_ptr = config.option(key);
        ConfigOptionVectorBase* opt_vec = static_cast<ConfigOptionVectorBase*>(raw_ptr);
        if (set_nil_to_default && raw_ptr->is_nil() && defaults.has(key) &&
            std::find(filament_extruder_override_keys.begin(), filament_extruder_override_keys.end(), key) == filament_extruder_override_keys.end()) {
            opt_vec->clear();
            opt_vec->resize(length, defaults.option(key));
        } else {
            opt_vec->resize(length, raw_ptr);
        }
    };

    for (auto& key : config.keys()) {
        if      (print_options_with_variant.count(key))       replace_nil_and_resize(key, process_variant_length);
        else if (filament_options_with_variant.count(key))    replace_nil_and_resize(key, filament_variant_length);
        else if (printer_options_with_variant_1.count(key))   replace_nil_and_resize(key, machine_variant_length);
        else if (printer_options_with_variant_2.count(key))   replace_nil_and_resize(key, machine_variant_length * 2);
    }
}
```

Algorithm: compute three target lengths, each defaulting to 1. **process length** = `len(print_extruder_variant)` if present, else 1. **filament length** = `len(filament_extruder_variant)` if present, else 1. **machine length** = `len(printer_extruder_variant)` if present; else `len(nozzle_diameter)` if present; else 1. Then iterate every config key; if it is in one of the four sets, resize the vector. `printer_options_with_variant_2` (silent-mode machine limits) uses **2× machine length** because values are stored as `(normal, silent)` pairs.

Nil replacement only happens when `set_nil_to_default` is true AND the option's vector is `is_nil()` AND the key is NOT in `filament_extruder_override_keys`. In that case the vector is cleared and repopulated from `defaults.option(key)` at the target length. Otherwise the vector is resized in place, growing by repeating the last element (C++ `resize` with default-fill semantics). Nullable options serialize `nil` in JSON as the literal string `"nil"` in the array.

`filament_extruder_override_keys` (`PrintConfig.cpp` 62-83) — kept as-is, never substituted with defaults, because the user explicitly chose to override a per-extruder value at the filament level:

```
filament_retraction_length, filament_z_hop, filament_z_hop_types,
filament_retract_lift_above, filament_retract_lift_below, filament_retract_lift_enforce,
filament_retraction_speed, filament_deretraction_speed, filament_retract_restart_extra,
filament_retraction_minimum_travel, filament_wipe_distance,
filament_retract_when_changing_layer, filament_wipe, filament_retract_before_wipe,
filament_long_retractions_when_cut, filament_retraction_distances_when_cut
```

Nothing here uses `Preset::filament_options()` / `nozzle_options()` — routing is purely via the four `*_with_variant` sets.

## 2. `Preset::normalize`

`src/libslic3r/Preset.cpp` 370-415:

```cpp
void Preset::normalize(DynamicPrintConfig &config)
{
    size_t n = 1;
    if (config.option("single_extruder_multi_material") == nullptr || config.opt_bool("single_extruder_multi_material")) {
        auto* filament_diameter = dynamic_cast<const ConfigOptionFloats*>(config.option("filament_diameter"));
        if (filament_diameter != nullptr) { n = filament_diameter->values.size(); config.set_num_filaments((unsigned int)n); }
    } else {
        auto* nozzle_diameter = dynamic_cast<const ConfigOptionFloats*>(config.option("nozzle_diameter"));
        if (nozzle_diameter != nullptr) { n = nozzle_diameter->values.size(); config.set_num_extruders((unsigned int)n); }
    }
    if (config.option("filament_diameter") != nullptr) {
        const auto &defaults = FullPrintConfig::defaults();
        for (const std::string &key : Preset::filament_options()) {
            if (key == "compatible_prints" || key == "compatible_printers") continue;
            if (filament_options_with_variant.find(key) != filament_options_with_variant.end()) continue;
            auto *opt = config.option(key, false);
            if (opt != nullptr && opt->is_vector())
                static_cast<ConfigOptionVectorBase*>(opt)->resize(n, defaults.option(key));
        }
        for (const std::string &key : { "filament_settings_id" }) {
            auto *opt = config.option(key, false);
            if (opt != nullptr && opt->type() == coStrings)
                static_cast<ConfigOptionStrings*>(opt)->values.resize(n, std::string());
        }
    }
    handle_legacy_sla(config);
}
```

Algorithm: (1) pick `n` — if `single_extruder_multi_material` is missing or true (Bambu), `n = len(filament_diameter)`; else `n = len(nozzle_diameter)`. (2) If `filament_diameter` is present, iterate every key in `Preset::filament_options()`; skip `compatible_prints`/`compatible_printers` (which remain scalar string lists of profile names), skip keys already handled per-variant; for remaining vector keys present in config, resize to `n`, padding from `FullPrintConfig::defaults()`. (3) Force `filament_settings_id` (coStrings) length to `n`, padding with empty strings. (4) `handle_legacy_sla` — SLA legacy aliases, ignore.

Difference from `extend_default_config_length`: that one is **per-variant** (per print-extruder / filament-extruder / printer-extruder index) and does the `"nil"`→default substitution. `normalize` is **per-filament** (one value per logical filament slot), and assumes vectors are already materialised (non-nil).

Call chain during vendor preset load (`PresetBundle::load_vendor_configs_from_json`, `PresetBundle.cpp` 4060-4120):
1. `config = *default_config` (resolved inheritance parent, ultimately rooted at `FullPrintConfig::defaults()`)
2. `config.apply(config_src)` — overlay the JSON
3. `extend_default_config_length(config, true, *default_config)` — per-variant expand + nil substitution
4. Non-instantiable parents: `remove_invalid_keys` then exit
5. `Preset::normalize(config)` — per-filament expand
6. `Preset::remove_invalid_keys(config, *default_config)`

User preset import (`PresetBundle.cpp` 1090-1128) follows the same order: apply → `extend_default_config_length` (only when inheriting from a default) → `Preset::normalize` → `remove_invalid_keys`. Also called from `PresetBundle::load_config_file` at line 3383 after G-code config parsing.

## 3. Option lists

**`Preset::filament_options()`** returns static `s_Preset_filament_options` (`Preset.cpp` 960-998), ~110 hand-written keys. Contains ALL 15 of our targets plus all `filament_*` keys, all plate temps (`cool_plate_temp`, `textured_cool_plate_temp`, `eng_plate_temp`, `hot_plate_temp`, `textured_plate_temp`, `supertack_plate_temp`, each with `_initial_layer` variants), both `nozzle_temperature*`, the fan/cooling keys, the retract override keys, the ramming/cooling-move/toolchange keys, `filament_extruder_variant`, `filament_vendor`, and the compatibility fields (`compatible_prints`, `compatible_printers`, `inherits`). Source grep: `Preset.cpp:960-998`.

**`Preset::print_options()`** returns static `s_Preset_print_options` (`Preset.cpp` 890-958), ~300 keys. Includes `print_extruder_id` and `print_extruder_variant` which drive per-variant process expansion.

**`Preset::nozzle_options()`** returns `print_config_def.extruder_option_keys()`, initialised by `PrintConfigDef::init_extruder_option_keys` (`PrintConfig.cpp` 6771+) as a hand-written list — NOT a filter over `PrintConfigDef`:

```
extruder_type, nozzle_diameter, default_nozzle_volume_type, min_layer_height, max_layer_height,
extruder_offset, retraction_length, z_hop, z_hop_types, travel_slope,
retract_lift_above, retract_lift_below, retract_lift_enforce, retraction_speed, deretraction_speed,
retract_before_wipe, retract_restart_extra, retraction_minimum_travel, wipe, wipe_distance,
retract_when_changing_layer, retract_length_toolchange, retract_restart_extra_toolchange,
extruder_colour, default_filament_profile, retraction_distances_when_cut, long_retractions_when_cut
```

**`Preset::printer_options()`** = `s_Preset_printer_options` ++ `s_Preset_machine_limits_options` ++ `nozzle_options()`. SLA functions are irrelevant.

No `ConfigOptionDef` flag ever participates — classification is list membership only.

## 4. Per-filament / per-extruder classification

No flag on `ConfigOptionDef` classifies options. Classification is pure list membership:

- in `s_Preset_filament_options` → filament profile key (per-filament slot)
- in `s_Preset_print_options` → process profile key
- in `s_Preset_printer_options` / `machine_limits_options` / `extruder_option_keys` → printer profile key
- in a `*_with_variant` set → additionally expanded per-variant

No per-plate flag — plate-variant temperatures (`textured_cool_plate_temp`, etc.) are just per-filament `coInts`.

For the 15 target keys — ALL are per-filament (in `s_Preset_filament_options`, none in `filament_options_with_variant`). So `Preset::normalize` (not `extend_default_config_length`) is what resizes them. Default values come from `FullPrintConfig::defaults()`:

| key | type | default |
|---|---|---|
| `pressure_advance` | coFloats | `{ 0.02 }` |
| `adaptive_pressure_advance` | coBools | `{ false }` |
| `adaptive_pressure_advance_bridges` | coFloats | `{ 0.0 }` |
| `adaptive_pressure_advance_overhangs` | coBools | `{ false }` |
| `adaptive_pressure_advance_model` | coStrings | `{ "0,0,0\n0,0,0" }` |
| `activate_chamber_temp_control` | coBools | `{ false }` |
| `dont_slow_down_outer_wall` | coBools | `{ false }` |
| `enable_overhang_bridge_fan` | coBools | `{ true }` |
| `enable_pressure_advance` | coBools | `{ false }` |
| `idle_temperature` | coInts | `{ 0 }` |
| `internal_bridge_fan_speed` | coInts | `{ -1 }` |
| `ironing_fan_speed` | coInts | `{ -1 }` |
| `support_material_interface_fan_speed` | coInts | `{ -1 }` |
| `textured_cool_plate_temp` | coInts | `{ 40 }` |
| `textured_cool_plate_temp_initial_layer` | coInts | `{ 35 }` |

Representative blocks from `PrintConfig.cpp`:

```cpp
def = this->add("textured_cool_plate_temp", coInts);                 // L888
def->min = 0; def->max = 300; def->set_default_value(new ConfigOptionInts{ 40 });

def = this->add("enable_overhang_bridge_fan", coBools);              // L1114
def->set_default_value(new ConfigOptionBools{ true });

def = this->add("pressure_advance", coFloats);                       // L2171
def->set_default_value(new ConfigOptionFloats { 0.02 });

def = this->add("idle_temperature", coInts);                         // L6528
def->min = 0; def->max = max_temp; def->set_default_value(new ConfigOptionInts{ 0 });

def = this->add("nozzle_temperature", coInts);                       // L6117
def->set_default_value(new ConfigOptionInts { 200 });

def = this->add("filament_diameter", coFloats);                      // L2420
def->set_default_value(new ConfigOptionFloats { 1.75 });

def = this->add("filament_colour", coStrings);                       // L2281
def->set_default_value(new ConfigOptionStrings{ "#F2754E" });

def = this->add("nozzle_diameter", coFloats);                        // L4392
def->set_default_value(new ConfigOptionFloats { 0.4 });
```

## 5. Default values source

`defaults` at the vendor load call site (`PresetBundle.cpp` 4079) is the fully-resolved parent config — either the inherited preset's config or the collection's root default preset. The root default is built from `FullPrintConfig::defaults()` (`PresetBundle.cpp` 239-243), a `StaticPrintConfig` populated by `PrintConfigDef` initialisation (~1000 entries in `PrintConfig.cpp`).

Conclusion: defaults cannot be reconstructed from vendor JSON, because every inheritance chain silently roots at `FullPrintConfig::defaults()`. Keys that no vendor ever overrides still have CLI-consulted defaults. For our port we must embed a Python dict of defaults for the keys we care about — realistically only the 15 target process-customisation keys, using the values in §4.

## 6. Sentinel keys in sample profiles

| file | `nozzle_diameter` | `filament_diameter` | `filament_extruder_variant` | `print_extruder_variant` | `printer_extruder_variant` |
|---|---|---|---|---|---|
| `BBL/machine/Bambu Lab A1 mini 0.4 nozzle.json` | `["0.4"]` (len 1) | absent | absent | absent | absent |
| `BBL/process/0.16mm High Quality @BBL A1M.json` | absent | absent | absent | absent | absent |
| `BBL/filament/Bambu PLA Basic @BBL A1M.json` | absent | absent | absent | absent | absent |

Leaf profiles inherit common parents (`fdm_bbl_3dp_001_common`, `fdm_process_single_0.16`, `Bambu PLA Basic @base`); sentinels live in the ancestors, not the leaves. Per-variant sentinels are absent for single-extruder BBL printers, so all three lengths collapse to 1 (or `nozzle_diameter` length = 1 for machine). `filament_diameter` comes from the filament chain. Our port can assume `n_filaments = number_of_filament_profiles_selected` and resize the 15 target keys to that length.

## 7. `Preset::remove_invalid_keys`

`src/libslic3r/Preset.cpp` 417-431:

```cpp
std::string Preset::remove_invalid_keys(DynamicPrintConfig &config, const DynamicPrintConfig &default_config)
{
    std::string incorrect_keys;
    for (const std::string &key : config.keys())
        if (! default_config.has(key)) {
            if (incorrect_keys.empty()) incorrect_keys = key;
            else { incorrect_keys += ", "; incorrect_keys += key; }
            config.erase(key);
        }
    return incorrect_keys;
}
```

For every key in `config`, if the reference `default_config` has no such key, erase it and append its name to the returned error string. Called immediately after `Preset::normalize` during vendor preset load (`PresetBundle.cpp` 4113), after `Preset::normalize` on user import (1123), and inside the non-instantiable branch (4082).

Port relevance is low — the `orca-slicer` CLI runs this itself on the JSON we hand it. We only need to replicate it if we want to suppress CLI warnings.
