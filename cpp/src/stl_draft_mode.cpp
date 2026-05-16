#include "stl_draft_mode.h"

#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Model.hpp"
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

void ground_all(Slic3r::Model& model) {
    for (auto* obj : model.objects) {
        if (!obj) continue;
        obj->ensure_on_bed(/*allow_negative_z=*/false);
    }
}

void apply_gui_import_name_fallbacks(Slic3r::Model& model) {
    for (auto* obj : model.objects) {
        if (!obj || !obj->name.empty()) continue;
        obj->name = std::filesystem::path(obj->input_file).filename().string();
    }
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
    scene["source_filename"] =
        std::filesystem::path(req.input_stl).filename().string();

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

    Slic3r::DynamicPrintConfig cfg = minimal_bed_config();
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
    apply_gui_import_name_fallbacks(model);

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

}  // namespace

int run_stl_draft_mode(const StlDraftRequest& req) {
    StlDraftResponse response;
    if (req.operation == "import") {
        return run_import(req, response);
    }
    if (req.operation == "layout" || req.operation == "export_3mf") {
        return fail("unsupported_operation",
                    req.operation + " is unavailable in this build",
                    response);
    }
    return fail("invalid_request",
                "operation must be import, layout, or export_3mf",
                response);
}

}  // namespace orca_headless
