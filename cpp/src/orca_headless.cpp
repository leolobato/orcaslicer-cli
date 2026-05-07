#include <cstdio>
#include <cstring>
#include <csignal>
#include <exception>
#include <iostream>
#include <unistd.h>

#include <boost/log/core.hpp>
#include <boost/log/expressions.hpp>
#include <boost/log/trivial.hpp>
#include <boost/log/utility/setup/console.hpp>

#include "libslic3r/libslic3r_version.h"
#include "libslic3r/Utils.hpp"

#include "json_io.h"
#include "slice_mode.h"
#include "use_set_mode.h"
#include "dump_profiles_mode.h"
#include "dump_options_mode.h"

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
        "  slice                Read JSON request on stdin, slice, write JSON to stdout\n"
        "  use-set              Read JSON request on stdin, scan 3MF for used filaments\n"
        "  dump-profiles        Read JSON {profiles_dir,user_dir,out_path} on stdin, emit profile manifest\n"
        "  dump-options         Read JSON {out_path} on stdin, emit option metadata catalogue\n",
        prog);
    return 2;
}

// Emit a structured JSON error envelope on stdout so the Python gateway
// (binary_client.py) always sees parseable output instead of an empty
// stream. Without this, an uncaught exception leaves stdout empty and the
// caller surfaces ``Expecting value: line 1 column 1 (char 0)``, masking
// the real failure. ``code`` is the stable wire identifier; ``message``
// is the ``what()`` string for human diagnosis. Best-effort only — if the
// dump itself throws (it shouldn't for plain strings) we fall back to a
// hardcoded literal so stdout is still parseable JSON.
//
// Writes via the saved real-stdout fd from
// ``redirect_libslic3r_stdout_pollution``: fd 1 itself has been pointed at
// stderr at startup so libslic3r's stray ``printf`` calls can't corrupt
// the JSON protocol.
static void emit_fatal_envelope(const char* code, const char* what_str) {
    int fd = orca_headless::real_stdout_fd();
    auto write_literal = [&](const char* s, size_t n) {
        if (fd >= 0) {
            ssize_t w = ::write(fd, s, n);
            (void)w;
        } else {
            std::cout.write(s, static_cast<std::streamsize>(n));
            std::cout.flush();
        }
    };
    try {
        nlohmann::json out = {
            {"status", "error"},
            {"code", code},
            {"message", what_str ? what_str : ""},
            {"details", nlohmann::json::object()},
        };
        std::string s = out.dump();
        s.push_back('\n');
        write_literal(s.data(), s.size());
    } catch (...) {
        constexpr char fallback[] =
            R"({"status":"error","code":"binary_fatal_serialize_failed","message":"","details":{}})" "\n";
        write_literal(fallback, sizeof(fallback) - 1);
    }
}

// Async-signal-safe envelope emitter. Writes a hardcoded JSON literal
// directly via ``write(2)`` to the saved real-stdout fd — no stdio, no
// allocation, no nlohmann::json. Required for fatal-signal handlers,
// where the C++ ``std::exception`` catch in main() cannot fire (signals
// bypass the language exception machinery) and the previous behaviour
// was an empty stdout that surfaces in the gateway as ``Expecting value:
// line 1 column 1 (char 0)``.
//
// The literals are sized at compile time so we don't need ``strlen`` —
// strlen is technically signal-safe on glibc but not POSIX-guaranteed.
// Each literal is a complete top-level JSON object terminated with a
// newline so the gateway's ``json.loads`` succeeds on the first read.
//
// Writes go to the saved real-stdout fd if available (fd 1 has been
// pointed at stderr by ``redirect_libslic3r_stdout_pollution`` so direct
// writes to STDOUT_FILENO would land on stderr). Falls back to fd 1 if
// the dup failed at startup — degraded but still better than nothing.
static void write_signal_envelope(const char* json_literal, size_t len) {
    int fd = orca_headless::real_stdout_fd();
    if (fd < 0) fd = STDOUT_FILENO;
    // Best-effort: ignore short writes / EINTR. We're about to die anyway.
    ssize_t written = ::write(fd, json_literal, len);
    (void)written;
}

#define SIGNAL_ENVELOPE(code) "{\"status\":\"error\",\"code\":\"" code "\",\"message\":\"binary terminated by signal\",\"details\":{}}\n"

