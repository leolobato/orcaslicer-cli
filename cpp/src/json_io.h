#pragma once

#include <nlohmann/json.hpp>
#include <map>
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
    bool auto_center = true;

    // Number of copies of each ModelObject to slice. 1 = original
    // behavior (no extra instances created). When > 1, the binary
    // duplicates each ModelObject's last instance copies-1 times
    // (mirroring Plater::increase_instances in
    // OrcaSlicer/src/slic3r/GUI/Plater.cpp:14255), then runs
    // libslic3r's arrangement::arrange to pack the result on the bed
    // (mirroring Plater::find_new_position in Plater.cpp:7362). If any
    // instance can't be placed, slicing fails with copies_dont_fit.
    // auto_center is suppressed when copies > 1 because arrange
    // inherently centers the packed result.
    int copies = 1;

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

    // Optional client-side process-domain customisations applied AFTER
    // the 3MF's own different_settings_to_system[0] overlay. Highest
    // priority — these win over both the system process profile and the
    // 3MF's customisations.
    //
    // Keys must be process-domain options (filament_*/__filament keys
    // are silently dropped by the overlay). String values match the
    // OrcaSlicer config-string convention used in project_settings.config.
    //
    // Reported back via ``settings_transfer.process_overrides_applied``.
    std::map<std::string, std::string> process_overrides;
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

// Stand up a libslic3r ``PresetBundle`` over PROFILES_DIR (system) and
// USER_PROFILES_DIR (user-imported), then emit a JSON manifest of all
// (machines, processes, filaments) at ``out_path``. Used by the FastAPI
// service at startup and from ``/profiles/reload`` to populate the in-
// memory profile caches without Python having to walk ``inherits``.
struct DumpProfilesRequest {
    std::string profiles_dir;   // /opt/orcaslicer/profiles (system root)
    std::string user_dir;       // /data (USER_PROFILES_DIR; may be empty)
    std::string out_path;       // /tmp/profiles-manifest.json
};

// Walks libslic3r's static ``print_config_def`` registry and writes a JSON
// catalogue of every process-domain option's metadata (label, type,
// min/max, enum values, tooltip, mode, gui_type) to ``out_path``. No
// PresetBundle, no profiles_dir — the registry is statically initialised
// inside libslic3r on link.
struct DumpOptionsRequest {
    std::string out_path;       // /tmp/options-manifest.json
};

SliceRequest parse_slice_request_from_stdin();
void write_slice_response_to_stdout(const SliceResponse& r);

UseSetRequest parse_use_set_request_from_stdin();
void write_use_set_response_to_stdout(const UseSetResponse& r);

DumpProfilesRequest parse_dump_profiles_request_from_stdin();
DumpOptionsRequest parse_dump_options_request_from_stdin();

// Save the real stdout fd and redirect fd 1 to stderr at process start.
//
// Vendored libslic3r calls raw ``printf`` from at least
// ``Support/TreeSupportCommon.hpp:597`` (``tree_supports_show_error``,
// commented "todo Remove! ONLY FOR PUBLIC BETA"), which writes to fd 1
// directly and bypasses our boost::log → stderr sink. Any such write
// corrupts the JSON protocol that the Python gateway reads off stdout —
// observed for re-slices of sliced 3MFs, where 7 lines of "Error: Not
// precalculated Placeable areas requested" landed before the response
// envelope, and ``json.loads`` failed at column 0.
//
// Calling ``redirect_libslic3r_stdout_pollution`` once at startup:
//   - duplicates fd 1 into a saved fd, accessible via ``real_stdout_fd``
//   - dup2's fd 2 (stderr) over fd 1, so any further ``printf``,
//     ``puts``, ``std::cout``, etc. lands on stderr instead of corrupting
//     the JSON pipe.
//
// Response writers (and the fatal-envelope emitter) then write directly
// to ``real_stdout_fd`` via ``write(2)`` rather than through ``std::cout``.
//
// Idempotent + safe to call before any libslic3r code runs. Returns the
// saved fd or -1 on dup failure (in which case ``std::cout`` is used as
// a degraded fallback).
int redirect_libslic3r_stdout_pollution();
int real_stdout_fd();

// Serialize a JSON object to the saved real stdout fd as one line. Used
// by all binary subcommands (slice, use-set, dump-profiles) — anything
// that writes a JSON envelope to the gateway must go through this so the
// libslic3r-stdout-pollution redirect still works. Falls back to
// ``std::cout`` (now pointed at stderr) if the redirect failed at
// startup, so the envelope at least lands somewhere visible rather than
// disappearing.
void write_envelope_line(const nlohmann::json& out);

}  // namespace orca_headless
