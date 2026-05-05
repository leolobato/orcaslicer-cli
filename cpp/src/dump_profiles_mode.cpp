#include "dump_profiles_mode.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <random>
#include <unordered_map>
#include <unordered_set>

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

// Scalar-or-first-of-vector serialization for keys the GUI stores as
// vectors-with-one-element on simple printers (``nozzle_diameter`` on
// single-nozzle machines) but as scalars on processes (``layer_height``).
// Mirrors the list-or-string handling Python does in
// ``app/profiles.py::get_machine_profiles``.
std::string opt_first_string(
    const Slic3r::DynamicPrintConfig& cfg, const char* key) {
    const auto* opt = cfg.option(key);
    if (!opt) return "";
    if (opt->is_vector()) {
        if (opt->is_nil()) return "";
        const auto s = opt->serialize();
        const auto sep = s.find_first_of(",;");
        return sep == std::string::npos ? s : s.substr(0, sep);
    }
    return opt->serialize();
}

void emit_machines(const Slic3r::PresetBundle& bundle, nlohmann::json& out) {
    for (const auto& preset : bundle.printers) {
        // ``load_vendor_configs_from_json`` only loads instantiated
        // presets into the collection — non-instantiated parents stay
        // in the bundle's internal ``m_config_maps``. So every entry
        // here is what the API would have surfaced via
        // ``instantiation == "true"``.
        nlohmann::json e;
        e["setting_id"]      = preset.setting_id;
        e["name"]            = preset.name;
        // Use VendorProfile::id (directory name like "BBL") not ::name
        // (display "Bambulab") to match the API contract Python emitted.
        e["vendor"]          = preset.vendor ? preset.vendor->id : std::string{};
        e["nozzle_diameter"] = opt_first_string(preset.config, "nozzle_diameter");
        e["printer_model"]   = preset.config.opt_string("printer_model");
        out.push_back(std::move(e));
    }
}

// Build a name → setting_id index for the loaded printers. The raw
// ``compatible_printers`` field on each filament/process holds printer
// preset NAMES (e.g. "Bambu Lab A1 mini 0.4 nozzle"); the API exposes
// stable setting_ids. Names not found here are dropped — Python's
// listing functions do the same (``_select_profile_key_by_name``).
std::unordered_map<std::string, std::string>
build_printer_name_to_setting_id(const Slic3r::PresetBundle& bundle) {
    std::unordered_map<std::string, std::string> out;
    for (const auto& p : bundle.printers) {
        if (!p.setting_id.empty())
            out.emplace(p.name, p.setting_id);
    }
    return out;
}

nlohmann::json compat_printer_setting_ids(
    const Slic3r::DynamicPrintConfig& cfg,
    const std::unordered_map<std::string, std::string>& name_to_id) {
    nlohmann::json arr = nlohmann::json::array();
    const auto* opt = cfg.option<Slic3r::ConfigOptionStrings>(
        "compatible_printers");
    if (!opt) return arr;
    std::unordered_set<std::string> seen;
    for (const auto& name : opt->values) {
        const auto it = name_to_id.find(name);
        if (it == name_to_id.end() || it->second.empty()) continue;
        if (seen.insert(it->second).second)
            arr.push_back(it->second);
    }
    return arr;
}

void emit_processes(const Slic3r::PresetBundle& bundle, nlohmann::json& out) {
    const auto name_to_id = build_printer_name_to_setting_id(bundle);
    for (const auto& preset : bundle.prints) {
        nlohmann::json e;
        e["setting_id"]          = preset.setting_id;
        e["name"]                = preset.name;
        e["vendor"]              = preset.vendor ? preset.vendor->id : std::string{};
        e["compatible_printers"] = compat_printer_setting_ids(preset.config, name_to_id);
        e["layer_height"]        = opt_first_string(preset.config, "layer_height");
        out.push_back(std::move(e));
    }
}

void emit_filaments(const Slic3r::PresetBundle& bundle, nlohmann::json& out) {
    const auto name_to_id = build_printer_name_to_setting_id(bundle);
    for (const auto& preset : bundle.filaments) {
        nlohmann::json e;
        e["setting_id"]          = preset.setting_id;
        e["filament_id"]         = preset.filament_id;
        e["name"]                = preset.name;
        e["vendor"]              = preset.vendor ? preset.vendor->id : std::string{};
        e["compatible_printers"] = compat_printer_setting_ids(preset.config, name_to_id);
        e["filament_type"]       = opt_first_string(preset.config, "filament_type");
        // AMS assignability matches Python's
        // ``_is_ams_assignable_filament``: instantiated (already
        // filtered by load_vendor_configs_from_json), with non-empty
        // setting_id AND filament_id. User-imported filaments without
        // a generated filament_id are non-assignable.
        e["ams_assignable"] =
            !preset.setting_id.empty() && !preset.filament_id.empty();
        out.push_back(std::move(e));
    }
}

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

    // Absorb user-imported presets (filaments authored via
    // /profiles/filaments/import, or any per-user machine/process JSON
    // dropped under USER_PROFILES_DIR). PresetCollection::load_presets
    // recurses into <dir>/<subdir>/base/ automatically — matches the
    // existing layout written by ``materialize_filament_import``.
    if (!req.user_dir.empty() &&
        boost::filesystem::exists(req.user_dir)) {
        Slic3r::PresetsConfigSubstitutions subs;
        const auto rule =
            Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent;
        try {
            const boost::filesystem::path user(req.user_dir);
            if (boost::filesystem::exists(user / "machine"))
                bundle.printers.load_presets(req.user_dir, "machine", subs, rule);
            if (boost::filesystem::exists(user / "process"))
                bundle.prints.load_presets(req.user_dir, "process", subs, rule);
            if (boost::filesystem::exists(user / "filament"))
                bundle.filaments.load_presets(req.user_dir, "filament", subs, rule);
        } catch (const std::exception& e) {
            return fail("user_load_failed", e.what());
        }
    }

    nlohmann::json manifest;
    manifest["machines"]  = nlohmann::json::array();
    emit_machines(bundle, manifest["machines"]);
    manifest["processes"] = nlohmann::json::array();
    emit_processes(bundle, manifest["processes"]);
    manifest["filaments"] = nlohmann::json::array();
    emit_filaments(bundle, manifest["filaments"]);

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
