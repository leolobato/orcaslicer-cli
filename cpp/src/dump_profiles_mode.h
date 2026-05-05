#pragma once
#include "json_io.h"

namespace orca_headless {

// Stand up a libslic3r ``PresetBundle`` over PROFILES_DIR (system) and
// USER_PROFILES_DIR (user-imported), then write a JSON manifest of
// (machines, processes, filaments) to ``req.out_path``. Returns 0 on
// success, non-zero on failure. Always writes a JSON envelope to stdout
// (``{"status":"ok"|"error", ...}``) for symmetry with slice / use-set.
//
// Replaces the Python-side inheritance walk in ``app/profiles.py``;
// FastAPI lifespan calls this once at startup and again on
// ``/profiles/reload``.
int run_dump_profiles_mode(const DumpProfilesRequest& req);

}  // namespace orca_headless
