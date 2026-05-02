#include <cstdio>
#include <cstring>

#include "libslic3r/libslic3r_version.h"

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
        std::fprintf(stderr, "slice mode: not implemented yet\n");
        return 1;
    }
    return print_usage(argv[0]);
}
