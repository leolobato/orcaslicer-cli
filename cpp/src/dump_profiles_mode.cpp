#include "dump_profiles_mode.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <random>

#include <boost/filesystem.hpp>
#include <boost/system/error_code.hpp>
#include <nlohmann/json.hpp>

#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Utils.hpp"

// Use ``boost::filesystem`` (libslic3r's choice) directly to avoid
// colliding with the ``namespace fs = boost::filesystem;`` alias the
// libslic3r headers leak into TUs that include them.

namespace orca_headless {

namespace {

// RAII shim that lets us call ``PresetBundle::load_system_presets_from_json``
// (which reads from ``data_dir() / PRESET_SYSTEM_DIR``) against our actual
// profiles dir. We make a temp ``data_dir`` whose ``system`` entry is a
// symlink to the real ``profiles_dir``, point libslic3r's runtime
// ``set_data_dir`` at the temp dir, and clean up on scope exit. This is
// faster than copying the 60+ vendor metadata trees and uses libslic3r's
// authoritative load path unmodified.
class DataDirShim {
public:
    explicit DataDirShim(const std::string& real_profiles_dir) {
        std::random_device rd;
        std::mt19937_64 rng(rd());
        char buf[64];
        std::snprintf(buf, sizeof(buf), "orca-headless-dump-%016llx",
                      static_cast<unsigned long long>(rng()));
        tmpdir_ = boost::filesystem::temp_directory_path() / buf;
        boost::filesystem::create_directories(tmpdir_);
        // Symlink target must be absolute for libslic3r's relative path
        // resolution downstream to behave identically to a real install.
        boost::filesystem::create_directory_symlink(
            boost::filesystem::absolute(real_profiles_dir),
            tmpdir_ / "system");
        prev_data_dir_ = Slic3r::data_dir();
        Slic3r::set_data_dir(tmpdir_.string());
    }
    ~DataDirShim() {
        Slic3r::set_data_dir(prev_data_dir_);
        boost::system::error_code ec;
        boost::filesystem::remove_all(tmpdir_, ec);
    }
    DataDirShim(const DataDirShim&) = delete;
    DataDirShim& operator=(const DataDirShim&) = delete;
private:
    boost::filesystem::path tmpdir_;
    std::string             prev_data_dir_;
};

}  // namespace

int run_dump_profiles_mode(const DumpProfilesRequest& req) {
    auto fail = [&](const char* code, const std::string& msg) {
        nlohmann::json err = {
            {"status",  "error"},
            {"code",    code},
            {"message", msg},
            {"details", nlohmann::json::object()},
        };
        std::cout << err.dump() << std::endl;
        return 1;
    };

    if (!boost::filesystem::exists(req.profiles_dir))
        return fail("invalid_request",
                    "profiles_dir does not exist: " + req.profiles_dir);

    Slic3r::PresetBundle bundle;
    try {
        DataDirShim shim(req.profiles_dir);
        auto subs_pair = bundle.load_system_presets_from_json(
            Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
        if (!subs_pair.second.empty()) {
            // libslic3r writes per-vendor parse warnings as a single
            // accumulated string. Surface them on stderr for triage but
            // keep going — the GUI itself does the same.
            std::fprintf(stderr,
                "[dump-profiles] load_system_presets_from_json reported errors:\n%s\n",
                subs_pair.second.c_str());
        }
    } catch (const std::exception& e) {
        return fail("system_load_failed", e.what());
    }

    nlohmann::json manifest;
    manifest["machines"]  = nlohmann::json::array();
    manifest["processes"] = nlohmann::json::array();
    manifest["filaments"] = nlohmann::json::array();

    std::ofstream ofs(req.out_path);
    if (!ofs) return fail("io_error",
                          "cannot open out_path for write: " + req.out_path);
    ofs << manifest.dump();

    nlohmann::json ok = {
        {"status",  "ok"},
        {"code",    "done"},
        {"message", ""},
        {"details", {
            {"out_path", req.out_path},
            {"counts", {
                {"machines",  bundle.printers.size()},
                {"processes", bundle.prints.size()},
                {"filaments", bundle.filaments.size()},
            }},
        }},
    };
    std::cout << ok.dump() << std::endl;
    return 0;
}

}  // namespace orca_headless
