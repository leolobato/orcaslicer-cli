#include "stl_draft_mode.h"
#include "profile_chain.h"

#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Arrange.hpp"
#include "libslic3r/Geometry.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/ModelArrange.hpp"
#include "libslic3r/Orient.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/PrintConfig.hpp"

#include <algorithm>
#include <exception>
#include <filesystem>
#include <string>
#include <utility>
#include <vector>

namespace orca_headless {

namespace {

const std::vector<std::string> k_actions{
    "auto_orient",
    "rotate_x_90",
    "rotate_x_minus_90",
    "rotate_y_90",
    "rotate_y_minus_90",
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
    cfg.apply(Slic3r::FullPrintConfig::defaults());
    auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area", true);
    area->values = {
        Slic3r::Vec2d(0.0, 0.0),
        Slic3r::Vec2d(180.0, 0.0),
        Slic3r::Vec2d(180.0, 180.0),
        Slic3r::Vec2d(0.0, 180.0),
    };
    return cfg;
}

void ensure_defaulted_config(Slic3r::DynamicPrintConfig& cfg) {
    Slic3r::DynamicPrintConfig defaults;
    defaults.apply(Slic3r::FullPrintConfig::defaults());
    defaults.apply(cfg);
    cfg = std::move(defaults);
}

void ensure_printable_area_fallback(Slic3r::DynamicPrintConfig& cfg) {
    const auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area");
    const bool has_printable_area = area && !area->values.empty();

    ensure_defaulted_config(cfg);
    if (has_printable_area) return;

    auto* fallback_area = cfg.opt<Slic3r::ConfigOptionPoints>(
        "printable_area", true);
    fallback_area->values = {
        Slic3r::Vec2d(0.0, 0.0),
        Slic3r::Vec2d(180.0, 0.0),
        Slic3r::Vec2d(180.0, 180.0),
        Slic3r::Vec2d(0.0, 180.0),
    };
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

void auto_orient_all(Slic3r::Model& model,
                     const Slic3r::DynamicPrintConfig& cfg) {
    Slic3r::orientation::OrientMeshs selected;
    Slic3r::orientation::OrientMeshs unselected;

    for (auto* obj : model.objects) {
        if (!obj) continue;
        for (auto* inst : obj->instances) {
            if (!inst) continue;

            Slic3r::orientation::OrientMesh om;
            om.name = obj->name;
            om.mesh = obj->mesh();
            // GUI parity: OrientJob::get_orient_mesh reads the object-local
            // support threshold first, then falls back to full_config
            // (../OrcaSlicer/src/slic3r/GUI/Jobs/OrientJob.cpp:225-242).
            if (obj->config.has("support_threshold_angle")) {
                om.overhang_angle = obj->config.opt_int("support_threshold_angle");
            } else if (cfg.has("support_threshold_angle")) {
                om.overhang_angle = cfg.opt_int("support_threshold_angle");
            }
            om.setter = [inst](const Slic3r::orientation::OrientMesh& p) {
                inst->rotate(p.rotation_matrix);
                inst->get_object()->invalidate_bounding_box();
                inst->get_object()->ensure_on_bed();
            };
            selected.emplace_back(std::move(om));
        }
    }

    if (selected.empty()) return;

    Slic3r::orientation::OrientParams params;
    // GUI parity: OrientJob uses min-volume mode unless the canvas
    // OrientSettings.min_area flag is enabled
    // (../OrcaSlicer/src/slic3r/GUI/Jobs/OrientJob.cpp:163-170).
    params.min_volume = true;
    params.progressind = [](unsigned, std::string) {};

    Slic3r::orientation::orient(selected, unselected, params);
    for (auto& mesh : selected) mesh.apply();
    ground_all(model);
}

bool arrange_draft_instances(Slic3r::Model& model,
                             const Slic3r::DynamicPrintConfig& cfg,
                             std::string& error) {
    using namespace Slic3r::arrangement;

    ArrangePolygons movable;
    std::vector<Slic3r::ModelInstance*> instances;

    for (auto* obj : model.objects) {
        if (!obj) continue;
        for (auto* inst : obj->instances) {
            if (!inst) continue;

            // GUI parity: ArrangeJob::prepare_arrange_polygon delegates to
            // get_instance_arrange_poly (../OrcaSlicer/src/slic3r/GUI/Jobs/ArrangeJob.cpp:99-108),
            // then ArrangeJob::process runs the same param update and
            // inflation pipeline before arrangement
            // (../OrcaSlicer/src/slic3r/GUI/Jobs/ArrangeJob.cpp:536-567).
            ArrangePolygon ap = Slic3r::get_instance_arrange_poly(inst, cfg);
            ap.itemid = static_cast<int>(movable.size());
            instances.push_back(inst);
            movable.emplace_back(std::move(ap));
        }
    }
    if (movable.empty()) return true;

    ArrangeParams params;
    params.allow_rotations = true;
    params.is_seq_print = false;
    params.min_obj_distance = 0;
    if (cfg.has("printable_height"))
        params.printable_height = cfg.opt_float("printable_height");
    if (cfg.has("extruder_clearance_radius"))
        params.clearance_radius = cfg.opt_float("extruder_clearance_radius");
    if (cfg.has("extruder_clearance_height_to_rod"))
        params.clearance_height_to_rod =
            cfg.opt_float("extruder_clearance_height_to_rod");
    if (cfg.has("extruder_clearance_height_to_lid"))
        params.clearance_height_to_lid =
            cfg.opt_float("extruder_clearance_height_to_lid");
    if (cfg.has("nozzle_height"))
        params.nozzle_height = cfg.opt_float("nozzle_height");
    if (const auto* bop = cfg.option<Slic3r::ConfigOptionPoint>("best_object_pos"))
        params.align_center = bop->value;
    params.progressind = [](unsigned, std::string) {};

    update_arrange_params(params, &cfg, movable);
    update_selected_items_inflation(movable, &cfg, params);
    update_selected_items_axis_align(movable, &cfg, params);

    Slic3r::Points bedpts = get_shrink_bedpts(&cfg, params);
    if (bedpts.size() < 3) {
        error = "printable area has fewer than 3 points";
        return false;
    }

    arrange(movable, /*fixed=*/{}, bedpts, params);

    for (size_t i = 0; i < movable.size(); ++i) {
        const auto& ap = movable[i];
        if (!ap.is_arranged() || ap.bed_idx != 0) {
            error = "Cannot place STL draft on bed";
            return false;
        }
        instances[i]->apply_arrange_result(ap.translation.cast<double>(),
                                           ap.rotation);
    }

    ground_all(model);
    return true;
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

void rotate_axis(Slic3r::Model& model, Slic3r::Axis axis, double radians) {
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
                axis,
                inst->get_rotation(axis) + radians);
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

void apply_gui_stl_import_normalization(
    Slic3r::Model& model,
    const Slic3r::DynamicPrintConfig& cfg) {
    double preferred_orientation = 0.0;
    if (cfg.has("preferred_orientation")) {
        preferred_orientation = cfg.opt_float("preferred_orientation");
    }

    for (auto* obj : model.objects) {
        if (!obj) continue;
        // GUI parity: STL import applies the printer/process preferred Z
        // orientation immediately after name fallback
        // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6430-6434).
        if (preferred_orientation != 0.0) {
            obj->rotate(
                Slic3r::Geometry::deg2rad(preferred_orientation),
                Slic3r::Z);
        }

        // GUI parity: non-3MF/non-AMF imports normalize object geometry
        // around the origin before placement
        // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6557-6560).
        obj->center_around_origin(false);
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
                               const Slic3r::DynamicPrintConfig& cfg,
                               const nlohmann::json& warnings) {
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

    double preferred_orientation = 0.0;
    if (cfg.has("preferred_orientation")) {
        preferred_orientation = Slic3r::Geometry::deg2rad(
            cfg.opt_float("preferred_orientation"));
    }

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
            {"mesh_transform", {
                // GUI parity: non-project STL import mutates object-local
                // geometry with ModelObject::rotate and
                // center_around_origin(false)
                // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6430-6434,
                // 6557-6560). ModelObject::origin_translation records the
                // accumulated local translation for callers that need to
                // reproduce that normalization
                // (../OrcaSlicer/src/libslic3r/Model.hpp:389-394).
                {"offset", {
                    obj->origin_translation.x(),
                    obj->origin_translation.y(),
                    obj->origin_translation.z(),
                }},
                {"rotation", {0.0, 0.0, preferred_orientation}},
                {"scale", {1.0, 1.0, 1.0}},
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
    scene["warnings"] = warnings;
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

    nlohmann::json warnings = nlohmann::json::array();
    // GUI parity: non-project imports remove zero-volume objects before
    // placement/normalization (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6479).
    const int deleted_objects = model.removed_objects_with_zero_volume();
    if (deleted_objects > 0) {
        warnings.push_back({
            {"code", "zero_volume_removed"},
            {"message", "Objects with zero volume were removed"},
            {"count", deleted_objects},
        });
    }

    if (model.objects.empty()) {
        return fail("invalid_stl",
                    "STL file contains no printable-volume objects",
                    response);
    }

    // GUI parity: Plater backfills empty imported object names from the
    // source filename (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6430-6433).
    apply_gui_import_name_fallbacks(model, req);
    apply_gui_stl_import_normalization(model, cfg);

    if (req.center) {
        // GUI parity: non-project import placement uses
        // Model::center_instances_around_point with the active bed center
        // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6587-6589).
        model.center_instances_around_point(bed_center(cfg));
    }
    // GUI parity: Plater grounds imported objects with ensure_on_bed
    // (../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6572-6573).
    ground_all(model);

    if (req.auto_orient) {
        try {
            auto_orient_all(model, cfg);
        } catch (const std::exception& e) {
            warnings.push_back({
                {"code", "auto_orient_failed"},
                {"message", e.what()},
            });
        }
    }
    if (req.arrange) {
        std::string error;
        if (!arrange_draft_instances(model, cfg, error)) {
            warnings.push_back({
                {"code", "arrange_failed"},
                {"message", error},
            });
        }
    }
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
    response.scene = scene_for_model(req, model, cfg, warnings);
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
    } else if (req.action == "rotate_x_90") {
        rotate_axis(draft.model, Slic3r::X, Slic3r::Geometry::deg2rad(90.0));
    } else if (req.action == "rotate_x_minus_90") {
        rotate_axis(draft.model, Slic3r::X, Slic3r::Geometry::deg2rad(-90.0));
    } else if (req.action == "rotate_y_90") {
        rotate_axis(draft.model, Slic3r::Y, Slic3r::Geometry::deg2rad(90.0));
    } else if (req.action == "rotate_y_minus_90") {
        rotate_axis(draft.model, Slic3r::Y, Slic3r::Geometry::deg2rad(-90.0));
    } else if (req.action == "rotate_z_90") {
        rotate_axis(draft.model, Slic3r::Z, Slic3r::Geometry::deg2rad(90.0));
    } else if (req.action == "rotate_z_minus_90") {
        rotate_axis(draft.model, Slic3r::Z, Slic3r::Geometry::deg2rad(-90.0));
    } else if (req.action == "reset") {
        reset_layout(draft.model, draft.config);
    } else if (req.action == "auto_orient") {
        try {
            auto_orient_all(draft.model, draft.config);
        } catch (const std::exception& e) {
            return fail("auto_orient_failed", e.what(), response);
        }
    } else if (req.action == "arrange") {
        std::string error;
        if (!arrange_draft_instances(draft.model, draft.config, error)) {
            return fail("arrange_failed", error, response);
        }
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
    response.scene = scene_for_model(
        req, draft.model, draft.config, nlohmann::json::array());
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
