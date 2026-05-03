#include <cstdio>
#include <cstring>
#include <exception>

#include "libslic3r/libslic3r_version.h"

#include "json_io.h"
#include "slice_mode.h"

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
