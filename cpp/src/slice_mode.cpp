#include "slice_mode.h"
#include "progress.h"

#include "libslic3r/Model.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Print.hpp"
#include "libslic3r/PrintBase.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/Utils.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <optional>
#include <set>
#include <sstream>

#include <boost/log/trivial.hpp>
#include <nlohmann/json.hpp>

namespace orca_headless {

namespace {

// Filter applied to ``threemf_config`` before it becomes
// ``bundle.project_config``. Mirrors the GUI's ``s_project_options``
// (vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:37-52) — only these
// keys are project-scoped; everything else in the 3MF's
// project_settings.config belongs to the printer/process/filament presets
// and would corrupt cross-printer slices if applied wholesale.
static const std::vector<std::string> s_project_options{
    "flush_volumes_vector", "flush_volumes_matrix",
    "filament_colour", "filament_colour_type", "filament_multi_colour",
    "wipe_tower_x", "wipe_tower_y", "wipe_tower_rotation_angle",
    "curr_bed_type", "flush_multiplier",
    "nozzle_volume_type", "filament_map_mode", "filament_map",
};

// Keys we ignore when diffing edited vs parent for the
// ``settings_transfer`` response — these are inheritance/identity
// metadata, not user customizations.
static const std::set<std::string> s_dirty_diff_ignore{
    "compatible_printers", "compatible_prints",
    "compatible_printers_condition", "compatible_prints_condition",
    "inherits", "print_settings_id", "filament_settings_id",
    "printer_settings_id",
};

// Printer-slot blocklist for 3MF override application: keys whose values
// describe per-extruder topology (vector layouts indexed by extruder
// count). The 3MF's ``different_settings_to_system[printer_slot]`` may
// declare them when the authoring printer had a different topology
// (e.g. P-series multi-extruder values exported into a project sliced
// for an A1 mini), and overlaying them onto our resolved machine config
// recreates the SIGSEGV class — ``update_values_to_printer_extruders``
// dereferences out of bounds when the size doesn't match the active
// machine's extruder count.
static const std::set<std::string> s_printer_slot_blocklist{
    "extruder_variant_list",
    "printer_extruder_variant",
    "printer_extruder_id",
    "extruder_type",
    "nozzle_volume_type",
    "filament_extruder_variant",
    "filament_self_index",
    "extruder_ams_count",
};

// Split a semicolon-delimited key list (the format
// ``different_settings_to_system`` slots use).
std::vector<std::string> split_semicolons(const std::string& s) {
    std::vector<std::string> out;
    size_t start = 0;
    for (size_t i = 0; i <= s.size(); ++i) {
        if (i == s.size() || s[i] == ';') {
            if (i > start) out.emplace_back(s, start, i - start);
            start = i + 1;
        }
    }
    return out;
}

// Overlay the keys named in ``key_list`` from ``src`` onto ``dst``.
// Used to apply 3MF process- and printer-slot customizations onto the
// final config — the bundle's ``load_project_embedded_presets`` only
// handles filament variants. Without this pass, project-customized
// process keys (``layer_height``, ``bridge_speed``, etc.) revert to
// the system preset's defaults and the slice diverges meaningfully
// from the GUI.
//
// Returns the list of keys actually transferred.
std::vector<std::string> apply_threemf_slot_overrides(
    Slic3r::DynamicPrintConfig& dst,
    const Slic3r::DynamicPrintConfig& src,
    const std::string& key_list,
    bool exclude_filament_keys,
    const std::set<std::string>& excluded_keys) {
    auto starts_with = [](const std::string& s, const char* prefix) {
        const size_t n = std::strlen(prefix);
        return s.size() >= n && std::memcmp(s.data(), prefix, n) == 0;
    };
    auto ends_with = [](const std::string& s, const char* suffix) {
        const size_t n = std::strlen(suffix);
        return s.size() >= n &&
            std::memcmp(s.data() + s.size() - n, suffix, n) == 0;
    };
    std::vector<std::string> transferred;
    for (const auto& key : split_semicolons(key_list)) {
        if (key == "compatible_printers" || key == "compatible_prints") continue;
        if (exclude_filament_keys &&
            (starts_with(key, "filament_") || ends_with(key, "_filament"))) {
            continue;
        }
        if (excluded_keys.count(key) != 0) continue;
        const Slic3r::ConfigOption* src_opt = src.option(key);
        if (src_opt == nullptr) continue;
        Slic3r::ConfigOption* dst_opt = dst.option(key, /*create=*/false);
        if (dst_opt == nullptr) continue;
        dst_opt->set(src_opt);
        transferred.push_back(key);
    }
    return transferred;
}

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
    Slic3r::PresetCollection& coll, const std::string& dir_path) {
    namespace fs = std::filesystem;
    if (dir_path.empty() || !fs::is_directory(dir_path)) {
        throw std::runtime_error("chain dir is not a directory: " + dir_path);
    }
    size_t loaded = 0;
    for (const auto& entry : fs::directory_iterator(dir_path)) {
        if (!entry.is_regular_file()) continue;
        if (entry.path().extension() != ".json") continue;
        const std::string path = entry.path().string();

        std::map<std::string, std::string> kv;
        Slic3r::DynamicPrintConfig cfg;
        Slic3r::ConfigSubstitutionContext ctx(
            Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
        std::string reason;
        cfg.load_from_json(
            path, ctx, /*load_inherits_to_config=*/true, kv, reason);

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
        // PresetCollection::load_preset (Preset.cpp:2291-2316) does NOT
        // call Preset::normalize. For FILAMENT collections we run it
        // explicitly to pad per-filament-vector keys to filament_diameter's
        // length so that full_fff_config's per-slot merge
        // (PresetBundle.cpp:3217-3220) doesn't nullptr-deref on sparse
        // user-imported filament JSONs.
        //
        // Do NOT call Preset::normalize on PRINTER or PROCESS presets:
        // its single_extruder_multi_material branch (Preset.cpp:373-379)
        // calls config.set_num_filaments(1) on those configs when
        // filament_diameter is absent, which truncates per-filament-vector
        // keys (filament_extruder_variant, filament_self_index, etc.) on
        // a multi-filament slice and SIGSEGVs in full_fff_config's
        // per-slot vector-merge loop.
        if (coll.type() == Slic3r::Preset::TYPE_FILAMENT) {
            Slic3r::Preset::normalize(preset.config);
        }
        ++loaded;
    }
    return loaded;
}

// Center the combined instance bounding box on the build plate. Mirrors
// `Model::center_instances_around_point`, which is how the GUI's "fit to
// plate" path reseats objects.
void recenter_on_plate(Slic3r::Model& model,
                       const Slic3r::DynamicPrintConfig& cfg) {
    const auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area");
    if (!area || area->values.size() < 3) return;
    double min_x = area->values[0].x(), max_x = min_x;
    double min_y = area->values[0].y(), max_y = min_y;
    for (const auto& p : area->values) {
        min_x = std::min(min_x, p.x()); max_x = std::max(max_x, p.x());
        min_y = std::min(min_y, p.y()); max_y = std::max(max_y, p.y());
    }
    Slic3r::Vec2d center((min_x + max_x) / 2.0, (min_y + max_y) / 2.0);
    model.center_instances_around_point(center);
    for (auto* obj : model.objects) {
        if (!obj) continue;
        obj->ensure_on_bed(/*allow_negative_z=*/false);
    }
}

int fail(const std::string& code, const std::string& message,
         SliceResponse& r) {
    r.status = "error";
    r.error_code = code;
    r.error_message = message;
    write_slice_response_to_stdout(r);
    return 1;
}

}  // namespace

int run_slice_mode(const SliceRequest& req) {
    SliceResponse response;
    response.output_3mf = req.output_3mf;

    // libslic3r writes backup files to temporary_dir() during 3MF reads.
    // On a fresh container the global isn't initialized; default it to the
    // platform's temp dir before touching Model::read_from_file.
    if (Slic3r::temporary_dir().empty()) {
        std::error_code ec;
        std::filesystem::path tmp = std::filesystem::temp_directory_path(ec);
        if (ec || tmp.empty()) tmp = "/tmp";
        Slic3r::set_temporary_dir(tmp.string());
    }

    emit_progress("loading_3mf", 0);

    // 1. Load the input 3MF as a project — pulls the model, bundled config,
    //    plate data, and project_presets all in one call. This is the same
    //    entry the GUI uses (Plater::priv::load_files for .3mf with project).
    Slic3r::DynamicPrintConfig threemf_config;
    Slic3r::ConfigSubstitutionContext subs_ctx(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    Slic3r::PlateDataPtrs plate_data;
    std::vector<Slic3r::Preset*> project_presets;

    Slic3r::Model model;
    try {
        model = Slic3r::Model::read_from_file(
            req.input_3mf,
            &threemf_config,
            &subs_ctx,
            Slic3r::LoadStrategy::LoadModel
                | Slic3r::LoadStrategy::LoadConfig
                | Slic3r::LoadStrategy::LoadAuxiliary,
            &plate_data,
            &project_presets);
    } catch (const std::exception& e) {
        return fail("invalid_3mf",
                    std::string("read_from_file: ") + e.what(), response);
    }

    if (model.objects.empty()) {
        return fail("empty_model", "loaded 3MF has no objects", response);
    }

    // Match GUI's PresetBundle.cpp:3528-3529 cleanup: drop
    // ``extruder_ams_count`` from the parsed 3MF config so no later code
    // path can read stale data sized for the authoring printer's topology.
    threemf_config.erase("extruder_ams_count");

    emit_progress("setting_up_bundle", 5);

    // 2. Stand up a real PresetBundle. The constructor is GUI-free
    //    (vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:238-308 — no
    //    AppConfig, wxApp, or Semver globals). We feed it the
    //    inheritance closure for the request — Python pre-resolved each
    //    chain link as its own flat JSON (with `inherits` preserved) into
    //    a per-category directory. The bundle then chases parents by name
    //    via `get_selected_preset_parent`, exactly like the GUI does.
    Slic3r::PresetBundle bundle;

    try {
        load_chain_dir_into(bundle.printers, req.machine_chain_dir);
        load_chain_dir_into(bundle.prints, req.process_chain_dir);
        load_chain_dir_into(bundle.filaments, req.filament_chain_dir);
    } catch (const std::exception& e) {
        return fail("invalid_profile",
                    std::string("load chain dir: ") + e.what(), response);
    }

    // 3. Select leaf presets. select_preset_by_name sets both the
    //    "selected" index and the "edited" working copy (Preset.cpp:3091).
    //    full_fff_config reads `prints.get_edited_preset()` so this is the
    //    handle that drives the final config.
    if (!bundle.printers.select_preset_by_name(req.machine_leaf_name, true)) {
        return fail("invalid_profile",
                    "machine leaf '" + req.machine_leaf_name +
                        "' not found in chain dir", response);
    }
    if (!bundle.prints.select_preset_by_name(req.process_leaf_name, true)) {
        return fail("invalid_profile",
                    "process leaf '" + req.process_leaf_name +
                        "' not found in chain dir", response);
    }

    // For multi-filament slicing, full_fff_config (PresetBundle.cpp:3128)
    // iterates `bundle.filament_presets` (a vector<string> of per-slot
    // names) and calls `find_preset(name)` for each. Single-filament
    // (PresetBundle.cpp:3098) reads `filaments.get_edited_preset()`
    // instead — so we set BOTH paths: select the slot-0 filament as the
    // edited preset, and populate the per-slot vector.
    if (req.filament_settings_id.empty()) {
        return fail("invalid_request",
                    "filament_settings_id must list at least one slot",
                    response);
    }
    if (!bundle.filaments.select_preset_by_name(
            req.filament_settings_id.front(), true)) {
        return fail("invalid_profile",
                    "filament leaf '" + req.filament_settings_id.front() +
                        "' not found in chain dir", response);
    }
    bundle.filament_presets = req.filament_settings_id;

    // 4. Absorb the 3MF's project-local preset variants. After this the
    //    filament collection has both system presets (loaded from chain
    //    dir) and any project-local variants from the 3MF (e.g. "Bambu
    //    PLA Basic @BBL A1M(my-project.3mf)" — a snapshot of the system
    //    preset with the user's slot-1 customizations baked in).
    //
    //    PresetCollection::load_project_embedded_presets
    //    (Preset.cpp:1515-1598) stitches each variant: copies its parent's
    //    full config, then overlays the variant's deltas. Result is a flat
    //    Preset in the collection, findable by name.
    try {
        bundle.load_project_embedded_presets(
            project_presets,
            Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    } catch (const std::exception& e) {
        return fail("invalid_3mf",
                    std::string("load_project_embedded_presets: ") + e.what(),
                    response);
    }

    // 5. Replace per-slot filament names with the 3MF's project-local
    //    variant when the user's selection matches its base. This is what
    //    PresetBundle::load_3mf_file does in the GUI: when slot N's name
    //    in the 3MF is a project-local variant, that variant goes into
    //    `filament_presets[N]` so its (system_base + 3MF_deltas) config
    //    is what flows into full_fff_config — not the bare system preset.
    //
    //    When the user genuinely swapped to a different filament, the
    //    base mismatches and we keep the user's choice; the variant's
    //    customizations are reported as "filament_changed" / discarded
    //    in the settings_transfer response.
    //    Per-slot status semantics (mirrors the OLD apply_overrides_for_slot
    //    name guard, deleted in d730c52 and partially restored here):
    //      "no_customizations"  — 3MF didn't author this slot or named a
    //                             filament we can't resolve; nothing to apply.
    //      "applied"            — user's pick matches the 3MF's author choice
    //                             (either by exact name or via project-local
    //                             variant inheriting from user's pick); the
    //                             per-filament-slot 3MF override pass below
    //                             will overlay the customized keys.
    //      "filament_changed"   — user swapped to a different filament base;
    //                             3MF customizations don't apply to the new
    //                             filament, report the discarded key list.
    nlohmann::json filament_slot_status = nlohmann::json::array();
    {
        const auto* threemf_names =
            threemf_config.option<Slic3r::ConfigOptionStrings>(
                "filament_settings_id", false);
        for (size_t i = 0; i < bundle.filament_presets.size(); ++i) {
            const std::string& selected = bundle.filament_presets[i];
            const std::string original =
                (threemf_names != nullptr && i < threemf_names->values.size())
                    ? threemf_names->values[i] : "";
            nlohmann::json entry;
            entry["slot"] = i;
            entry["original_filament"] = original;
            entry["selected_filament"] = selected;
            entry["transferred"] = nlohmann::json::array();
            entry["discarded"] = nlohmann::json::array();

            if (original.empty()) {
                entry["status"] = "no_customizations";
                filament_slot_status.push_back(entry);
                continue;
            }
            // Exact name match — user picked the same filament the 3MF
            // authored with. Per-slot customizations apply directly.
            if (original == selected) {
                entry["status"] = "applied";
                filament_slot_status.push_back(entry);
                continue;
            }
            // Names differ. Could be a project-local variant
            // ("Bambu PLA Basic @BBL A1M(my-project.3mf)") whose
            // inherits points at the user's pick — in which case we
            // route the variant into bundle.filament_presets[i] so its
            // pre-stitched config (parent + deltas, applied during
            // load_project_embedded_presets) is what full_config reads.
            const Slic3r::Preset* variant =
                bundle.filaments.find_preset(original, false, true);
            if (variant != nullptr && variant->inherits() == selected) {
                bundle.filament_presets[i] = original;
                entry["status"] = "applied";
                filament_slot_status.push_back(entry);
                continue;
            }
            // Genuine filament swap: customizations don't apply. Report
            // the discarded keys (the variant's deltas vs its base) so
            // the client can surface what was dropped.
            if (variant != nullptr) {
                const Slic3r::Preset* variant_parent =
                    bundle.filaments.find_preset(variant->inherits(), false, true);
                if (variant_parent != nullptr) {
                    auto discarded = bundle.filaments.dirty_options_without_option_list(
                        variant, variant_parent, s_dirty_diff_ignore, false);
                    entry["discarded"] = discarded;
                }
            }
            entry["status"] = "filament_changed";
            filament_slot_status.push_back(entry);
        }
    }

    // 6. Filter the 3MF's project_settings.config to project-only keys
    //    (s_project_options whitelist) and feed it to bundle.project_config.
    //    full_fff_config does `out.apply(this->project_config)` after the
    //    process/filament/printer merge (PresetBundle.cpp:3047).
    bundle.project_config.apply_only(threemf_config, s_project_options);

    // 7. Sync flush-volume / wipe-tower vectors for the active filament
    //    count and machine topology. Replaces our old hand-rolled
    //    resize_flush_volumes_for_topology — bundle does this via
    //    PresetBundle.cpp:4294 (matrix + vector + multiplier all aligned
    //    to current `filament_presets.size()` and printer's nozzle count).
    bundle.update_multi_material_filament_presets();

    emit_progress("composing_config", 20);

    // 8. Compose the final config via the GUI's authoritative path.
    //    full_fff_config (PresetBundle.cpp:3039) does:
    //      defaults → process → filament_default → printer → project_config
    //    then layers per-slot filament configs on top, runs
    //    update_values_to_printer_extruders for variant-aware keys, and
    //    populates filament_ids / different_settings vectors. The whole
    //    pipeline our old code emulated piecemeal.
    Slic3r::DynamicPrintConfig final_cfg;
    try {
        // full_config is the public wrapper that dispatches to full_fff_config
        // for FFF printers (PresetBundle.cpp:3013-3018). full_fff_config itself
        // is private. Same call signature; same behavior for our case.
        final_cfg = bundle.full_config(
            /*apply_extruder=*/true,
            /*filament_maps=*/std::nullopt);
    } catch (const std::exception& e) {
        return fail("compose_failed",
                    std::string("full_config: ") + e.what(), response);
    }
    emit_progress("config_composed", 21);

    // 9. Apply the 3MF's process- and printer-slot customizations on top
    //    of the bundle's full_config output. The bundle's
    //    ``load_project_embedded_presets`` only handles filament variants
    //    (it absorbs project-local Preset objects from the 3MF). Process
    //    and printer customizations live in the 3MF's project_settings.config
    //    body — ``threemf_config`` here — and the
    //    ``different_settings_to_system`` fingerprint lists which keys
    //    were customized. Layout: ``[process, filament_0, …, filament_{N-1}, printer]``.
    //
    //    Without this pass, project-customized process keys (e.g.
    //    layer_height, bridge_speed, default_acceleration) revert to the
    //    system preset's defaults and the slice diverges meaningfully
    //    from the GUI (verified on fixture 01: GUI's layer_height = 0.25
    //    customization was lost, slice ran 53% slower with default 0.20).
    std::vector<std::string> process_override_keys;
    std::vector<std::string> printer_override_keys;
    if (const auto* fp = threemf_config.option<Slic3r::ConfigOptionStrings>(
            "different_settings_to_system", false);
        fp != nullptr && !fp->values.empty()) {
        // Process slot (index 0): exclude filament-like keys (they belong
        // to filament slots even when listed under process).
        process_override_keys = apply_threemf_slot_overrides(
            final_cfg, threemf_config, fp->values[0],
            /*exclude_filament_keys=*/true,
            /*excluded_keys=*/{});
        emit_progress("process_overrides_applied", 22);

        // Per-filament slots (indices 1..N): apply only when the slot's
        // name guard from step 5 said "applied" (project-local variant
        // matched user's pick, or no project-local but the 3MF and user
        // agree on the filament). For "filament_changed" slots, the
        // 3MF's customization referenced a different filament's defaults
        // and we leave it discarded. For "no_customizations", the apply
        // is idempotent (vector already matches threemf_config) but we
        // skip it for clarity.
        //
        // Most multi-filament 3MFs ALSO embed a project-local preset for
        // each customized slot, which load_project_embedded_presets
        // already stitched into the bundle. For those, this overlay is
        // idempotent (same values). For 3MFs that list per-slot
        // customizations in the fingerprint without an embedded preset
        // (rare; the GUI only writes the fingerprint when it also
        // creates a project-local preset), this overlay is what carries
        // the override through.
        const size_t num_filament_slots =
            fp->values.size() >= 2 ? fp->values.size() - 2 : 0;
        for (size_t i = 0; i < num_filament_slots && i < filament_slot_status.size(); ++i) {
            if (filament_slot_status[i]["status"] != "applied") continue;
            const std::string& key_list = fp->values[i + 1];
            if (key_list.empty()) continue;
            auto transferred = apply_threemf_slot_overrides(
                final_cfg, threemf_config, key_list,
                /*exclude_filament_keys=*/false,
                /*excluded_keys=*/{});
            // Merge into the slot status — step 5 may have already
            // captured the variant's deltas via dirty_options; union
            // them with what we overlaid here so the response reflects
            // the full set of keys that landed.
            std::set<std::string> seen;
            for (const auto& k : filament_slot_status[i]["transferred"]) {
                seen.insert(k.get<std::string>());
            }
            for (const auto& k : transferred) {
                if (seen.insert(k).second) {
                    filament_slot_status[i]["transferred"].push_back(k);
                }
            }
        }

        emit_progress("filament_overrides_applied", 23);

        // Printer slot (last): no name guard — machine is fixed by the
        // request. Apply with the topology blocklist to avoid SIGSEGVs
        // from per-extruder vector mismatches.
        if (fp->values.size() >= 2) {
            printer_override_keys = apply_threemf_slot_overrides(
                final_cfg, threemf_config, fp->values.back(),
                /*exclude_filament_keys=*/false,
                /*excluded_keys=*/s_printer_slot_blocklist);
        }
        emit_progress("printer_overrides_applied", 24);
    }

    // 10. Build the settings_transfer response. Process and printer keys
    //     come from the override pass above; filament_slots was populated
    //     during step 5 (project-local variant detection).
    nlohmann::json transfer_status = nlohmann::json::object();
    {
        const bool any_filament_applied = std::any_of(
            filament_slot_status.begin(), filament_slot_status.end(),
            [](const nlohmann::json& e) { return e["status"] == "applied"; });
        const bool any =
            !process_override_keys.empty() ||
            !printer_override_keys.empty() ||
            any_filament_applied;
        transfer_status["status"] = any ? "applied" : "no_customizations";
        transfer_status["process_keys"] = process_override_keys;
        transfer_status["printer_keys"] = printer_override_keys;
        transfer_status["filament_slots"] = filament_slot_status;
    }

    // curr_bed_type lives in the 3MF's project_settings.config but isn't
    // listed in different_settings_to_system. It's a project-level field
    // libslic3r reads to pick which <plate>_temp keys drive bed-temp
    // gcode (GCode.cpp:2116/2580/2937). The s_project_options whitelist
    // already routes it through bundle.project_config; the additional
    // step here is the caller-supplied override (e.g. user re-picked the
    // plate in the GUI), which takes precedence over whatever the input
    // 3MF authored. Mirrors `Plater::on_change_bed_type`.
    if (!req.plate_type.empty()) {
        try {
            Slic3r::ConfigSubstitutionContext ctxt{
                Slic3r::ForwardCompatibilitySubstitutionRule::Disable};
            final_cfg.set_deserialize("curr_bed_type", req.plate_type, ctxt);
            transfer_status["curr_bed_type"] = req.plate_type;
        } catch (const std::exception& e) {
            return fail(
                "invalid_plate_type",
                std::string("plate_type=\"") + req.plate_type + "\" is not a "
                    "valid OrcaSlicer bed type for this machine: " + e.what(),
                response);
        }
    } else if (const auto* opt = final_cfg.option("curr_bed_type"); opt != nullptr) {
        transfer_status["curr_bed_type"] = opt->serialize();
    }

    response.settings_transfer = transfer_status;

    // 10. Wire AMS / filament selection metadata onto the final config so
    //     libslic3r threads it into slice_info.config + gcode metadata.
    if (!req.filament_map.empty()) {
        auto* opt = final_cfg.opt<Slic3r::ConfigOptionInts>("filament_map", true);
        opt->values = req.filament_map;
    }
    {
        auto* opt = final_cfg.opt<Slic3r::ConfigOptionStrings>(
            "filament_settings_id", true);
        opt->values = req.filament_settings_id;
    }

    // 11. Recenter / drop-to-bed (GUI does this on every load).
    if (req.recenter) {
        emit_progress("recentering", 25);
        try {
            recenter_on_plate(model, final_cfg);
        } catch (const std::exception& e) {
            return fail("recenter_failed", std::string("recenter: ") + e.what(),
                        response);
        }
    } else {
        // Drop any model the 3MF saved hovering above (or buried below) z=0
        // onto the bed. Without this, libslic3r's skirt/brim generator throws
        // "Coordinate outside allowed range" when the printable-area polygon
        // is intersected against a model whose instance offset puts it
        // outside the bed in Z.
        for (auto* obj : model.objects) {
            if (!obj) continue;
            obj->ensure_on_bed(/*allow_negative_z=*/false);
        }
    }

    emit_progress("slicing_construct_print", 28);

    // 12. Configure the Print and run process(). BBL-printer flag controls
    //     output formatting (CONFIG_BLOCK markers, label_object tagging).
    Slic3r::Print print;
    print.restart();
    print.is_BBL_printer() = true;

    emit_progress("slicing_apply", 30);
    try {
        print.apply(model, final_cfg);
    } catch (const std::exception& e) {
        return fail("apply_failed",
                    std::string("Print::apply: ") + e.what(), response);
    }

    // Run the GUI's pre-slice validation gate. Mirrors the GUI's
    // BackgroundSlicingProcess::start_internal path which calls
    // Print::validate() before Print::process(). validate() invokes
    // sequential_print_clearance_valid() (Print.cpp:1222) when
    // print_sequence == ByObject && objects.size() > 1, which sets
    // ModelInstance::arrange_order = k+1 per instance (Print.cpp:881).
    // Without that side effect, GCode export's
    // sort_object_instances_by_model_order (GCode.cpp:2324-2363) uses
    // the default arrange_order = 0 for every instance and std::lower_bound
    // deduplicates them — only ONE instance reaches the iteration vector.
    emit_progress("slicing_validate", 31);
    {
        Slic3r::StringObjectException validate_warning;
        Slic3r::StringObjectException validate_err;
        try {
            validate_err = print.validate(&validate_warning, nullptr, nullptr);
        } catch (const std::exception& e) {
            return fail("validate_failed",
                        std::string("Print::validate: ") + e.what(),
                        response);
        }
        if (!validate_err.string.empty()) {
            return fail("validate_failed", validate_err.string, response);
        }
        if (!validate_warning.string.empty()) {
            BOOST_LOG_TRIVIAL(info)
                << "Print::validate warning: " << validate_warning.string;
        }
    }

    emit_progress("slicing_callback", 32);
    print.set_status_callback(
        [](const Slic3r::PrintBase::SlicingStatus& status) {
            int pct = 30 + static_cast<int>(status.percent * 0.6);
            emit_progress(status.text, pct);
        });

    emit_progress("slicing_process", 35);
    try {
        print.process();
    } catch (const std::exception& e) {
        return fail("slice_failed",
                    std::string("Print::process: ") + e.what(), response);
    }

    emit_progress("exporting_gcode", 90);

    // 13. Export gcode to a temp file. store_bbs_3mf reads the gcode bytes
    //     from PlateData.gcode_file when SaveStrategy::WithGcode is set.
    const std::filesystem::path temp_gcode_path =
        std::filesystem::temp_directory_path() /
        ("orca-headless-gcode-" + std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count()) + ".gcode");

    Slic3r::GCodeProcessorResult gcode_result;
    try {
        print.export_gcode(temp_gcode_path.string(), &gcode_result, nullptr);
    } catch (const std::exception& e) {
        return fail("gcode_export_failed",
                    std::string("export_gcode: ") + e.what(), response);
    }

    emit_progress("writing_3mf", 95);

    // 14. Build single-plate PlateData. Mirrors PartPlateList::store_to_3mf_structure.
    auto* plate = new Slic3r::PlateData();
    plate->plate_index = std::max(0, req.plate_id - 1);
    plate->gcode_file = gcode_result.filename;
    plate->is_sliced_valid = true;
    plate->config.apply(final_cfg);
    plate->toolpath_outside = gcode_result.toolpath_outside;
    plate->is_label_object_enabled = gcode_result.label_object_enabled;
    plate->limit_filament_maps = gcode_result.limit_filament_maps;
    plate->layer_filaments = gcode_result.layer_filaments;
    plate->printer_model_id = req.printer_model_id;
    if (const auto* nd = final_cfg.opt<Slic3r::ConfigOptionFloats>("nozzle_diameter")) {
        std::string joined;
        for (size_t i = 0; i < nd->values.size(); ++i) {
            if (i) joined += ' ';
            char buf[16];
            std::snprintf(buf, sizeof(buf), "%g", nd->values[i]);
            joined += buf;
        }
        plate->nozzle_diameters = joined;
    }

    {
        const auto& ps = print.print_statistics();
        if (ps.total_weight != 0.0) {
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%.2f", ps.total_weight);
            plate->gcode_weight = buf;
        }
        const size_t normal_idx =
            static_cast<size_t>(Slic3r::PrintEstimatedStatistics::ETimeMode::Normal);
        const float normal_time =
            gcode_result.print_statistics.modes[normal_idx].time;
        plate->gcode_prediction = std::to_string(static_cast<int>(normal_time));
        if (gcode_result.initial_layer_time > 0.0f) {
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%f", gcode_result.initial_layer_time);
            plate->first_layer_time = buf;
        }
        plate->is_support_used = print.is_support_used();

        for (size_t obj_id = 0; obj_id < model.objects.size(); ++obj_id) {
            const auto* obj = model.objects[obj_id];
            if (!obj) continue;
            for (size_t inst_id = 0; inst_id < obj->instances.size(); ++inst_id) {
                plate->objects_and_instances.emplace_back(
                    static_cast<int>(obj_id), static_cast<int>(inst_id));
            }
        }

        plate->parse_filament_info(&gcode_result);

        // parse_filament_info only sets id/used_m/used_g (bbs_3mf.cpp:593).
        // Fill type / color / filament_id from the per-slot Preset and the
        // 3MF's project_settings.config.
        const auto* threemf_colors =
            threemf_config.option<Slic3r::ConfigOptionStrings>(
                "filament_colour", false);
        const auto* threemf_ids =
            threemf_config.option<Slic3r::ConfigOptionStrings>(
                "filament_ids", false);
        for (size_t i = 0; i < plate->slice_filaments_info.size(); ++i) {
            auto& info = plate->slice_filaments_info[i];
            if (i < bundle.filament_presets.size()) {
                const Slic3r::Preset* fp = bundle.filaments.find_preset(
                    bundle.filament_presets[i], false, true);
                if (fp != nullptr) {
                    if (const auto* t = fp->config.opt<Slic3r::ConfigOptionStrings>(
                            "filament_type"); t && !t->values.empty()) {
                        info.type = t->values.front();
                    }
                }
            }
            if (threemf_colors && i < threemf_colors->values.size()) {
                info.color = threemf_colors->values[i];
            }
            if (threemf_ids && i < threemf_ids->values.size()) {
                info.filament_id = threemf_ids->values[i];
            }
        }
    }

    // 15. Write the .3mf with embedded gcode + slice_info.
    Slic3r::StoreParams store_params;
    const std::string output_path_str = req.output_3mf;
    store_params.path = output_path_str.c_str();
    store_params.model = &model;
    store_params.config = &final_cfg;
    store_params.strategy =
        Slic3r::SaveStrategy::Zip64
        | Slic3r::SaveStrategy::WithGcode
        | Slic3r::SaveStrategy::WithSliceInfo
        | Slic3r::SaveStrategy::SkipModel;
    store_params.plate_data_list.push_back(plate);

    bool stored = false;
    try {
        stored = Slic3r::store_bbs_3mf(store_params);
    } catch (const std::exception& e) {
        Slic3r::release_PlateData_list(store_params.plate_data_list);
        std::error_code ec;
        std::filesystem::remove(temp_gcode_path, ec);
        return fail("store_3mf_failed",
                    std::string("store_bbs_3mf: ") + e.what(), response);
    }

    Slic3r::release_PlateData_list(store_params.plate_data_list);
    std::error_code ec;
    std::filesystem::remove(temp_gcode_path, ec);

    if (!stored) {
        return fail("store_3mf_returned_false",
                    "store_bbs_3mf returned false", response);
    }

    emit_progress("done", 100);

    // 16. Populate the success response from print + GCodeProcessorResult.
    const auto& stats = print.print_statistics();
    const size_t normal_idx =
        static_cast<size_t>(Slic3r::PrintEstimatedStatistics::ETimeMode::Normal);
    response.status = "ok";
    response.estimate.weight_g = stats.total_weight;
    response.estimate.time_seconds =
        gcode_result.print_statistics.modes[normal_idx].time;
    response.estimate.prepare_seconds =
        gcode_result.print_statistics.modes[normal_idx].prepare_time;
    if (stats.total_used_filament > 0) {
        const double model_filament_mm =
            stats.total_used_filament - stats.total_wipe_tower_filament;
        const double weight_per_mm =
            stats.total_weight / stats.total_used_filament;
        response.estimate.model_weight_g = model_filament_mm * weight_per_mm;
        response.estimate.filament_used_m.push_back(stats.total_used_filament / 1000.0);
        response.estimate.model_filament_used_m.push_back(model_filament_mm / 1000.0);
    } else {
        response.estimate.model_weight_g = 0.0;
        response.estimate.filament_used_m.push_back(0.0);
        response.estimate.model_filament_used_m.push_back(0.0);
    }

    write_slice_response_to_stdout(response);
    return 0;
}

}  // namespace orca_headless
