#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace orca_headless {

struct SliceRequest {
    std::string input_3mf;
    std::string output_3mf;
    std::string machine_profile;
    std::string process_profile;
    std::vector<std::string> filament_profiles;
    int plate_id = 1;
    bool recenter = true;

    // Optional: explicit AMS slot per filament index. Empty = no override.
    std::vector<int> filament_map;

    // Optional: explicit selected filament names per slot, used by the
    // project-overrides pass to decide whether per-filament customizations
    // from the 3MF apply (name match) or get discarded (filament swapped).
    std::vector<std::string> filament_settings_id;

    // Optional: BBL printer model_id (e.g. "N1" for A1 mini). Stamped onto
    // PlateData::printer_model_id so it surfaces in slice_info.config.
    // Empty for non-BBL vendors.
    std::string printer_model_id;
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
