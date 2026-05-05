#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace orca_headless {

struct SliceRequest {
    std::string input_3mf;
    std::string output_3mf;

    // Per-category directories holding one JSON per chain link (leaf +
    // every ancestor) in the inheritance closure. The binary loads each
    // via ``PresetCollection::load_preset`` so a real ``PresetBundle``
    // can resolve ``get_selected_preset_parent`` /
    // ``dirty_options_without_option_list`` natively. Python pre-resolves
    // each link's flat config so each loaded preset is standalone.
    std::string machine_chain_dir;
    std::string process_chain_dir;
    std::string filament_chain_dir;  // shared across slots, dedup'd by name

    // Leaf preset names — what the bundle's ``select_preset_by_name``
    // (machine, process) and ``filament_presets`` (per slot) reference.
    std::string machine_leaf_name;
    std::string process_leaf_name;

    int plate_id = 1;
    bool recenter = true;

    // Optional: explicit AMS slot per filament index. Empty = no override.
    std::vector<int> filament_map;

    // Selected filament leaf names per slot — populates
    // ``bundle.filament_presets``. Doubles as the name-guard target for
    // 3MF per-slot overrides (when the user swaps to a different filament
    // than the one the 3MF authored, the customizations are discarded).
    std::vector<std::string> filament_settings_id;

    // Optional: BBL printer model_id (e.g. "N1" for A1 mini). Stamped onto
    // PlateData::printer_model_id so it surfaces in slice_info.config.
    // Empty for non-BBL vendors.
    std::string printer_model_id;

    // Optional: OrcaSlicer ``curr_bed_type`` label (e.g. "Textured PEI Plate").
    // When non-empty, overrides whatever the input 3MF authored — necessary
    // because the GUI lets the user re-pick the plate type independently of
    // the project file. Caller is expected to validate the value against the
    // target machine's supported list before sending.
    std::string plate_type;
};

struct SliceResponseEstimate {
    // Total wall-clock time the printer will spend on this job, including
    // prepare moves (purge tower, initial homing, etc.).
    double time_seconds = 0.0;
    // Time spent on prepare moves only (everything before the model
    // starts printing). Sourced from
    // ``gcode_result.print_statistics.modes[Normal].prepare_time``.
    double prepare_seconds = 0.0;
    // Total grams of filament. Sum across all slots.
    double weight_g = 0.0;
    // Grams of filament actually deposited on the model (excludes purge
    // tower / flush). Approximated from the wipe-tower extrusion ratio
    // since libslic3r doesn't carry a direct "model weight" field.
    double model_weight_g = 0.0;
    // Per-slot filament used in metres (TOTAL — includes purge tower).
    std::vector<double> filament_used_m;
    // Per-slot filament used in metres for the model only (excludes
    // purge tower extrusions).
    std::vector<double> model_filament_used_m;
};

struct SliceResponse {
    std::string status;            // "ok" or "error"
    std::string output_3mf;
    SliceResponseEstimate estimate;
    nlohmann::json settings_transfer = nlohmann::json::object();

    // Populated only when status == "error".
    std::string error_code;
    std::string error_message;
    nlohmann::json error_details = nlohmann::json::object();
};

struct UseSetRequest {
    std::string input_3mf;
};

struct UseSetPlateInfo {
    int plate_id = 0;          // 1-based
    std::vector<int> used_filament_indices;  // 0-based, sorted
};

struct UseSetResponse {
    std::string status;        // "ok" or "error"
    std::vector<UseSetPlateInfo> plates;
    std::string error_code;
    std::string error_message;
    nlohmann::json error_details = nlohmann::json::object();
};

SliceRequest parse_slice_request_from_stdin();
void write_slice_response_to_stdout(const SliceResponse& r);

UseSetRequest parse_use_set_request_from_stdin();
void write_use_set_response_to_stdout(const UseSetResponse& r);

}  // namespace orca_headless
