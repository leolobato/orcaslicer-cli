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
};

struct SliceResponseEstimate {
    double time_seconds = 0.0;
    double weight_g = 0.0;
    std::vector<double> filament_used_m;
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

SliceRequest parse_slice_request_from_stdin();
void write_slice_response_to_stdout(const SliceResponse& r);

}  // namespace orca_headless
