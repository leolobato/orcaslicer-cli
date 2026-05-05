#include "dump_profiles_mode.h"

#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>

namespace orca_headless {

int run_dump_profiles_mode(const DumpProfilesRequest& req) {
    nlohmann::json manifest;
    manifest["machines"]  = nlohmann::json::array();
    manifest["processes"] = nlohmann::json::array();
    manifest["filaments"] = nlohmann::json::array();

    std::ofstream ofs(req.out_path);
    if (!ofs) {
        nlohmann::json err = {
            {"status",  "error"},
            {"code",    "io_error"},
            {"message", "cannot open out_path for write"},
            {"details", {{"path", req.out_path}}},
        };
        std::cout << err.dump() << std::endl;
        return 1;
    }
    ofs << manifest.dump();

    nlohmann::json ok = {
        {"status",  "ok"},
        {"code",    "done"},
        {"message", ""},
        {"details", {
            {"out_path", req.out_path},
            {"counts", {
                {"machines",  0},
                {"processes", 0},
                {"filaments", 0},
            }},
        }},
    };
    std::cout << ok.dump() << std::endl;
    return 0;
}

}  // namespace orca_headless
