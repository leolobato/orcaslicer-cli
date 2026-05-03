#include "use_set_mode.h"

#include "libslic3r/Model.hpp"
#include "libslic3r/TriangleSelector.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Utils.hpp"

#include <filesystem>
#include <map>
#include <set>
#include <string>

namespace orca_headless {

namespace {

// Extract filament slot indices referenced by a ModelVolume:
// - the volume's default extruder (the `extruder` config option,
//   1-based; "" or "0" means "use object default = slot 0")
// - every state present in `mmu_segmentation_facets.used_states`
//   (the bitmask of which `EnforcerBlockerType::ExtruderN` values
//   appear in the painted facet bitstream)
//
// EnforcerBlockerType::Extruder1 == 1 maps to slot 0; Extruder2 == 2
// maps to slot 1; etc. The 0th element of `used_states` is NONE
// (unpainted background) which we always treat as slot 0 implicitly.
std::set<int> volume_filament_indices(const Slic3r::ModelVolume& vol) {
    std::set<int> out;
    // Default extruder is 1-based; 0 means "inherit object default".
    const int extruder = vol.extruder_id();
    if (extruder >= 1) {
        out.insert(extruder - 1);
    } else {
        out.insert(0);
    }
    if (vol.is_mm_painted()) {
        const auto& data = vol.mmu_segmentation_facets.get_data();
        for (size_t state = 1; state < data.used_states.size(); ++state) {
            if (data.used_states[state]) {
                // state index N corresponds to EnforcerBlockerType::ExtruderN,
                // which maps to filament slot N-1.
                out.insert(static_cast<int>(state) - 1);
            }
        }
    }
    return out;
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

    // Plate assignment: PlateData carries `objects_and_instances`, but on
    // an unsliced 3MF the list is empty. For Phase 1 we conservatively
    // bucket every object into plate 1 (matching the single-plate
    // assumption the rest of the binary makes). Multi-plate dispatch
    // lands when a multi-plate fixture exists.
    std::map<int, std::set<int>> plate_to_indices;
    for (const auto& obj : model.objects) {
        if (!obj) continue;
        for (const auto& vol : obj->volumes) {
            if (!vol) continue;
            for (int i : volume_filament_indices(*vol)) {
                plate_to_indices[1].insert(i);
            }
        }
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
