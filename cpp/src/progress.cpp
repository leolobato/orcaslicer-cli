#include "progress.h"

#include <iostream>
#include <nlohmann/json.hpp>

namespace orca_headless {

void emit_progress(const std::string& phase, int percent) {
    nlohmann::json e = {
        {"phase", phase},
        {"percent", percent},
    };
    std::cerr << e.dump() << "\n";
    std::cerr.flush();
}

}  // namespace orca_headless
