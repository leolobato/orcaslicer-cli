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

#include <optional>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <unordered_map>
#include <unordered_set>

#include <nlohmann/json.hpp>

namespace orca_headless {

namespace {

bool starts_with(const std::string& s, const char* prefix) {
    const size_t n = std::strlen(prefix);
    return s.size() >= n && std::memcmp(s.data(), prefix, n) == 0;
}

bool ends_with(const std::string& s, const char* suffix) {
    const size_t n = std::strlen(suffix);
    return s.size() >= n && std::memcmp(s.data() + s.size() - n, suffix, n) == 0;
}

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

// Overlay the keys declared in `different_settings_to_system[slot]` from
// the 3MF's project config onto the target config. Returns the list of
// keys actually transferred.
//
// For the process slot (index 0), filament-like keys (`filament_*` /
// `*_filament`) are filtered out — they belong to filament slots, even
// when listed under the process fingerprint. Same rule the legacy Python
// path enforced.
// Printer-slot blocklist: keys whose values describe the printer's
// per-extruder topology (vector layouts indexed by extruder count) and must
// only ever come from the printer preset itself. The 3MF's
// `different_settings_to_system[printer_slot]` may declare them when the
// authoring printer had a different topology (e.g. P-series multi-extruder
// values exported into a project later sliced for an A1 mini), and overlaying
// them onto our resolved machine config recreates the SIGSEGV class the
// `s_project_options` whitelist closed for the project-config path —
// `update_values_to_printer_extruders` dereferences these vectors out of
// bounds when the size doesn't match the active machine's extruder count.
//
// Mirrors the reasoning behind `s_project_options` above; keep both lists
// read together at the top of the file so future audits see the pair.
static const std::unordered_set<std::string> s_printer_slot_blocklist{
    "extruder_variant_list",
    "printer_extruder_variant",
    "printer_extruder_id",
    "extruder_type",
    "nozzle_volume_type",
    "filament_extruder_variant",
    "filament_self_index",
    "extruder_ams_count",
};

std::vector<std::string> apply_overrides_for_slot(
    Slic3r::DynamicPrintConfig& dst,
    const Slic3r::DynamicPrintConfig& src,
    const std::string& key_list,
    bool exclude_filament_keys,
    const std::unordered_set<std::string>& excluded_keys) {
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
        if (dst_opt == nullptr) continue;  // unknown to libslic3r
        dst_opt->set(src_opt);
        transferred.push_back(key);
    }
    return transferred;
}

// Load a single preset JSON file (machine / process / filament) into a
// DynamicPrintConfig. The OrcaSlicer profiles are flat JSON objects whose
// keys map 1:1 to libslic3r config option names. We use load_from_json
// with `load_inherits=false` because callers (the Python service)
// pre-resolve the inheritance chain before passing files in.
Slic3r::DynamicPrintConfig load_preset_json(const std::string& path) {
    Slic3r::DynamicPrintConfig cfg;
    Slic3r::ConfigSubstitutionContext ctx(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    // libslic3r returns extra key/value pairs (e.g. "name", "from", "type"
    // metadata that aren't config options) via key_values, plus any error
    // text via reason. We don't propagate either for Phase 1; failures
    // surface as exit-non-zero from the int return and are caught by the
    // caller's try/catch.
    std::map<std::string, std::string> key_values;
    std::string reason;
    cfg.load_from_json(path, ctx, /*load_inherits_in_config=*/false,
                       key_values, reason);
    return cfg;
}

// Resize the project's flush-volume vectors to match the target machine's
// extruder count and the active filament count. Mirrors the GUI's
// `PresetBundle::update_multi_material_preferences`
// (vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:4316-4355) — the GUI
// runs this whenever the active printer or filament list changes, so by
// the time the slicer runs the matrix dimensions match. We have no
// PresetBundle, so we run it explicitly here on the filtered
// `project_config` before it's handed to `construct_full_config`.
//
// Without this, a 3MF authored on a 4-filament project sliced with 2
// filaments (or any cross-printer flow that changes nozzle count) keeps
// the 4×4 matrix and trips
// `vendor/OrcaSlicer/src/libslic3r/GCode.cpp:5394-5411`'s
// "Flush volumes matrix do not match to the correct size!" abort
// mid-export.
//
// Layout (per OrcaSlicer): `flush_volumes_matrix` is `num_filaments² ·
// nozzle_count` doubles laid out per-nozzle slab; `flush_volumes_vector`
// is `2 · num_filaments` doubles (purge volume in/out per filament);
// `flush_multiplier` is `nozzle_count` doubles. New cells default to
// `i == j ? 0 : flush_vec[2i] + flush_vec[2j+1]` for the per-pair
// purge, matching the GUI's preserve-and-fill at line 4350.
void resize_flush_volumes_for_topology(
    Slic3r::DynamicPrintConfig& project_config,
    size_t num_filaments, size_t nozzle_count) {
    if (num_filaments == 0 || nozzle_count == 0) return;

    auto* matrix_opt = project_config.option<Slic3r::ConfigOptionFloats>(
        "flush_volumes_matrix");
    auto* mult_opt = project_config.option<Slic3r::ConfigOptionFloats>(
        "flush_multiplier");
    auto* vec_opt = project_config.option<Slic3r::ConfigOptionFloats>(
        "flush_volumes_vector");

    size_t old_nozzle_count = (mult_opt != nullptr) ? mult_opt->values.size() : 0;
    size_t old_matrix_total = (matrix_opt != nullptr) ? matrix_opt->values.size() : 0;
    size_t old_num_filaments = 0;
    if (old_nozzle_count > 0 && old_matrix_total > 0) {
        old_num_filaments = static_cast<size_t>(
            std::sqrt(static_cast<double>(old_matrix_total) /
                      static_cast<double>(old_nozzle_count)) + 1e-6);
    }

    if (mult_opt != nullptr && old_nozzle_count != nozzle_count) {
        mult_opt->values.resize(nozzle_count, 1.0);
    }

    const size_t target_matrix_per_slab = num_filaments * num_filaments;
    if (matrix_opt == nullptr ||
        old_matrix_total == target_matrix_per_slab * nozzle_count) {
        return;
    }

    // Pad/truncate flush_volumes_vector to 2 entries per filament before
    // synthesising new matrix cells from it (matches GUI lines 4327-4335).
    if (vec_opt != nullptr) {
        auto& vec = vec_opt->values;
        while (vec.size() < 2 * num_filaments) {
            vec.push_back(vec.size() > 1 ? vec[0] : 140.0);
            vec.push_back(vec.size() > 1 ? vec[1] : 140.0);
        }
        while (vec.size() > 2 * num_filaments) {
            vec.pop_back();
            vec.pop_back();
        }
    }

    const std::vector<double>& old_matrix = matrix_opt->values;
    const std::vector<double>* fill_vec =
        (vec_opt != nullptr) ? &vec_opt->values : nullptr;

    std::vector<double> new_matrix(target_matrix_per_slab * nozzle_count, 0.0);
    for (size_t i = 0; i < num_filaments; ++i) {
        for (size_t j = 0; j < num_filaments; ++j) {
            for (size_t nozzle_id = 0; nozzle_id < nozzle_count; ++nozzle_id) {
                const size_t dst_idx =
                    i * num_filaments + j + target_matrix_per_slab * nozzle_id;
                if (i < old_num_filaments && j < old_num_filaments &&
                    nozzle_id < old_nozzle_count) {
                    const size_t old_per_slab =
                        old_num_filaments * old_num_filaments;
                    const size_t src_idx =
                        i * old_num_filaments + j + old_per_slab * nozzle_id;
                    if (src_idx < old_matrix.size()) {
                        new_matrix[dst_idx] = old_matrix[src_idx];
                        continue;
                    }
                }
                if (i == j) {
                    new_matrix[dst_idx] = 0.0;
                } else if (fill_vec != nullptr &&
                           2 * i < fill_vec->size() &&
                           2 * j + 1 < fill_vec->size()) {
                    new_matrix[dst_idx] =
                        (*fill_vec)[2 * i] + (*fill_vec)[2 * j + 1];
                } else {
                    // Fallback if vec_opt was missing entirely.
                    new_matrix[dst_idx] = 280.0;
                }
            }
        }
    }
    matrix_opt->values = std::move(new_matrix);
}

// Build a lookup from project-local filament preset names → the base
// system preset they inherit from. OrcaSlicer marks a per-slot override
// of a system filament with an arbitrary user-typed suffixed name (e.g.
// `Bambu PLA Basic @BBL A1M(my-project.3mf)` — the suffix can be
// anything, no structural pattern), and embeds the project-local
// preset's actual definition in `Metadata/filament_settings_*.config`
// inside the .3mf. The embedded preset's `inherits` field is the
// reliable signal for "this is a variant of base preset X".
//
// `Model::read_from_file` with `LoadConfig` populates `project_presets`
// from those embedded configs (see
// `vendor/OrcaSlicer/src/libslic3r/Format/bbs_3mf.cpp:1862-1874`'s
// `_extract_project_embedded_presets_from_archive` calls). We use that
// to resolve project-local names to their base before the per-slot
// name guard fires — without this, our exact-string match treats the
// suffixed name as a different filament and discards a perfectly valid
// override (the GUI itself has no name guard at all and applies these
// unconditionally; see PresetBundle.cpp:3641-3712).
std::unordered_map<std::string, std::string>
build_project_filament_inherits_map(
    const std::vector<Slic3r::Preset*>& project_presets) {
    std::unordered_map<std::string, std::string> out;
    for (const auto* preset : project_presets) {
        if (preset == nullptr) continue;
        if (preset->type != Slic3r::Preset::TYPE_FILAMENT) continue;
        const std::string& name = preset->name;
        const std::string& inherits = preset->inherits();
        if (!name.empty() && !inherits.empty()) {
            out[name] = inherits;
        }
    }
    return out;
}

// Center the combined instance bounding box on the build plate. Mirrors
// `Model::center_instances_around_point`, which is how the GUI's "fit to
// plate" path reseats objects (it shifts each instance's offset, NOT the
// object's intrinsic mesh — the latter is what the previous implementation
// did, and it produced positions hundreds of mm off-center because the
// 3MF's stored instance offsets stayed in place on top of our translate).
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

// Helper: emit error + return 1 with a populated SliceResponse.
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

    // Match the GUI's cleanup at PresetBundle.cpp:3528-3529: drop
    // `extruder_ams_count` from the parsed 3MF config so no later code
    // path can read stale data. The `s_project_options` whitelist already
    // keeps it out of `project_config`, but `threemf_config` is still
    // passed to `apply_overrides_for_slot` for the per-slot fingerprints,
    // and the `s_printer_slot_blocklist` only blocks declared keys —
    // erasing here keeps the data structure honest if the fingerprint
    // ever expands.
    threemf_config.erase("extruder_ams_count");

    // Map from project-local filament preset names to their inherited
    // base preset. Populated from the embedded `Metadata/filament_settings_*.config`
    // files inside the .3mf (loaded into `project_presets` by
    // `Model::read_from_file`). Used by the per-slot name guard below
    // to recognise that `Foo @A1M(arbitrary user text)` is a variant of
    // base preset `Foo @A1M` and the per-slot override should still apply.
    const auto project_filament_inherits =
        build_project_filament_inherits_map(project_presets);

    emit_progress("loading_profiles", 10);

    // 2. Load the three profile JSONs (resolved upstream by Python).
    Slic3r::DynamicPrintConfig machine_cfg, process_cfg;
    std::vector<Slic3r::DynamicPrintConfig> filament_cfgs;

    try {
        machine_cfg = load_preset_json(req.machine_profile);
        process_cfg = load_preset_json(req.process_profile);
        for (const auto& fp : req.filament_profiles) {
            filament_cfgs.push_back(load_preset_json(fp));
        }
    } catch (const std::exception& e) {
        return fail("invalid_profile",
                    std::string("load preset JSON: ") + e.what(), response);
    }

    emit_progress("composing_config", 20);

    // 3a. Filter the 3MF's project_settings.config to the project-only keys
    //     the GUI uses. The 3MF on disk carries ~500 keys including
    //     printer-extruder topology (`extruder_variant_list`,
    //     `printer_extruder_variant`, `printer_extruder_id`, `extruder_type`,
    //     `filament_extruder_variant`, `filament_self_index`, …) that
    //     describe the printer it was authored for, NOT the printer we slice
    //     onto. The GUI's `PresetBundle::load_config_file_config` keeps only
    //     the 13 keys in `s_project_options`
    //     (`vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:37-52`) via
    //     `this->project_config.apply_only(config, s_project_options)` at
    //     line 3717, so when `full_fff_config` later does
    //     `out.apply(this->project_config)` only those 13 keys overlay.
    //
    //     We load the 3MF directly via `Model::read_from_file(LoadConfig)`
    //     and get the raw project_settings.config without that filtering.
    //     Blanket-applying it inside `construct_full_config` (line 76)
    //     overwrites the printer's correctly-sized per-extruder vectors
    //     with longer ones from the 3MF. `support_different_extruders`
    //     then trips on the comma-separated `extruder_variant_list` entry
    //     and routes through the multi-extruder branch of
    //     `update_values_to_printer_extruders`, which dereferences out of
    //     bounds and SIGSEGVs. Mirroring the GUI's whitelist removes the
    //     corruption at the source. Keep in sync if the vendor adds keys.
    static const std::vector<std::string> s_project_options{
        "flush_volumes_vector", "flush_volumes_matrix",
        "filament_colour", "filament_colour_type", "filament_multi_colour",
        "wipe_tower_x", "wipe_tower_y", "wipe_tower_rotation_angle",
        "curr_bed_type", "flush_multiplier",
        "nozzle_volume_type", "filament_map_mode", "filament_map",
    };
    Slic3r::DynamicPrintConfig project_config;
    project_config.apply_only(threemf_config, s_project_options);

    // 3a.1. Resize flush-volumes vectors so they match the active filament
    //       count and the target machine's nozzle count. The 3MF's matrix
    //       was sized for whatever the file was authored with; if we slice
    //       it on a printer with a different nozzle count or a request
    //       with a different filament count, GCode export aborts with
    //       "Flush volumes matrix do not match to the correct size!".
    //       The GUI runs this on every printer/filament-list change via
    //       `update_multi_material_preferences`; we run it once here.
    {
        size_t nozzle_count = 1;
        if (const auto* nd = machine_cfg.option<Slic3r::ConfigOptionFloats>(
                "nozzle_diameter"); nd != nullptr && !nd->values.empty()) {
            nozzle_count = nd->values.size();
        }
        resize_flush_volumes_for_topology(
            project_config, filament_cfgs.size(), nozzle_count);
    }

    // 3b. Pre-populate each filament config with libslic3r's filament-key
    //     defaults before wrapping it in a Preset. The GUI's filament
    //     Presets inherit from `filaments.default_preset()` which has every
    //     filament option populated; our `load_preset_json` only loads what
    //     the JSON literally contains, so without this backstop
    //     `construct_full_config`'s per-key merge (PresetBundle.cpp:147-177)
    //     dereferences nullptr when filament[0] declares a key that another
    //     slot's sparser JSON omits. We filter to filament_options() only
    //     because applying ALL FullPrintConfig defaults would inject
    //     printer/process keys into the filament Preset and corrupt the
    //     final merge.
    const auto& full_defaults = Slic3r::FullPrintConfig::defaults();
    Slic3r::DynamicPrintConfig filament_defaults;
    for (const std::string& key : Slic3r::Preset::filament_options()) {
        const Slic3r::ConfigOption* opt = full_defaults.option(key);
        if (opt != nullptr) filament_defaults.set_key_value(key, opt->clone());
    }
    auto fill_filament_defaults =
        [&filament_defaults](Slic3r::DynamicPrintConfig& cfg) {
            Slic3r::DynamicPrintConfig out;
            out.apply(filament_defaults);
            out.apply(cfg);
            cfg = std::move(out);
        };
    for (auto& fc : filament_cfgs) {
        fill_filament_defaults(fc);
        // Mirror what `PresetBundle::load_*` runs on every filament Preset
        // it materialises (vendor/OrcaSlicer/src/libslic3r/Preset.cpp:370):
        // walk filament_diameter's length and resize every per-filament
        // vector key to match, padding from FullPrintConfig defaults. The
        // explicit fill_filament_defaults above already covers the
        // canonical key set, but `Preset::normalize` also picks up any
        // upstream-Orca additions to `Preset::filament_options()` that
        // a user-imported filament JSON happens to carry — without this
        // call, a sparser slot's missing key in `construct_full_config`'s
        // per-key merge nullptr-derefs.
        Slic3r::Preset::normalize(fc);
    }

    Slic3r::Preset printer_preset(Slic3r::Preset::TYPE_PRINTER, "wrapper-printer");
    printer_preset.config = std::move(machine_cfg);
    Slic3r::Preset print_preset(Slic3r::Preset::TYPE_PRINT, "wrapper-process");
    print_preset.config = std::move(process_cfg);
    std::vector<Slic3r::Preset> filament_presets;
    filament_presets.reserve(filament_cfgs.size());
    for (size_t i = 0; i < filament_cfgs.size(); ++i) {
        Slic3r::Preset fp(Slic3r::Preset::TYPE_FILAMENT,
                          "wrapper-filament-" + std::to_string(i));
        fp.config = std::move(filament_cfgs[i]);
        filament_presets.push_back(std::move(fp));
    }

    // 3c. Compose the final DynamicPrintConfig via the GUI's authoritative
    //     `PresetBundle::construct_full_config` (PresetBundle.cpp:61), passing
    //     the FILTERED `project_config` (not the raw `threemf_config`) so
    //     printer-extruder fields stay sourced from the printer preset.
    Slic3r::DynamicPrintConfig final_cfg;
    try {
        final_cfg = Slic3r::PresetBundle::construct_full_config(
            printer_preset, print_preset, project_config,
            filament_presets,
            /*apply_extruder=*/true,
            /*filament_maps_new=*/std::nullopt);
    } catch (const std::exception& e) {
        return fail("compose_failed",
                    std::string("construct_full_config: ") + e.what(),
                    response);
    }

    // construct_full_config doesn't call Preset::normalize itself, so still
    // run it to pad any missing per-filament vectors (covers user-imported
    // filament JSONs that omit keys the leaf system filament would have
    // had via inheritance).
    Slic3r::Preset::normalize(final_cfg);

    // Honor the 3MF's `different_settings_to_system` fingerprint:
    //   [process, filament_0, …, filament_{N-1}, printer]
    // (See PresetBundle::load_3mf_*; the printer slot lives at
    // num_filaments+1.) This is how the GUI carries user customizations
    // — e.g. layer_height, sparse_infill_density — that should override
    // the resolved system process. Without this overlay we slice with
    // bare system defaults and the output diverges meaningfully from the
    // GUI even on simple projects.
    //
    // Per-filament slots (indexes 1..N) are applied with a name guard:
    // only when the request's `filament_settings_id[i]` (a display name)
    // matches the 3MF's `filament_settings_id[i]` — modulo project-local
    // preset variants, which carry an arbitrary user-typed suffix on
    // their name (e.g. `Foo @A1M(my notes)`) but inherit from a system
    // base preset. We resolve those through `project_filament_inherits`
    // before the comparison so the override doesn't get silently dropped.
    // The GUI itself has no name guard at all and applies these
    // unconditionally (see `PresetBundle::load_3mf_*` at
    // vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:3641-3712); ours
    // is a more conservative report-on-divergence behaviour for genuine
    // filament swaps.
    //
    // When the user genuinely swapped filaments (different base preset),
    // the customizations referenced the OLD filament's defaults and
    // become meaningless on the new one — discard and report back so
    // the client can surface what was dropped.
    std::vector<std::string> threemf_filament_names;
    if (const auto* opt = threemf_config.option<Slic3r::ConfigOptionStrings>(
            "filament_settings_id", false);
        opt != nullptr) {
        threemf_filament_names = opt->values;
    }

    nlohmann::json transfer_status = nlohmann::json::object();
    transfer_status["status"] = "no_3mf_settings";
    if (const auto* fp = threemf_config.option<Slic3r::ConfigOptionStrings>(
            "different_settings_to_system", false);
        fp != nullptr && !fp->values.empty()) {
        // Process slot (index 0): filament-like keys excluded.
        auto process_keys = apply_overrides_for_slot(
            final_cfg, threemf_config, fp->values[0],
            /*exclude_filament_keys=*/true,
            /*excluded_keys=*/{});

        // Per-filament slots: indexes 1..N. Layout is
        // [process, filament_0, …, filament_{N-1}, printer].
        nlohmann::json filament_slot_status = nlohmann::json::array();
        const size_t num_filament_slots =
            fp->values.size() >= 2 ? fp->values.size() - 2 : 0;
        for (size_t i = 0; i < num_filament_slots; ++i) {
            const std::string& key_list = fp->values[i + 1];
            const std::string original =
                i < threemf_filament_names.size()
                    ? threemf_filament_names[i] : "";
            const std::string selected =
                i < req.filament_settings_id.size()
                    ? req.filament_settings_id[i] : "";
            nlohmann::json entry;
            entry["slot"] = i;
            entry["original_filament"] = original;
            entry["selected_filament"] = selected;
            // Resolve project-local preset names (e.g. `Foo @A1M(my notes)`)
            // to their base via the embedded preset's `inherits` field
            // before the name guard fires. Falls through to the original
            // name when the 3MF didn't embed a project-local preset for
            // this slot (i.e. it just references a system preset directly).
            std::string original_resolved = original;
            if (auto it = project_filament_inherits.find(original);
                it != project_filament_inherits.end()) {
                original_resolved = it->second;
            }
            entry["original_filament_resolved"] = original_resolved;
            if (key_list.empty()) {
                entry["status"] = "no_customizations";
                entry["transferred"] = nlohmann::json::array();
                entry["discarded"] = nlohmann::json::array();
            } else if (!original.empty() && original_resolved == selected) {
                // The per-slot override applies as a full-vector copy of
                // the listed keys from threemf_config onto final_cfg —
                // each per-filament key is stored as a parallel vector
                // indexed by slot, so `dst_opt->set(src_opt)` lands the
                // values at their correct indices automatically.
                const auto transferred = apply_overrides_for_slot(
                    final_cfg, threemf_config, key_list,
                    /*exclude_filament_keys=*/false,
                    /*excluded_keys=*/{});
                entry["status"] = "applied";
                entry["transferred"] = transferred;
                entry["discarded"] = nlohmann::json::array();
            } else {
                entry["status"] = "filament_changed";
                entry["transferred"] = nlohmann::json::array();
                entry["discarded"] = split_semicolons(key_list);
            }
            filament_slot_status.push_back(entry);
        }

        // Printer slot (last): no name guard — machine is fixed by the
        // request, any declared printer key overlays straight onto the
        // resolved machine config. Per-extruder topology keys are blocked
        // (see s_printer_slot_blocklist) because their vector layouts
        // belong to the authoring printer's nozzle count, not ours.
        std::vector<std::string> printer_keys;
        if (fp->values.size() >= 2) {
            printer_keys = apply_overrides_for_slot(
                final_cfg, threemf_config, fp->values.back(),
                /*exclude_filament_keys=*/false,
                /*excluded_keys=*/s_printer_slot_blocklist);
        }
        const bool any_filament_applied = std::any_of(
            filament_slot_status.begin(), filament_slot_status.end(),
            [](const nlohmann::json& e) { return e["status"] == "applied"; });
        const bool any =
            !process_keys.empty() || !printer_keys.empty() || any_filament_applied;
        transfer_status["status"] = any ? "applied" : "no_customizations";
        transfer_status["process_keys"] = process_keys;
        transfer_status["printer_keys"] = printer_keys;
        transfer_status["filament_slots"] = filament_slot_status;
    }

    // curr_bed_type is a project-level field stored in the 3MF's
    // project_settings.config but NOT listed in different_settings_to_system.
    // libslic3r reads it to pick which <plate>_temp keys drive bed
    // temperature gcode (GCode.cpp:2116/2580/2937). Carry it over so our
    // output uses the same bed type the user authored.
    if (const auto* opt = threemf_config.option("curr_bed_type"); opt != nullptr) {
        if (auto* dst = final_cfg.option("curr_bed_type", /*create=*/false);
            dst != nullptr) {
            dst->set(opt);
            transfer_status["curr_bed_type"] = opt->serialize();
        }
    }

    // Caller-supplied override (e.g. user re-picked the plate in the GUI):
    // takes precedence over whatever the input 3MF authored. Mirrors what
    // ``Plater::on_change_bed_type`` does in the GUI when the dropdown changes.
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
    }

