#pragma once
#include <string>

namespace orca_headless {

// Emit a single progress event as line-delimited JSON on stderr.
// The Python service parses these line-by-line for SSE streaming.
void emit_progress(const std::string& phase, int percent);

}  // namespace orca_headless