static void fatal_signal_handler(int signo) {
    // Pick the envelope by signal so the gateway can distinguish crash
    // classes in metrics. SIGSEGV typically = nullptr deref / use-after-
    // free; SIGABRT = libstdc++/glibc assert or std::terminate; SIGFPE =
    // div-by-zero or bad fp op; SIGBUS = misaligned access / mmap fault.
    switch (signo) {
        case SIGSEGV: {
            constexpr char msg[] = SIGNAL_ENVELOPE("binary_signal_segv");
            write_signal_envelope(msg, sizeof(msg) - 1);
            break;
        }
        case SIGABRT: {
            constexpr char msg[] = SIGNAL_ENVELOPE("binary_signal_abort");
            write_signal_envelope(msg, sizeof(msg) - 1);
            break;
        }
        case SIGFPE: {
            constexpr char msg[] = SIGNAL_ENVELOPE("binary_signal_fpe");
            write_signal_envelope(msg, sizeof(msg) - 1);
            break;
        }
        case SIGBUS: {
            constexpr char msg[] = SIGNAL_ENVELOPE("binary_signal_bus");
            write_signal_envelope(msg, sizeof(msg) - 1);
            break;
        }
        default: {
            constexpr char msg[] = SIGNAL_ENVELOPE("binary_signal_unknown");
            write_signal_envelope(msg, sizeof(msg) - 1);
            break;
        }
    }
    // Restore the default handler and re-raise so the process exits with
    // the conventional 128+signo code and any installed core-dumper runs.
    std::signal(signo, SIG_DFL);
    std::raise(signo);
}

#undef SIGNAL_ENVELOPE

static void install_fatal_signal_handlers() {
    // sigaction would be preferable for portability of SA_RESETHAND, but
    // std::signal is sufficient: we re-arm SIG_DFL and re-raise inside
    // the handler so a second crash during the handler itself just dies
    // normally.
    std::signal(SIGSEGV, fatal_signal_handler);
    std::signal(SIGABRT, fatal_signal_handler);
    std::signal(SIGFPE,  fatal_signal_handler);
    std::signal(SIGBUS,  fatal_signal_handler);
}

// std::terminate() fires when an exception escapes a noexcept boundary
// or a destructor throws while another exception is unwinding — both
// reach abort() by default, but the SIGABRT handler runs *after* whatever
// state mangling happened on the way. Hook terminate too so we can emit
// a more specific error code distinguishing "ran past noexcept" from
// "raw signal". The terminate handler is allowed to do more (it runs
// before abort), so we use the regular ``emit_fatal_envelope`` here.
[[noreturn]] static void on_terminate() {
    try {
        // Try to surface what's actually unwinding, if anything.
        if (auto exptr = std::current_exception()) {
            try { std::rethrow_exception(exptr); }
            catch (const std::exception& e) {
                emit_fatal_envelope("binary_terminate", e.what());
            }
            catch (...) {
                emit_fatal_envelope("binary_terminate", "unknown exception");
            }
        } else {
            emit_fatal_envelope("binary_terminate", "no active exception");
        }
    } catch (...) { /* swallow */ }
    std::abort();  // triggers SIGABRT → handler → conventional exit
}

int main(int argc, char** argv) {
    // MUST run before any libslic3r code: vendored libslic3r contains
    // raw ``printf`` calls (e.g. tree-support warnings) that bypass
    // boost::log and corrupt the JSON protocol on stdout. The redirect
    // saves the real stdout fd for our envelope writers and points fd 1
    // at stderr so any libslic3r ``printf`` lands on stderr instead.
    orca_headless::redirect_libslic3r_stdout_pollution();
    configure_libslic3r_logging();
    install_fatal_signal_handlers();
    std::set_terminate(on_terminate);
    if (argc < 2) return print_usage(argv[0]);
    if (std::strcmp(argv[1], "--version") == 0) return print_version();
    if (std::strcmp(argv[1], "slice") == 0) {
        try {
            auto req = orca_headless::parse_slice_request_from_stdin();
            return orca_headless::run_slice_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            emit_fatal_envelope("binary_fatal", e.what());
            return 1;
        }
    }
    if (std::strcmp(argv[1], "use-set") == 0) {
        try {
            auto req = orca_headless::parse_use_set_request_from_stdin();
            return orca_headless::run_use_set_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            emit_fatal_envelope("binary_fatal", e.what());
            return 1;
        }
    }
    if (std::strcmp(argv[1], "dump-profiles") == 0) {
        try {
            auto req = orca_headless::parse_dump_profiles_request_from_stdin();
            return orca_headless::run_dump_profiles_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            emit_fatal_envelope("binary_fatal", e.what());
            return 1;
        }
    }
    if (std::strcmp(argv[1], "dump-options") == 0) {
        try {
            auto req = orca_headless::parse_dump_options_request_from_stdin();
            return orca_headless::run_dump_options_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            return 1;
        }
    }
    return print_usage(argv[0]);
}
