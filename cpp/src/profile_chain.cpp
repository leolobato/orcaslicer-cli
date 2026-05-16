#include "profile_chain.h"

#include "libslic3r/PrintConfig.hpp"

#include <filesystem>
#include <map>
#include <stdexcept>
#include <utility>

namespace orca_headless {

// Load every ``.json`` file from ``dir_path`` into ``coll`` via
// ``PresetCollection::load_preset``. Each link in the inheritance closure
// (leaf + ancestors) is its own JSON; the bundle's parent lookup then
// chases ``inherits`` by name.
//
// ``load_inherits_to_config=true`` is critical: it keeps the ``inherits``
// field inside the resulting DynamicPrintConfig so
// ``Preset::inherits()`` (Preset.hpp:299, reads ``config["inherits"]``)
// returns the parent's name. Without it ``get_selected_preset_parent()``
// silently falls through to ``default_preset()`` and
// ``dirty_options_without_option_list`` reports every key as customized.
size_t load_chain_dir_into(
    Slic3r::PresetCollection& coll,
    const std::string& dir_path) {
    namespace fs = std::filesystem;
    if (dir_path.empty() || !fs::is_directory(dir_path)) {
        throw std::runtime_error("chain dir is not a directory: " + dir_path);
    }

    // Mirrors the GUI's per-subfile load sequence at
    // PresetBundle.cpp:4077-4103 (parse_subfile, called from
    // load_vendor_configs_from_json):
    //
    //   config = *default_config;           // parent's full config OR
    //                                       // collection's default_preset
    //   config.apply(config_src);           // overlay this file's deltas
    //   extend_default_config_length(...);  // resize per-variant vectors
    //   Preset::normalize(config);          // pad per-filament vectors
    //
    // Our chain JSONs are pre-flattened in Python (resolve_profile_by_name
    // does the recursive merge), so we don't walk the chain in C++ — but
    // we still need the GUI's "fill missing keys from defaults" step.
    // Otherwise multi-filament slicing SIGSEGVs in full_fff_config's
    // per-slot vector merge (PresetBundle.cpp:3201-3236), which iterates
    // every key in `filaments.default_preset().config` and dereferences
    // each slot's `filament_temp_configs[i].option(key)` — null pointer
    // for any missing key crashes `opt_vec_dst->set(...)`. Vendor JSONs
    // commonly omit options like `filament_self_index` and
    // `pellet_flow_coefficient`; the GUI fills them via the parent-chain
    // copy at parse_subfile:4077.
    //
    // Use the collection's own default_preset as the "fill" source —
    // that's what parse_subfile does at line 4075 (`default_preset()`)
    // for non-printer types. Printer uses default_preset_for(config_src)
    // but for our case (no SLA) the result is the same default preset.
    const Slic3r::DynamicPrintConfig& defaults = coll.default_preset().config;

    size_t loaded = 0;
    for (const auto& entry : fs::directory_iterator(dir_path)) {
        if (!entry.is_regular_file()) continue;
        if (entry.path().extension() != ".json") continue;
        const std::string path = entry.path().string();

        std::map<std::string, std::string> kv;
        Slic3r::DynamicPrintConfig config_src;
        Slic3r::ConfigSubstitutionContext ctx(
            Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
        std::string reason;
        config_src.load_from_json(
            path, ctx, /*load_inherits_to_config=*/true, kv, reason);

        // Step 1+2: layer JSON over defaults (parse_subfile:4077-4078).
        Slic3r::DynamicPrintConfig cfg;
        cfg.apply(defaults);
        cfg.apply(config_src);

        // Step 3: resize per-variant vectors (parse_subfile:4079).
        Slic3r::extend_default_config_length(
            cfg, /*set_nil_to_default=*/true, defaults);

        const std::string name =
            (kv.count("name") && !kv["name"].empty())
                ? kv["name"]
                : entry.path().stem().string();
        Slic3r::Preset& preset = coll.load_preset(
            path, name, std::move(cfg), /*select=*/false);
        if (auto it = kv.find("filament_id"); it != kv.end()) {
            preset.filament_id = it->second;
        }
        if (auto it = kv.find("setting_id"); it != kv.end()) {
            preset.setting_id = it->second;
        }
        // Mark vendor-bundle chain links as system presets. The Python
        // wrapper resolves the inheritance chain against PROFILES_DIR
        // (the bundled BBL/system catalog) — every link we write here
        // maps to a preset that the GUI loads via `load_vendor_configs_from_json`
        // with `is_system=true`. Without this flag,
        // `PresetCollection::get_preset_base` (Preset.cpp:2650) walks
        // `inherits` to the common ancestor (e.g. `fdm_bbl_3dp_001_common`)
        // and `get_selected_preset_parent` (Preset.cpp:2595) returns
        // that ancestor — so the export-time diff
        // (`PresetBundle::export_config_3mf`, PresetBundle.cpp:3081-3258)
        // reports every key the leaf legitimately overrides, bloating
        // `different_settings_to_system` to ~30 keys per filament + 30
        // printer keys. With `is_system=true`, `get_preset_base(leaf) ==
        // &leaf` → parent for diff is self → diff = only the smart-
        // transfer overlay applied to the edited preset, matching GUI.
        // The filament-specific code path at PresetBundle.cpp:3158-3168
        // has the same branch on `is_system`.
        preset.is_system = true;
        // Step 4: Preset::normalize (parse_subfile:4103).
        // Safe on all three preset types — its set_num_filaments call
        // (Preset.cpp:379) is gated on filament_diameter being present,
        // which only filament configs have. Printer/process configs make
        // it a no-op.
        Slic3r::Preset::normalize(preset.config);
        ++loaded;
    }
    return loaded;
}

}  // namespace orca_headless
