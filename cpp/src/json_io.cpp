#include "json_io.h"

#include <iostream>
#include <sstream>

using nlohmann::json;

namespace orca_headless {

SliceRequest parse_slice_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());

    SliceRequest req;
    req.input_3mf        = j.at("input_3mf").get<std::string>();
    req.output_3mf       = j.at("output_3mf").get<std::string>();
    req.machine_profile  = j.at("machine_profile").get<std::string>();
    req.process_profile  = j.at("process_profile").get<std::string>();
    req.filament_profiles = j.at("filament_profiles").get<std::vector<std::string>>();
    req.plate_id         = j.value("plate_id", 1);
    if (j.contains("options")) {
        req.recenter = j["options"].value("recenter", true);
    }
    if (j.contains("filament_map") && j["filament_map"].is_array()) {
        req.filament_map = j["filament_map"].get<std::vector<int>>();
    }
    if (j.contains("filament_settings_id") && j["filament_settings_id"].is_array()) {
        req.filament_settings_id = j["filament_settings_id"].get<std::vector<std::string>>();
    }
    return req;
}

void write_slice_response_to_stdout(const SliceResponse& r) {
    json out;
    out["status"] = r.status;
    if (r.status == "ok") {
        out["output_3mf"] = r.output_3mf;
        out["estimate"] = {
            {"time_seconds", r.estimate.time_seconds},
            {"weight_g", r.estimate.weight_g},
            {"filament_used_m", r.estimate.filament_used_m},
        };
        out["settings_transfer"] = r.settings_transfer;
    } else {
        out["code"] = r.error_code;
        out["message"] = r.error_message;
        out["details"] = r.error_details;
    }
    std::cout << out.dump() << std::endl;
}

}  // namespace orca_headless