    response.settings_transfer = transfer_status;

    // 4. Wire AMS / filament selection metadata onto the final config so
    //    libslic3r threads it into slice_info.config + gcode metadata.
    if (!req.filament_map.empty()) {
        auto* opt = final_cfg.opt<Slic3r::ConfigOptionInts>("filament_map", true);
        opt->values = req.filament_map;
    }
    if (!req.filament_settings_id.empty()) {
        auto* opt = final_cfg.opt<Slic3r::ConfigOptionStrings>(
            "filament_settings_id", true);
        opt->values = req.filament_settings_id;
    }

    // 5. Recenter the model on the plate (GUI does this on import).
    if (req.recenter) {
        emit_progress("recentering", 25);
        try {
            recenter_on_plate(model, final_cfg);
        } catch (const std::exception& e) {
            return fail("recenter_failed", std::string("recenter: ") + e.what(),
                        response);
        }
    } else {
        // Even when not recentering, drop any model that the 3MF saved
        // hovering above (or buried below) z=0 onto the bed. The GUI
        // implicitly does this on every load — without it, libslic3r's
        // skirt/brim generator throws "Coordinate outside allowed range"
        // when the printable-area polygon is intersected against a model
        // whose instance offset puts it outside the bed in Z.
        for (auto* obj : model.objects) {
            if (!obj) continue;
            obj->ensure_on_bed(/*allow_negative_z=*/false);
        }
    }

