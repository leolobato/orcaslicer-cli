#include "use_set_mode.h"

#include "libslic3r/Model.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Utils.hpp"

#include <filesystem>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace orca_headless {

namespace {

// Snapshot of the global filament-key defaults that the GUI's
// `PartPlate::get_extruders_under_cli` reads from the project config.
// All fields are 1-based filament IDs (libslic3r convention).
//   - support_interface / support: 0 means "unset / no per-object support"
//   - wall / sparse_infill / solid_infill: 1 means "use the volume's
//     default extruder (slot 0)", anything else is an explicit override
//   - support_enabled mirrors `enable_support || raft_layers > 0`
struct GlobalFilamentDefaults {
    int  support_intf = 0;
    int  support = 0;
    int  wall = 1;
    int  sparse_infill = 1;
    int  solid_infill = 1;
    bool support_enabled = false;
    size_t num_filaments = 1;
};

GlobalFilamentDefaults read_global_defaults(
    const Slic3r::DynamicPrintConfig& cfg) {
    GlobalFilamentDefaults g;
    if (cfg.has("support_interface_filament"))
        g.support_intf = cfg.opt_int("support_interface_filament");
    if (cfg.has("support_filament"))
        g.support = cfg.opt_int("support_filament");
    if (cfg.has("wall_filament"))
        g.wall = cfg.opt_int("wall_filament");
    if (cfg.has("sparse_infill_filament"))
        g.sparse_infill = cfg.opt_int("sparse_infill_filament");
    if (cfg.has("solid_infill_filament"))
        g.solid_infill = cfg.opt_int("solid_infill_filament");
    if (cfg.has("enable_support"))
        g.support_enabled = cfg.opt_bool("enable_support");
    if (cfg.has("raft_layers") && cfg.opt_int("raft_layers") > 0)
        g.support_enabled = true;
    if (const auto* colors = cfg.option<Slic3r::ConfigOptionStrings>("filament_colour"))
        g.num_filaments = colors->values.size();
    return g;
}

int object_int_opt(const Slic3r::ModelObject& obj,
                   const std::string& key, int fallback) {
    const Slic3r::ConfigOption* opt = obj.config.option(key);
    return opt ? opt->getInt() : fallback;
}

bool object_bool_opt(const Slic3r::ModelObject& obj,
                     const std::string& key, bool fallback) {
    const Slic3r::ConfigOption* opt = obj.config.option(key);
    return opt ? opt->getBool() : fallback;
}

// Decide whether support is enabled for this object (per-object override
// → global). Mirrors the per-object/global priority that the GUI's
// `PartPlate::get_extruders_under_cli` applies before walking
// support_filament / support_interface_filament.
bool object_support_enabled(const Slic3r::ModelObject& obj,
                            const GlobalFilamentDefaults& glb) {
    const Slic3r::ConfigOption* support_opt = obj.config.option("enable_support");
    const Slic3r::ConfigOption* raft_opt    = obj.config.option("raft_layers");
    if (!support_opt && !raft_opt) {
        return glb.support_enabled;
    }
    bool enabled = false;
    if (support_opt) enabled = support_opt->getBool();
    if (raft_opt)    enabled = enabled || (raft_opt->getInt() > 0);
    return enabled;
}

// Collect 0-based filament slots referenced by one object instance.
// Faithful port of the GUI's `PartPlate::get_extruders_under_cli`
// (PartPlate.cpp:1614) for one object: walks volumes (paint + default
// extruder), layer_config_ranges, per-object support config, and
// per-object wall/sparse_infill/solid_infill_filament with
// global-config fallbacks.
std::set<int> object_filament_indices(
    const Slic3r::ModelObject& obj,
    const Slic3r::ModelInstance* instance,
    const GlobalFilamentDefaults& glb) {
    std::set<int> out;
    if (instance && !instance->printable) return out;

    // Per-volume: paint walk + volume default extruder. ModelVolume's
    // `get_extruders()` already covers both and excludes negative /
    // support-blocker / support-enforcer volumes.
    for (const Slic3r::ModelVolume* mv : obj.volumes) {
        if (!mv) continue;
        for (int e : mv->get_extruders()) {
            if (e >= 1) out.insert(e - 1);
        }
    }

    // Layer-range "use extruder N from Z=A to Z=B" overrides.
    for (const auto& lr : obj.layer_config_ranges) {
        if (lr.second.has("extruder")) {
            const int id = lr.second.option("extruder")->getInt();
            if (id > 0) out.insert(id - 1);
        }
    }

    // Support filament keys land only when support is actually on for
    // this object. Per-object override beats per-object's missing key
    // beats global default.
    if (object_support_enabled(obj, glb)) {
        const int sup_intf = object_int_opt(obj, "support_interface_filament", 0);
        const int sup_intf_eff = sup_intf != 0 ? sup_intf : glb.support_intf;
        if (sup_intf_eff != 0) out.insert(sup_intf_eff - 1);

        const int sup = object_int_opt(obj, "support_filament", 0);
        const int sup_eff = sup != 0 ? sup : glb.support;
        if (sup_eff != 0) out.insert(sup_eff - 1);
    }

    // wall / sparse_infill / solid_infill: default sentinel is 1 ("use
    // the volume default = slot 0"). Anything other than 1 surfaces a
    // distinct slot. The GUI dedupes; we use a set so duplicates fold.
    auto add_default_fallback = [&](const std::string& key, int glb_val) {
        const int obj_val = object_int_opt(obj, key, 1);
        const int eff = obj_val != 1 ? obj_val : glb_val;
        if (eff != 1) out.insert(eff - 1);
    };
    add_default_fallback("wall_filament", glb.wall);
    add_default_fallback("sparse_infill_filament", glb.sparse_infill);
    add_default_fallback("solid_infill_filament", glb.solid_infill);

    return out;
}

// Add 0-based slots referenced by manual per-plate ToolChange custom
// gcode events (the GUI surfaces these via `consider_custom_gcode=true`
// in `PartPlate::get_extruders_under_cli`).
void add_custom_gcode_slots(
    const Slic3r::Model& model,
    int plate_index_zero_based,
    size_t num_filaments,
    std::set<int>& out) {
    auto it = model.plates_custom_gcodes.find(plate_index_zero_based);
    if (it == model.plates_custom_gcodes.end()) return;
    for (const auto& item : it->second.gcodes) {
        if (item.type != Slic3r::CustomGCode::Type::ToolChange) continue;
        const int e = item.extruder;
        if (e >= 1 && static_cast<size_t>(e) <= num_filaments) {
            out.insert(e - 1);
        }
    }
}

int fail(const std::string& code, const std::string& message,
         UseSetResponse& r) {
    r.status = "error";
    r.error_code = code;
    r.error_message = message;
    write_use_set_response_to_stdout(r);
    return 1;
}

}  // namespace

