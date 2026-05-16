#pragma once

#include "libslic3r/Preset.hpp"

#include <cstddef>
#include <string>

namespace orca_headless {

size_t load_chain_dir_into(
    Slic3r::PresetCollection& coll,
    const std::string& dir_path);

}  // namespace orca_headless
