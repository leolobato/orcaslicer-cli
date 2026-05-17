#include "stl_draft_mode.h"
#include "profile_chain.h"

#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Geometry.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/PrintConfig.hpp"

#include <algorithm>
#include <exception>
#include <filesystem>
#include <string>
#include <vector>

namespace orca_headless {

namespace {

const std::vector<std::string> k_actions{
    "auto_orient",
    "rotate_z_90",
    "rotate_z_minus_90",
    "center",
    "arrange",
    "reset",
};

int fail(const std::string& code, const std::string& message, StlDraftResponse& r) {
    r.status = "error";
    r.error_code = code;
    r.error_message = message;
    write_stl_draft_response_to_stdout(r);
    return 1;
}

Slic3r::DynamicPrintConfig minimal_bed_config() {
    Slic3r::DynamicPrintConfig cfg;
    auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area", true);
    area->values = {
        Slic3r::Vec2d(0.0, 0.0),
        Slic3r::Vec2d(180.0, 0.0),
        Slic3r::Vec2d(180.0, 180.0),
        Slic3r::Vec2d(0.0, 180.0),
    };
    return cfg;
}

void ensure_printable_area_fallback(Slic3r::DynamicPrintConfig& cfg) {
    const auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area");
    if (area && !area->values.empty()) return;

    Slic3r::DynamicPrintConfig fallback = minimal_bed_config();
    cfg.apply(fallback);
}

Slic3r::Vec2d bed_center(const Slic3r::DynamicPrintConfig& cfg) {
    const auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area");
    if (!area || area->values.empty()) {
        return {90.0, 90.0};
    }

    double min_x = area->values.front().x();
    double max_x = min_x;
    double min_y = area->values.front().y();
    double max_y = min_y;
    for (const auto& p : area->values) {
        min_x = std::min(min_x, p.x());
        max_x = std::max(max_x, p.x());
        min_y = std::min(min_y, p.y());
        max_y = std::max(max_y, p.y());
    }
    return {(min_x + max_x) / 2.0, (min_y + max_y) / 2.0};
}

std::string source_filename_for(const StlDraftRequest& req) {
    if (!req.source_filename.empty()) {
        return std::filesystem::path(req.source_filename).filename().string();
    }
    return std::filesystem::path(req.input_stl).filename().string();
}

void ground_all(Slic3r::Model& model) {
    for (auto* obj : model.objects) {
        if (!obj) continue;
        obj->ensure_on_bed(/*allow_negative_z=*/false);
    }
}

struct DraftModel {
    Slic3r::Model model;
    Slic3r::DynamicPrintConfig config;
};

DraftModel read_draft_3mf(const std::string& path) {
    DraftModel draft;
    Slic3r::ConfigSubstitutionContext substitutions(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    Slic3r::PlateDataPtrs plate_data;
    std::vector<Slic3r::Preset*> project_presets;

    // GUI parity: project 3MF loading asks Model::read_from_file for model,
    // config, and auxiliary data together
    // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:5825-5848), which bottoms
    // out in load_bbs_3mf for .3mf files
    // (../OrcaSlicer/src/libslic3r/Model.cpp:320-325). Keeping LoadConfig
    // here preserves the draft's stored printer bed instead of reverting to
    // the fallback bed on layout-only requests.
    draft.model = Slic3r::Model::read_from_file(
        path,
        &draft.config,
        &substitutions,
        Slic3r::LoadStrategy::LoadModel
            | Slic3r::LoadStrategy::LoadConfig
            | Slic3r::LoadStrategy::LoadAuxiliary,
        &plate_data,
        &project_presets);
    ensure_printable_area_fallback(draft.config);
    return draft;
}

void rotate_z(Slic3r::Model& model, double radians) {
    for (auto* obj : model.objects) {
        if (!obj) continue;
        for (size_t i = 0; i < obj->instances.size(); ++i) {
            auto* inst = obj->instances[i];
            if (!inst) continue;

            const Slic3r::Vec3d center_before =
                obj->instance_bounding_box(i, false).center();
            // GUI parity: the rotate gizmo ultimately copies the updated
            // rotation into ModelInstance state
            // (../OrcaSlicer/src/slic3r/GUI/Gizmos/GizmoObjectManipulation.cpp:350-389).
            inst->set_rotation(
                Slic3r::Z,
                inst->get_rotation(Slic3r::Z) + radians);
            obj->invalidate_bounding_box();

            const Slic3r::Vec3d center_after =
                obj->instance_bounding_box(i, false).center();
            inst->set_offset(inst->get_offset() + center_before - center_after);
            obj->invalidate_bounding_box();
        }
    }
    ground_all(model);
}

void reset_layout(Slic3r::Model& model,
                  const Slic3r::DynamicPrintConfig& cfg) {
    for (auto* obj : model.objects) {
        if (!obj) continue;
        for (auto* inst : obj->instances) {
            if (!inst) continue;
            // GUI parity: reset rotation zeroes ModelInstance transform
            // rotation before the GL canvas syncs it back to the model
            // (../OrcaSlicer/src/slic3r/GUI/Gizmos/GizmoObjectManipulation.cpp:554-589).
            inst->set_rotation(Slic3r::Vec3d::Zero());
            inst->set_scaling_factor(Slic3r::Vec3d::Ones());
            inst->set_mirror(Slic3r::Vec3d::Ones());
        }
        obj->invalidate_bounding_box();
    }
    model.center_instances_around_point(bed_center(cfg));
    ground_all(model);
}

void copy_file_or_throw(const std::string& src, const std::string& dst) {
    std::filesystem::copy_file(
        src,
        dst,
        std::filesystem::copy_options::overwrite_existing);
}

void apply_gui_import_name_fallbacks(Slic3r::Model& model,
                                     const StlDraftRequest& req) {
    const std::string source_filename = source_filename_for(req);
    const std::string input_filename =
        std::filesystem::path(req.input_stl).filename().string();
    for (auto* obj : model.objects) {
        if (!obj) continue;
        if (!obj->name.empty()
            && (source_filename.empty() || obj->name != input_filename)) {
            continue;
        }
        obj->name = source_filename.empty() ? input_filename : source_filename;
    }
}

bool apply_plate_type_override(const StlDraftRequest& req,
                               Slic3r::DynamicPrintConfig& cfg,
                               StlDraftResponse& response) {
    if (req.plate_type.empty()) return true;
    try {
        Slic3r::ConfigSubstitutionContext ctxt{
            Slic3r::ForwardCompatibilitySubstitutionRule::Disable};
        cfg.set_deserialize("curr_bed_type", req.plate_type, ctxt);
    } catch (const std::exception& e) {
        fail("invalid_plate_type",
             std::string("plate_type=\"") + req.plate_type + "\" is not a "
                "valid OrcaSlicer bed type for this machine: " + e.what(),
             response);
        return false;
    }
    return true;
}

bool has_profile_context(const StlDraftRequest& req) {
    return !req.machine_chain_dir.empty()
        || !req.process_chain_dir.empty()
        || !req.machine_leaf_name.empty()
        || !req.process_leaf_name.empty();
}

bool configure_import_profile_context(const StlDraftRequest& req,
                                      Slic3r::DynamicPrintConfig& cfg,
                                      StlDraftResponse& response) {
    if (!has_profile_context(req)) {
        cfg = minimal_bed_config();
        return apply_plate_type_override(req, cfg, response);
    }
    if (req.machine_chain_dir.empty()
        || req.process_chain_dir.empty()
        || req.machine_leaf_name.empty()
        || req.process_leaf_name.empty()) {
        fail("invalid_request",
             "machine/process chain dirs and leaf names are required together",
             response);
        return false;
    }

    Slic3r::PresetBundle bundle;
    try {
        load_chain_dir_into(bundle.printers, req.machine_chain_dir);
        load_chain_dir_into(bundle.prints, req.process_chain_dir);
    } catch (const std::exception& e) {
        fail("invalid_profile",
             std::string("load chain dir: ") + e.what(),
             response);
        return false;
    }

    if (!bundle.printers.select_preset_by_name(req.machine_leaf_name, true)) {
        fail("invalid_profile",
             "machine leaf '" + req.machine_leaf_name +
                "' not found in chain dir",
             response);
        return false;
    }
    if (!bundle.prints.select_preset_by_name(req.process_leaf_name, true)) {
        fail("invalid_profile",
             "process leaf '" + req.process_leaf_name +
                "' not found in chain dir",
             response);
        return false;
    }

    // GUI parity: compose the preview draft config from the same edited
    // presets full_fff_config reads (PresetBundle.cpp:3039-3047), but omit
    // filament because STL preview placement only needs process + printer
    // context. The chain files themselves are loaded through parse_subfile's
    // default-fill pattern in load_chain_dir_into.
    cfg = Slic3r::DynamicPrintConfig();
    cfg.apply(Slic3r::FullPrintConfig::defaults());
    cfg.apply(bundle.prints.get_edited_preset().config);
    cfg.apply(bundle.printers.get_edited_preset().config);

    return apply_plate_type_override(req, cfg, response);
}

bool store_draft_3mf(const std::string& path,
                     Slic3r::Model& model,
                     Slic3r::DynamicPrintConfig& cfg) {
    Slic3r::StoreParams store_params;
    store_params.path = path.c_str();
    store_params.model = &model;
    store_params.config = &cfg;
    store_params.strategy = Slic3r::SaveStrategy::Zip64;
    return Slic3r::store_bbs_3mf(store_params);
}

nlohmann::json scene_for_model(const StlDraftRequest& req,
                               const Slic3r::Model& model,
                               const Slic3r::DynamicPrintConfig& cfg) {
    nlohmann::json scene;
    scene["draft_token"] = req.draft_token;
    std::string source_filename = source_filename_for(req);
    if (source_filename.empty()) {
        for (const auto* obj : model.objects) {
            if (obj && !obj->name.empty()) {
                source_filename = obj->name;
                break;
            }
        }
    }
    scene["source_filename"] = source_filename;

    const auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area");
    nlohmann::json printable = nlohmann::json::array();
    double min_x = 0.0;
    double max_x = 180.0;
    double min_y = 0.0;
    double max_y = 180.0;
    if (area && !area->values.empty()) {
        min_x = max_x = area->values.front().x();
        min_y = max_y = area->values.front().y();
        for (const auto& p : area->values) {
            printable.push_back({p.x(), p.y()});
            min_x = std::min(min_x, p.x());
            max_x = std::max(max_x, p.x());
            min_y = std::min(min_y, p.y());
            max_y = std::max(max_y, p.y());
        }
    }

    scene["bed"] = {
        {"width", max_x - min_x},
        {"depth", max_y - min_y},
        {"printable_area", printable},
    };

    nlohmann::json objects = nlohmann::json::array();
    for (size_t obj_idx = 0; obj_idx < model.objects.size(); ++obj_idx) {
        const auto* obj = model.objects[obj_idx];
        if (!obj || obj->instances.empty()) continue;

        const auto* inst = obj->instances.front();
        const Slic3r::BoundingBoxf3 bb = obj->instance_bounding_box(0, false);
        objects.push_back({
            {"id", std::to_string(obj_idx)},
            {"name", obj->name},
            {"transform", {
                {"offset", {
                    inst->get_offset().x(),
                    inst->get_offset().y(),
                    inst->get_offset().z(),
                }},
                {"rotation", {
                    inst->get_rotation().x(),
                    inst->get_rotation().y(),
                    inst->get_rotation().z(),
                }},
                {"scale", {
                    inst->get_scaling_factor().x(),
                    inst->get_scaling_factor().y(),
                    inst->get_scaling_factor().z(),
                }},
            }},
            {"bbox", {
                {"min", {bb.min.x(), bb.min.y(), bb.min.z()}},
                {"max", {bb.max.x(), bb.max.y(), bb.max.z()}},
            }},
            {"printable",
                bb.min.x() >= min_x && bb.max.x() <= max_x
                    && bb.min.y() >= min_y && bb.max.y() <= max_y},
        });
    }

    scene["objects"] = objects;
    scene["warnings"] = nlohmann::json::array();
    scene["actions"] = k_actions;
    return scene;
}

int run_import(const StlDraftRequest& req, StlDraftResponse& response) {
    if (req.input_stl.empty() || req.output_3mf.empty()) {
        return fail("invalid_request",
                    "input_stl and output_3mf are required",
                    response);
    }

    Slic3r::DynamicPrintConfig cfg;
    if (!configure_import_profile_context(req, cfg, response)) {
        return 1;
    }

    Slic3r::ConfigSubstitutionContext substitutions(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    Slic3r::Model model;
    try {
        // GUI parity: Plater::load_files calls Model::read_from_file for STL
        // imports (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6406), and
        // Model::read_from_file dispatches .stl to load_stl then adds default
        // instances (../OrcaSlicer/src/libslic3r/Model.cpp:277-360).
        model = Slic3r::Model::read_from_file(
            req.input_stl,
            &cfg,
            &substitutions,
            Slic3r::LoadStrategy::LoadModel
                | Slic3r::LoadStrategy::AddDefaultInstances);
    } catch (const std::exception& e) {
        return fail("invalid_stl",
                    std::string("read_from_file: ") + e.what(),
                    response);
    }

    if (model.objects.empty()) {
        return fail("invalid_stl", "STL file contains no objects", response);
    }

    // GUI parity: Plater backfills empty imported object names from the
    // source filename (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6430-6433).
    apply_gui_import_name_fallbacks(model, req);

    if (req.center) {
        // GUI parity: non-project import placement uses
        // Model::center_instances_around_point with the active bed center
        // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6587-6589).
        model.center_instances_around_point(bed_center(cfg));
    }
    // GUI parity: Plater grounds imported objects with ensure_on_bed
    // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6572-6573).
    ground_all(model);

    try {
        if (!store_draft_3mf(req.output_3mf, model, cfg)) {
            return fail("export_failed",
                        "store_bbs_3mf returned false",
                        response);
        }
    } catch (const std::exception& e) {
        return fail("export_failed",
                    std::string("store_bbs_3mf: ") + e.what(),
                    response);
    }

    response.status = "ok";
    response.scene = scene_for_model(req, model, cfg);
    write_stl_draft_response_to_stdout(response);
    return 0;
}

int run_layout(const StlDraftRequest& req, StlDraftResponse& response) {
    if (req.input_3mf.empty() || req.output_3mf.empty() || req.action.empty()) {
        return fail("invalid_request",
                    "input_3mf, output_3mf, and action are required",
                    response);
    }

    DraftModel draft;
    try {
        draft = read_draft_3mf(req.input_3mf);
    } catch (const std::exception& e) {
        return fail("invalid_draft",
                    std::string("read_from_file: ") + e.what(),
                    response);
    }

    if (draft.model.objects.empty()) {
        return fail("invalid_draft", "draft 3MF contains no objects", response);
    }

    if (req.action == "center") {
        // GUI parity: centering uses Model::center_instances_around_point
        // (../OrcaSlicer/src/libslic3r/Model.cpp:699-716) followed by
        // ensure_on_bed (../OrcaSlicer/src/libslic3r/Model.cpp:1717-1735).
        draft.model.center_instances_around_point(bed_center(draft.config));
        ground_all(draft.model);
    } else if (req.action == "rotate_z_90") {
        rotate_z(draft.model, Slic3r::Geometry::deg2rad(90.0));
    } else if (req.action == "rotate_z_minus_90") {
        rotate_z(draft.model, Slic3r::Geometry::deg2rad(-90.0));
    } else if (req.action == "reset") {
        reset_layout(draft.model, draft.config);
    } else if (req.action == "auto_orient") {
        return fail("auto_orient_failed",
                    "auto_orient requires orientation support",
                    response);
    } else if (req.action == "arrange") {
        return fail("arrange_failed",
                    "arrange requires arrange support",
                    response);
    } else {
        return fail("invalid_request", "unknown action: " + req.action, response);
    }

    try {
        if (!store_draft_3mf(req.output_3mf, draft.model, draft.config)) {
            return fail("export_failed",
                        "store_bbs_3mf returned false",
                        response);
        }
    } catch (const std::exception& e) {
        return fail("export_failed",
                    std::string("store_bbs_3mf: ") + e.what(),
                    response);
    }

    response.status = "ok";
    response.scene = scene_for_model(req, draft.model, draft.config);
    write_stl_draft_response_to_stdout(response);
    return 0;
}

int run_export_3mf(const StlDraftRequest& req, StlDraftResponse& response) {
    if (req.input_3mf.empty() || req.output_3mf.empty()) {
        return fail("invalid_request",
                    "input_3mf and output_3mf are required",
                    response);
    }
    try {
        copy_file_or_throw(req.input_3mf, req.output_3mf);
    } catch (const std::exception& e) {
        return fail("export_failed",
                    std::string("copy export draft: ") + e.what(),
                    response);
    }

    response.status = "ok";
    write_stl_draft_response_to_stdout(response);
    return 0;
}

}  // namespace

int run_stl_draft_mode(const StlDraftRequest& req) {
    StlDraftResponse response;
    if (req.operation == "import") {
        return run_import(req, response);
    }
    if (req.operation == "layout") {
        return run_layout(req, response);
    }
    if (req.operation == "export_3mf") {
        return run_export_3mf(req, response);
    }
    return fail("invalid_request",
                "operation must be import, layout, or export_3mf",
                response);
}

}  // namespace orca_headless