    emit_progress("slicing_construct_print", 28);

    // 6. Configure the Print and run process(). BBL-printer flag controls
    //    output formatting (CONFIG_BLOCK markers, label_object tagging).
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

    emit_progress("slicing_callback", 32);
    print.set_status_callback(
        [](const Slic3r::PrintBase::SlicingStatus& status) {
            // Map libslic3r's 0..100 percent into our 30..90 band so the
            // bookend phases (load, export) keep their share of progress.
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

    // 7. Export gcode to a temp file. store_bbs_3mf reads the gcode bytes
    //    from PlateData.gcode_file when SaveStrategy::WithGcode is set.
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

    // 8. Build single-plate PlateData. Mirrors the layout
    //    PartPlateList::store_to_3mf_structure produces for a 1-plate print.
    auto* plate = new Slic3r::PlateData();
    plate->plate_index = std::max(0, req.plate_id - 1);
    plate->gcode_file = gcode_result.filename;
    plate->is_sliced_valid = true;
    plate->config.apply(final_cfg);
    plate->toolpath_outside = gcode_result.toolpath_outside;
    plate->is_label_object_enabled = gcode_result.label_object_enabled;
    plate->limit_filament_maps = gcode_result.limit_filament_maps;
    plate->layer_filaments = gcode_result.layer_filaments;
    // Identifies the target physical printer in slice_info.config — e.g.
    // "N1" for an A1 mini. Resolved by the Python service from the parent
    // BBL machine profile; empty for vendors that don't declare model_id.
    plate->printer_model_id = req.printer_model_id;
    // Stamp nozzle_diameters as a space-delimited string mirroring the GUI
    // (PartPlate.cpp:7240). Without this, slice_info.config carries an
    // empty string even though the value sits in final_cfg.
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
        // first_layer_time lives at the top of GCodeProcessorResult (see
        // GCodeProcessor.hpp:155; GCodeProcessor.cpp:2614 populates it from
        // get_first_layer_time(Normal)). The GUI reads the same field at
        // Plater.cpp:10308.
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
        // Fill the remaining fields the GUI emits:
        //   - type comes from the resolved system filament profile
        //   - color and filament_id (the BBL tray catalog ID, e.g. "GFA00")
        //     are user-authored per-slot picks that live in the input 3MF's
        //     project_settings.config — read them from threemf_config
        //
        // tray_info_idx stays empty until Phase 3 plumbs AMS slot info
        // from the gateway.
        const auto* threemf_colors =
            threemf_config.option<Slic3r::ConfigOptionStrings>(
                "filament_colour", false);
        const auto* threemf_ids =
            threemf_config.option<Slic3r::ConfigOptionStrings>(
                "filament_ids", false);
        for (size_t i = 0; i < plate->slice_filaments_info.size(); ++i) {
            auto& info = plate->slice_filaments_info[i];
            if (i < filament_cfgs.size()) {
                if (const auto* t = filament_cfgs[i]
                        .opt<Slic3r::ConfigOptionStrings>("filament_type");
                    t && !t->values.empty()) {
                    info.type = t->values.front();
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

    // 9. Write the .3mf with embedded gcode + slice_info.
    Slic3r::StoreParams store_params;
    const std::string output_path_str = req.output_3mf;
    store_params.path = output_path_str.c_str();
    store_params.model = &model;
    store_params.config = &final_cfg;
    // SkipModel mirrors the GUI's "min-save" mode (Plater.cpp:14624 etc.)
    // and the legacy CLI's `--min-save 1` flag (commit 317b3d0): omit the
    // input geometry from the output 3MF since it's not needed downstream
    // — gcode + settings + thumbnails carry everything consumers use.
    // Saves ~3MB on a typical benchy-sized project.
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

    // 10. Populate the success response from print + GCodeProcessorResult.
    const auto& stats = print.print_statistics();
    const size_t normal_idx =
        static_cast<size_t>(Slic3r::PrintEstimatedStatistics::ETimeMode::Normal);
    response.status = "ok";
    response.estimate.weight_g = stats.total_weight;
    response.estimate.time_seconds =
        gcode_result.print_statistics.modes[normal_idx].time;
    response.estimate.prepare_seconds =
        gcode_result.print_statistics.modes[normal_idx].prepare_time;
    // Model-only weight: total minus the wipe-tower contribution. libslic3r
    // tracks `total_wipe_tower_filament` in mm; convert to grams using the
    // overall (mm → g) ratio derived from the totals. This is an approximation
    // — multi-filament prints with different densities per slot will be
    // slightly off — but matches what the GUI's slice_info "Model Filament
    // Weight" field reports for single-density jobs.
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
    // Single-element vectors for v1; multi-filament splits these per slot in
    // a later task once we wire `print.print_statistics().filament_stats`
    // (per-filament mm) into the response.

    // settings_transfer was populated inline during config composition;
    // leave it as-is here.

    write_slice_response_to_stdout(response);
    return 0;
}

}  // namespace orca_headless
