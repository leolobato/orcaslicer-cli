#pragma once
#include "json_io.h"

namespace orca_headless {

// Walk libslic3r's static ``Slic3r::print_config_def`` and write a JSON
// catalogue of every process-domain option's metadata (label, type,
// min/max, enum_values, tooltip, mode, gui_type, default) to
// ``req.out_path``. Returns 0 on success, non-zero on failure. Writes a
// JSON envelope to stdout (``{"status":"ok"|"error", ...}``) for symmetry
// with the other modes.
//
// Drives ``GET /options/process`` on the FastAPI side via app/options.py,
// which shells out at startup and on /profiles/reload.
int run_dump_options_mode(const DumpOptionsRequest& req);

}  // namespace orca_headless