int run_use_set_mode(const UseSetRequest& req) {
    UseSetResponse response;

    if (Slic3r::temporary_dir().empty()) {
        std::error_code ec;
        std::filesystem::path tmp = std::filesystem::temp_directory_path(ec);
        if (ec || tmp.empty()) tmp = "/tmp";
        Slic3r::set_temporary_dir(tmp.string());
    }

    Slic3r::DynamicPrintConfig cfg;
    Slic3r::ConfigSubstitutionContext subs(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    Slic3r::PlateDataPtrs plate_data;
    std::vector<Slic3r::Preset*> project_presets;

    Slic3r::Model model;
    try {
        model = Slic3r::Model::read_from_file(
            req.input_3mf, &cfg, &subs,
            Slic3r::LoadStrategy::LoadModel
                | Slic3r::LoadStrategy::LoadConfig,
            &plate_data, &project_presets);
    } catch (const std::exception& e) {
        return fail("invalid_3mf",
                    std::string("read_from_file: ") + e.what(), response);
    }

    const GlobalFilamentDefaults glb = read_global_defaults(cfg);

    // Build (plate_id_1based → set of (obj_idx, instance_idx)) from
    // PlateData. For un-sliced 3MFs that don't carry plate metadata,
    // fall back to "every object → plate 1, instance 0".
    std::map<int, std::vector<std::pair<int, int>>> plate_assignments;
    bool have_plate_metadata = false;
    for (size_t i = 0; i < plate_data.size(); ++i) {
        const auto* pd = plate_data[i];
        if (!pd) continue;
        if (pd->objects_and_instances.empty()) continue;
        const int plate_id = pd->plate_index + 1;
        for (const auto& pair : pd->objects_and_instances) {
            plate_assignments[plate_id].push_back(pair);
        }
        have_plate_metadata = true;
    }
    if (!have_plate_metadata) {
        for (size_t obj_idx = 0; obj_idx < model.objects.size(); ++obj_idx) {
            const auto* obj = model.objects[obj_idx];
            if (!obj) continue;
            for (size_t inst_idx = 0; inst_idx < obj->instances.size(); ++inst_idx) {
                plate_assignments[1].push_back(
                    {static_cast<int>(obj_idx), static_cast<int>(inst_idx)});
            }
        }
    }

    std::map<int, std::set<int>> plate_to_indices;
    for (const auto& [plate_id, pairs] : plate_assignments) {
        std::set<int>& bucket = plate_to_indices[plate_id];
        for (const auto& [obj_idx, inst_idx] : pairs) {
            if (obj_idx < 0 || static_cast<size_t>(obj_idx) >= model.objects.size())
                continue;
            const Slic3r::ModelObject* obj = model.objects[obj_idx];
            if (!obj) continue;
            const Slic3r::ModelInstance* inst = nullptr;
            if (inst_idx >= 0
                && static_cast<size_t>(inst_idx) < obj->instances.size()) {
                inst = obj->instances[inst_idx];
            }
            const auto slots = object_filament_indices(*obj, inst, glb);
            bucket.insert(slots.begin(), slots.end());
        }
        // Custom gcode lookup is keyed by 0-based plate index.
        add_custom_gcode_slots(model, plate_id - 1, glb.num_filaments, bucket);
    }

    if (plate_to_indices.empty()) {
        // No volumes — still emit plate 1 with the trivial slot 0 used
        // so the response always has something the gateway can render.
        plate_to_indices[1].insert(0);
    }

    response.status = "ok";
    for (const auto& [plate_id, indices] : plate_to_indices) {
        UseSetPlateInfo info;
        info.plate_id = plate_id;
        info.used_filament_indices.assign(indices.begin(), indices.end());
        response.plates.push_back(info);
    }
    write_use_set_response_to_stdout(response);
    return 0;
}

}  // namespace orca_headless
