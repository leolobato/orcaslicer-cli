#include "slice_mode.h"
#include "progress.h"

#include "libslic3r/Model.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/Print.hpp"
#include "libslic3r/PrintBase.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/Utils.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>

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
std::vector<std::string> apply_overrides_for_slot(
    Slic3r::DynamicPrintConfig& dst,
    const Slic3r::DynamicPrintConfig& src,
    const std::string& key_list,
    bool exclude_filament_keys) {
    std::vector<std::string> transferred;
    for (const auto& key : split_semicolons(key_list)) {
        if (key == "compatible_printers" || key == "compatible_prints") continue;
        if (exclude_filament_keys &&
            (starts_with(key, "filament_") || ends_with(key, "_filament"))) {
            continue;
        }
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

    // 3. Compose the final DynamicPrintConfig in the same order the GUI's
    //    `PresetBundle::construct_full_config` uses (PresetBundle.cpp:71-79):
    //    full defaults → machine → process → filament. The defaults pass is
    //    critical: it pre-populates every per-filament/per-extruder vector
    //    key with its default value, so that `Preset::normalize` below can
    //    `resize(n, default)` without the vector being absent.
    //
    //    For Phase 1 single-filament use the first filament cfg is applied
    //    flat; multi-filament needs the per-key vector merge from
    //    `construct_full_config` and is a follow-up task.
    Slic3r::DynamicPrintConfig final_cfg;
    final_cfg.apply(Slic3r::FullPrintConfig::defaults());
    final_cfg.apply(machine_cfg);
    final_cfg.apply(process_cfg);
    if (!filament_cfgs.empty()) {
        final_cfg.apply(filament_cfgs[0]);
    }

    // Pad per-filament vector keys to `num_filaments` (taken from
    // `filament_diameter` length when single_extruder_multi_material=1, else
    // from `nozzle_diameter`). Without this, `Print::apply` derefs vector
    // index 0 on options that the JSON didn't carry — null deref → SIGSEGV.
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
    // For Phase 1 we apply the process slot and the trailing printer
    // slot. Per-filament slots are applied in the multi-filament work
    // (which also needs the per-slot vector merge libslic3r expects).
    nlohmann::json transfer_status = nlohmann::json::object();
    transfer_status["status"] = "no_3mf_settings";
    if (const auto* fp = threemf_config.option<Slic3r::ConfigOptionStrings>(
            "different_settings_to_system", false);
        fp != nullptr && !fp->values.empty()) {
        // Process slot (index 0): filament-like keys excluded.
        auto process_keys = apply_overrides_for_slot(
            final_cfg, threemf_config, fp->values[0],
            /*exclude_filament_keys=*/true);
        // Printer slot (last): no name guard — machine is fixed by the
        // request, any declared printer key overlays straight onto the
        // resolved machine config.
        std::vector<std::string> printer_keys;
        if (fp->values.size() >= 2) {
            printer_keys = apply_overrides_for_slot(
                final_cfg, threemf_config, fp->values.back(),
                /*exclude_filament_keys=*/false);
        }
        const bool any =
            !process_keys.empty() || !printer_keys.empty();
        transfer_status["status"] = any ? "applied" : "no_customizations";
        transfer_status["process_keys"] = process_keys;
        transfer_status["printer_keys"] = printer_keys;
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
    // Single-element filament-used vector for v1; multi-filament splits this
    // per slot in a later task once we wire per-filament tracking.
    response.estimate.filament_used_m.push_back(stats.total_used_filament / 1000.0);

    // settings_transfer was populated inline during config composition;
    // leave it as-is here.

    write_slice_response_to_stdout(response);
    return 0;
}

}  // namespace orca_headless
