#pragma once
#include "json_io.h"

namespace orca_headless {

// Walks each ModelVolume's default extruder and `mmu_segmentation_facets`
// used-state map to collect filament slot indices referenced by every
// plate. Returns 0 on success, non-zero on failure. Writes a JSON
// envelope to stdout via write_use_set_response_to_stdout.
int run_use_set_mode(const UseSetRequest& req);

}  // namespace orca_headless
