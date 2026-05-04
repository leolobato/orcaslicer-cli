#pragma once
#include "json_io.h"

namespace orca_headless {

// Drives the slice flow: load 3MF → load profiles → apply overrides →
// slice → export. Mirrors the GUI's `Plater::priv::reslice` call sequence,
// using libslic3r's project-loading entry points.
//
// Returns 0 on success, non-zero on failure. The response (success or
// error) is written to stdout via write_slice_response_to_stdout.
int run_slice_mode(const SliceRequest& req);

}  // namespace orca_headless
