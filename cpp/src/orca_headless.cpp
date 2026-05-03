#include <cstdio>
#include <cstring>
#include <exception>
#include <iostream>

#include <boost/log/core.hpp>
#include <boost/log/expressions.hpp>
#include <boost/log/trivial.hpp>
#include <boost/log/utility/setup/console.hpp>

#include "libslic3r/libslic3r_version.h"
#include "libslic3r/Utils.hpp"

#include "json_io.h"
#include "slice_mode.h"

// libslic3r writes diagnostic messages through boost::log. The default
// install scribbles to whichever sink boost picks (often stdout in this
// container), which corrupts our stdout JSON protocol. Install a single
// sink that writes to stderr only, and silence everything below `error`
// since our progress channel already provides per-phase observability.
static void configure_libslic3r_logging() {
    namespace bl = boost::log;
    bl::core::get()->remove_all_sinks();
    bl::add_console_log(
        std::cerr,
        bl::keywords::format = "[%TimeStamp%][%Severity%] %Message%");
    Slic3r::set_logging_level(1);  // 1 = error and above
}

static int print_version() {
    std::printf("orca-headless 0.1.0 (libslic3r %s)\n", SLIC3R_VERSION);
    return 0;
}

static int print_usage(const char* prog) {
    std::fprintf(stderr,
        "Usage: %s <command>\n"
        "Commands:\n"
        "  --version            Print version and exit\n"
        "  slice                Read JSON request on stdin, slice, write JSON to stdout\n",
        prog);
    return 2;
}

int main(int argc, char** argv) {
    configure_libslic3r_logging();
    if (argc < 2) return print_usage(argv[0]);
    if (std::strcmp(argv[1], "--version") == 0) return print_version();
    if (std::strcmp(argv[1], "slice") == 0) {
        try {
            auto req = orca_headless::parse_slice_request_from_stdin();
            return orca_headless::run_slice_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            return 1;
        }
    }
    return print_usage(argv[0]);
}
