#include "json_io.h"

#include <cerrno>
#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>
#include <unistd.h>

using nlohmann::json;

namespace orca_headless {

namespace {
// Saved real stdout fd. -1 until ``redirect_libslic3r_stdout_pollution`` runs
// successfully. Response writers fall back to ``std::cout`` when -1.
int g_real_stdout_fd = -1;

// Best-effort raw write to the saved real stdout fd. Loops over partial
// writes / EINTR until either the buffer is fully written or write fails.
// Used by ``write_envelope_line`` below; never throws.
void write_all_or_drop(int fd, const char* buf, size_t len) {
    while (len > 0) {
        ssize_t n = ::write(fd, buf, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            return;  // pipe broken or fd bad — drop silently, caller already best-effort
        }
        buf += n;
        len -= static_cast<size_t>(n);
    }
}
}  // namespace

void write_envelope_line(const json& out) {
    std::string s = out.dump();
    s.push_back('\n');
    if (g_real_stdout_fd >= 0) {
        write_all_or_drop(g_real_stdout_fd, s.data(), s.size());
    } else {
        std::cout << s << std::flush;
    }
}

int redirect_libslic3r_stdout_pollution() {
    if (g_real_stdout_fd >= 0) return g_real_stdout_fd;  // idempotent
    int saved = ::dup(STDOUT_FILENO);
    if (saved < 0) {
        std::fprintf(stderr,
            "warn: dup(STDOUT) failed (errno=%d); libslic3r stdout pollution may corrupt JSON protocol\n",
            errno);
        return -1;
    }
    if (::dup2(STDERR_FILENO, STDOUT_FILENO) < 0) {
        std::fprintf(stderr,
            "warn: dup2(STDERR, STDOUT) failed (errno=%d); libslic3r stdout pollution may corrupt JSON protocol\n",
            errno);
        ::close(saved);
        return -1;
    }
    g_real_stdout_fd = saved;
    return saved;
}

int real_stdout_fd() { return g_real_stdout_fd; }


SliceRequest parse_slice_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());

    SliceRequest req;
    req.input_3mf          = j.at("input_3mf").get<std::string>();
    req.output_3mf         = j.at("output_3mf").get<std::string>();
    req.machine_chain_dir  = j.at("machine_chain_dir").get<std::string>();
    req.process_chain_dir  = j.at("process_chain_dir").get<std::string>();
    req.filament_chain_dir = j.at("filament_chain_dir").get<std::string>();
    req.machine_leaf_name  = j.at("machine_leaf_name").get<std::string>();
    req.process_leaf_name  = j.at("process_leaf_name").get<std::string>();
    req.plate_id           = j.value("plate_id", 1);
    if (j.contains("options")) {
        req.auto_center = j["options"].value("auto_center", true);
        req.copies = j["options"].value("copies", 1);
        if (req.copies < 1) req.copies = 1;
        if (req.copies > 100) req.copies = 100;
    }
    if (j.contains("filament_map") && j["filament_map"].is_array()) {
        req.filament_map = j["filament_map"].get<std::vector<int>>();
    }
    if (j.contains("filament_settings_id") && j["filament_settings_id"].is_array()) {
        req.filament_settings_id = j["filament_settings_id"].get<std::vector<std::string>>();
    }
    req.printer_model_id = j.value("printer_model_id", std::string());
    req.plate_type = j.value("plate_type", std::string());
    if (j.contains("process_overrides") && j["process_overrides"].is_object()) {
        for (const auto& [k, v] : j["process_overrides"].items()) {
            if (v.is_string()) {
                req.process_overrides.emplace(k, v.get<std::string>());
            }
            // Non-string values are silently dropped — the contract
            // requires stringified config values (matches project_settings.config).
        }
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
            {"prepare_seconds", r.estimate.prepare_seconds},
            {"weight_g", r.estimate.weight_g},
            {"model_weight_g", r.estimate.model_weight_g},
            {"filament_used_m", r.estimate.filament_used_m},
            {"model_filament_used_m", r.estimate.model_filament_used_m},
        };
        out["settings_transfer"] = r.settings_transfer;
    } else {
        out["code"] = r.error_code;
        out["message"] = r.error_message;
        out["details"] = r.error_details;
    }
    write_envelope_line(out);
}

UseSetRequest parse_use_set_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());
    UseSetRequest req;
    req.input_3mf = j.at("input_3mf").get<std::string>();
    return req;
}

void write_use_set_response_to_stdout(const UseSetResponse& r) {
    json out;
    out["status"] = r.status;
    if (r.status == "ok") {
        json plates = json::array();
        for (const auto& p : r.plates) {
            plates.push_back({
                {"id", p.plate_id},
                {"used_filament_indices", p.used_filament_indices},
            });
        }
        out["plates"] = plates;
    } else {
        out["code"] = r.error_code;
        out["message"] = r.error_message;
        out["details"] = r.error_details;
    }
    write_envelope_line(out);
}

DumpProfilesRequest parse_dump_profiles_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());
    DumpProfilesRequest req;
    req.profiles_dir = j.value("profiles_dir", std::string{});
    req.user_dir     = j.value("user_dir",     std::string{});
    req.out_path     = j.value("out_path",     std::string{});
    if (req.profiles_dir.empty() || req.out_path.empty())
        throw std::runtime_error(
            "dump-profiles: profiles_dir and out_path are required");
    return req;
}

DumpOptionsRequest parse_dump_options_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());
    DumpOptionsRequest req;
    req.out_path = j.value("out_path", std::string{});
    if (req.out_path.empty())
        throw std::runtime_error("dump-options: out_path is required");
    return req;
}

StlDraftRequest parse_stl_draft_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());

    StlDraftRequest req;
    req.operation = j.value("operation", std::string{});
    req.draft_token = j.value("draft_token", std::string{});
    req.source_filename = j.value("source_filename", std::string{});
    req.input_stl = j.value("input_stl", std::string{});
    req.input_3mf = j.value("input_3mf", std::string{});
    req.output_3mf = j.value("output_3mf", std::string{});
    req.action = j.value("action", std::string{});
    req.machine_chain_dir = j.value("machine_chain_dir", std::string{});
    req.process_chain_dir = j.value("process_chain_dir", std::string{});
    req.machine_leaf_name = j.value("machine_leaf_name", std::string{});
    req.process_leaf_name = j.value("process_leaf_name", std::string{});
    req.plate_type = j.value("plate_type", std::string{});
    if (j.contains("options") && j["options"].is_object()) {
        req.auto_orient = j["options"].value("auto_orient", false);
        req.arrange = j["options"].value("arrange", true);
        req.center = j["options"].value("center", true);
    }
    return req;
}

void write_stl_draft_response_to_stdout(const StlDraftResponse& r) {
    json out;
    out["status"] = r.status;
    if (r.status == "ok") {
        out["scene"] = r.scene;
    } else {
        out["code"] = r.error_code;
        out["message"] = r.error_message;
        out["details"] = r.error_details;
    }
    write_envelope_line(out);
}

}  // namespace orca_headless
