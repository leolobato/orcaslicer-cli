#include "dump_options_mode.h"

#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>

#include <nlohmann/json.hpp>

#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Config.hpp"

namespace orca_headless {

namespace {

// Stringify ConfigOptionType to match the names used in libslic3r's
// header (coBool, coFloat, coInt, coString, coEnum, coPercent,
// coFloatOrPercent, coPoint, coPoints, coBools, coFloats, coInts,
// coStrings, coPercents, coNone). Anything we don't recognise gets
// passed through as the integer representation; that's fine for clients
// who only care about the common cases.
const char* config_option_type_name(Slic3r::ConfigOptionType t) {
    using Slic3r::ConfigOptionType;
    switch (t) {
        case Slic3r::coNone:             return "coNone";
        case Slic3r::coFloat:            return "coFloat";
        case Slic3r::coFloats:           return "coFloats";
        case Slic3r::coInt:              return "coInt";
        case Slic3r::coInts:             return "coInts";
        case Slic3r::coString:           return "coString";
        case Slic3r::coStrings:          return "coStrings";
        case Slic3r::coPercent:          return "coPercent";
        case Slic3r::coPercents:         return "coPercents";
        case Slic3r::coFloatOrPercent:   return "coFloatOrPercent";
        case Slic3r::coFloatsOrPercents: return "coFloatsOrPercents";
        case Slic3r::coPoint:            return "coPoint";
        case Slic3r::coPoints:           return "coPoints";
        case Slic3r::coPoint3:           return "coPoint3";
        case Slic3r::coBool:             return "coBool";
        case Slic3r::coBools:            return "coBools";
        case Slic3r::coEnum:             return "coEnum";
        default:                         return "coUnknown";
    }
}

const char* config_option_mode_name(Slic3r::ConfigOptionMode m) {
    switch (m) {
        case Slic3r::comSimple:   return "simple";
        case Slic3r::comAdvanced: return "advanced";
        case Slic3r::comDevelop:  return "develop";
        default:                  return "unknown";
    }
}

const char* config_option_gui_type_name(Slic3r::ConfigOptionDef::GUIType g) {
    using G = Slic3r::ConfigOptionDef::GUIType;
    switch (g) {
        case G::undefined:    return "";
        case G::i_enum_open:  return "i_enum_open";
        case G::f_enum_open:  return "f_enum_open";
        case G::color:        return "color";
        case G::select_open:  return "select_open";
        case G::slider:       return "slider";
        case G::legend:       return "legend";
        case G::one_string:   return "one_string";
        default:              return "";
    }
}

// Returns true when this option belongs to the process domain. We exclude:
//   - filament-domain options (key starts with "filament_" or ends with
//     "_filament", or category is "Filament"). These belong to the
//     filament editor, not the process editor.
//   - machine-domain options (category is "Machine limits", or key is
//     a known printer-only key). The process editor doesn't expose them.
//
// We intentionally do NOT filter on empty category: ~60 legitimate process
// options (spiral_mode, enable_prime_tower, travel_speed, skirt_loops,
// resolution, print_sequence, gcode_label_objects, timelapse_type, etc.)
// have no category set in print_config_def but are exposed by TabPrint.
// Filtering them out caused a 60-key drift vs the Tab.cpp regex harvest.
bool is_process_domain_option(const std::string& key,
                              const Slic3r::ConfigOptionDef& def) {
    auto starts_with = [](const std::string& s, const char* p) {
        const size_t n = std::strlen(p);
        return s.size() >= n && std::memcmp(s.data(), p, n) == 0;
    };
    auto ends_with = [](const std::string& s, const char* p) {
        const size_t n = std::strlen(p);
        return s.size() >= n &&
            std::memcmp(s.data() + s.size() - n, p, n) == 0;
    };
    if (starts_with(key, "filament_") || ends_with(key, "_filament"))
        return false;
    if (def.category == "Filament") return false;
    if (def.category == "Machine limits") return false;
    return true;
}

// Serialize the default_value through the option's own ``serialize()``.
// This is the same path the GUI uses to render initial values, so vector
// and percent options come out in the canonical config-string form
// (matches what Python's project_settings.config produces).
//
// Guard: some internal coEnums options (e.g. default_nozzle_volume_type)
// construct their ConfigOptionEnumsGeneric default_value via the
// initializer_list ctor, which leaves the VALUE's keys_map == nullptr even
// when def.enum_keys_map is set. Calling serialize() on those dereferences
// the null keys_map pointer → SIGSEGV. Detect by dynamic_casting to the
// concrete type and checking its keys_map directly.
std::string serialize_default(const Slic3r::ConfigOptionDef& def) {
    if (!def.default_value) return "";
    if (def.type == Slic3r::coEnums) {
        // ConfigOptionEnumsGenericTempl<false> — non-nullable
        {
            const auto* ev = dynamic_cast<const Slic3r::ConfigOptionEnumsGeneric*>(
                def.default_value.get());
            if (ev && ev->keys_map == nullptr) return "";
        }
        // ConfigOptionEnumsGenericTempl<true> — nullable variant
        {
            const auto* ev = dynamic_cast<const Slic3r::ConfigOptionEnumsGenericNullable*>(
                def.default_value.get());
            if (ev && ev->keys_map == nullptr) return "";
        }
    }
    if (def.type == Slic3r::coEnum) {
        const auto* ev = dynamic_cast<const Slic3r::ConfigOptionEnumGeneric*>(
            def.default_value.get());
        if (ev && ev->keys_map == nullptr) return "";
    }
    return def.default_value->serialize();
}

// Min/max sentinels in libslic3r are FLT_MIN / FLT_MAX (not infinity).
// Compare against the actual sentinels used in PrintConfig.cpp; if the
// option didn't set a bound, the field is the type's full range.
bool has_finite_min(double v) {
    // libslic3r uses ``-FLT_MAX`` for "no min set" on float options
    // (see ConfigOptionDef default).
    return v > -std::numeric_limits<float>::max() * 0.99;
}
bool has_finite_max(double v) {
    return v < std::numeric_limits<float>::max() * 0.99;
}

void emit_options(nlohmann::json& out) {
    const auto& defs = Slic3r::print_config_def.options;
    for (const auto& [key, def] : defs) {
        if (!is_process_domain_option(key, def)) continue;
        nlohmann::json e;
        e["key"]      = key;
        e["label"]    = def.label;
        e["category"] = def.category;
        e["tooltip"]  = def.tooltip;
        e["type"]     = config_option_type_name(def.type);
        e["sidetext"] = def.sidetext;
        e["default"]  = serialize_default(def);

        if (has_finite_min(def.min))
            e["min"] = def.min;
        else
            e["min"] = nullptr;
        if (has_finite_max(def.max))
            e["max"] = def.max;
        else
            e["max"] = nullptr;

        if (!def.enum_values.empty())
            e["enum_values"] = def.enum_values;
        else
            e["enum_values"] = nullptr;

        // enum_labels may be empty (libslic3r falls back to enum_values
        // for display in that case); preserve the distinction.
        if (!def.enum_labels.empty())
            e["enum_labels"] = def.enum_labels;
        else
            e["enum_labels"] = nullptr;

        e["mode"]     = config_option_mode_name(def.mode);
        e["gui_type"] = config_option_gui_type_name(def.gui_type);
        e["nullable"] = def.nullable;
        e["readonly"] = def.readonly;
        out.push_back(std::move(e));
    }
}

}  // namespace

int run_dump_options_mode(const DumpOptionsRequest& req) {
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

    nlohmann::json catalogue;
    catalogue["options"] = nlohmann::json::array();
    try {
        emit_options(catalogue["options"]);
    } catch (const std::exception& e) {
        return fail("emit_failed", e.what());
    }

    std::ofstream ofs(req.out_path);
    if (!ofs) return fail("io_error",
                          "cannot open out_path for write: " + req.out_path);
    ofs << catalogue.dump();

    nlohmann::json ok = {
        {"status",  "ok"},
        {"code",    "done"},
        {"message", ""},
        {"details", {
            {"out_path", req.out_path},
            {"count",    catalogue["options"].size()},
        }},
    };
    std::cout << ok.dump() << std::endl;
    return 0;
}

}  // namespace orca_headless
