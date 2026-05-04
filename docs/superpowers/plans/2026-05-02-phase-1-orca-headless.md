# Phase 1: `orca-headless` binary fork — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal `orca-headless` C++ binary that links libslic3r and slices a 3MF using the GUI's project-loading path. Wire it into `orcaslicer-cli` behind a feature flag, alongside the existing AppImage CLI. Add token-based 3MF cache and new upload/download endpoints. Verify fidelity against a reference 3MF.

**Architecture:** Vendor OrcaSlicer source as a git submodule pinned to v2.3.2. Build libslic3r and a small `orca-headless.cpp` wrapper in a new Docker stage. Python service stays as the API surface; new endpoints add the token cache and a JSON-body slice endpoint that subprocess-invokes the binary. Feature flag (`USE_HEADLESS_BINARY`) selects between the new path and the existing AppImage path.

**Tech Stack:** C++ (libslic3r, CMake, nlohmann/json), Python 3.12 (FastAPI, asyncio.create_subprocess_exec), Docker multi-stage build, pytest.

**Repo conventions to honor:**
- Tests in `tests/` mirror `app/` module names (e.g. `tests/test_cache.py` for `app/cache.py`).
- Subprocess handling pattern lives in `app/slicer.py:1547` (`asyncio.create_subprocess_exec`) — reuse that approach.
- Config via `app/config.py` env-var pattern.
- Dockerfile is multi-stage; the existing AppImage extraction stage stays as-is for back-compat.

---

## Milestones

1. **Build system** (Tasks 1–4) — vendor OrcaSlicer, build a hello-world `orca-headless --version`.
2. **Binary slice mode** (Tasks 5–14) — implement load → overrides → slice → export.
3. **Python token cache** (Tasks 15–17) — `app/cache.py` with LRU + size cap.
4. **Cache HTTP endpoints** (Tasks 18–22) — upload, download, delete, stats.
5. **Binary subprocess wrapper + feature flag** (Tasks 23–27) — `app/binary_client.py` and new slice endpoint variant.
6. **Reference fidelity test** (Tasks 28–30) — slice a fixture 3MF through both paths, diff gcode.

Each milestone ends with a green-light state: builds, tests pass, manually verifiable.

---

## Files to be created or modified

**New files:**
- `.gitmodules` (if absent) and `vendor/OrcaSlicer/` — submodule pinned to v2.3.2
- `cpp/CMakeLists.txt` — top-level CMake; adds libslic3r subdirectory and `orca-headless` target
- `cpp/src/orca_headless.cpp` — main entry point, dispatches modes
- `cpp/src/json_io.h` / `cpp/src/json_io.cpp` — request/response JSON parsing
- `cpp/src/progress.h` / `cpp/src/progress.cpp` — stderr progress emitter
- `cpp/src/preset_overrides.h` / `cpp/src/preset_overrides.cpp` — `apply_project_overrides` (the GUI-equivalent overlay)
- `cpp/src/slice_mode.h` / `cpp/src/slice_mode.cpp` — `slice` mode implementation
- `cpp/tests/smoke.sh` — fixture-driven shell smoke test
- `cpp/tests/fixtures/single_plate.3mf` — small reference 3MF (~50KB)
- `app/cache.py` — `TokenCache` (LRU + size cap)
- `app/binary_client.py` — async subprocess wrapper for `orca-headless`
- `tests/test_cache.py` — token cache unit tests
- `tests/test_binary_client.py` — binary client unit tests (subprocess mocked)
- `tests/test_3mf_endpoints.py` — upload/download/delete endpoint tests
- `tests/test_slice_token_endpoint.py` — new JSON-body slice endpoint tests
- `tests/fixtures/binary_responses/` — canned JSON responses for mocked binary
- `tests/integration/test_slice_fidelity.py` — reference output comparison test (gated behind a marker so it doesn't run by default)

**Modified files:**
- `Dockerfile` — add C++ build stage, copy `orca-headless` into runtime
- `docker-compose.yml` — add `CACHE_DIR` env, mount cache volume
- `app/config.py` — new env vars: `CACHE_DIR`, `CACHE_MAX_BYTES`, `CACHE_MAX_FILES`, `USE_HEADLESS_BINARY`, `ORCA_HEADLESS_BINARY`
- `app/main.py` — register new endpoints
- `app/slicer.py` — branch on `USE_HEADLESS_BINARY` to invoke either AppImage CLI (existing) or `orca-headless` via `binary_client`
- `requirements.txt` — no change expected
- `pytest.ini` (if present) — add `integration` marker

**Untouched:** `app/profiles.py`, `app/normalize.py`, `app/threemf.py`, `app/slice_request.py`, `app/models.py`. These survive Phase 1 unchanged. Phase 2 deletes the now-unused ones.

---

## Milestone 1 — Build system

End state: `docker compose build` produces an image containing `/opt/orca-headless/bin/orca-headless`. Running `docker compose run orcaslicer-cli orca-headless --version` prints a version string sourced from libslic3r.

### Task 1: Vendor OrcaSlicer source as git submodule

**Files:**
- Create: `.gitmodules`
- Create: `vendor/OrcaSlicer/` (submodule directory)

- [ ] **Step 1: Add OrcaSlicer as submodule pinned to v2.3.2**

```bash
cd /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/orcaslicer-cli
git submodule add https://github.com/SoftFever/OrcaSlicer.git vendor/OrcaSlicer
cd vendor/OrcaSlicer
git checkout v2.3.2
cd ../..
git add .gitmodules vendor/OrcaSlicer
```

- [ ] **Step 2: Verify submodule is at the correct tag**

Run: `git -C vendor/OrcaSlicer describe --tags`
Expected: `v2.3.2`

- [ ] **Step 3: Commit submodule registration**

```bash
git commit -m "Vendor OrcaSlicer source at v2.3.2 as submodule"
```

### Task 2: Add CMake skeleton with hello-world binary

**Files:**
- Create: `cpp/CMakeLists.txt`
- Create: `cpp/src/orca_headless.cpp`

- [ ] **Step 1: Create top-level CMake**

Write `cpp/CMakeLists.txt`:

```cmake
cmake_minimum_required(VERSION 3.20)
project(orca_headless CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Pull libslic3r from the vendored OrcaSlicer source. We only build the
# library targets we depend on, not the GUI or upstream CLI.
set(SLIC3R_GUI OFF CACHE BOOL "" FORCE)
set(SLIC3R_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(BBL_RELEASE_TO_PUBLIC OFF CACHE BOOL "" FORCE)

add_subdirectory(${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/src/libslic3r
                 ${CMAKE_BINARY_DIR}/libslic3r-build)

add_executable(orca-headless
    src/orca_headless.cpp
)

target_include_directories(orca-headless PRIVATE
    ${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/src
)

target_link_libraries(orca-headless PRIVATE libslic3r)

install(TARGETS orca-headless RUNTIME DESTINATION bin)
```

- [ ] **Step 2: Create hello-world entry point**

Write `cpp/src/orca_headless.cpp`:

```cpp
#include <cstdio>
#include <cstring>
#include <string>

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
```

- [ ] **Step 3: Commit CMake + hello-world**

```bash
git add cpp/CMakeLists.txt cpp/src/orca_headless.cpp
git commit -m "Add CMake skeleton and hello-world orca-headless binary"
```

### Task 3: Update Dockerfile to build orca-headless

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Add C++ build stage and copy binary into runtime**

The current Dockerfile has two stages: `builder` (extracts AppImage) and runtime. Add a third stage `cpp-builder` that builds `orca-headless`. Both binaries coexist in runtime so we can A/B them via the feature flag.

Replace the file with this content (preserves existing AppImage path; adds new stage):

```dockerfile
# Stage 1: Extract pre-built OrcaSlicer from AppImage (legacy path; kept for
# back-compat during Phase 1)
FROM --platform=linux/amd64 ubuntu:24.04 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget squashfs-tools && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN wget --max-redirect=10 -q "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.3.2/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.2.AppImage" \
    -O orcaslicer.AppImage

RUN ELF_END=$( \
      SHOFF=$(od -A n -t u8 -j 40 -N 8 orcaslicer.AppImage | tr -d ' ') && \
      SHENTSIZE=$(od -A n -t u2 -j 58 -N 2 orcaslicer.AppImage | tr -d ' ') && \
      SHNUM=$(od -A n -t u2 -j 60 -N 2 orcaslicer.AppImage | tr -d ' ') && \
      echo $((SHOFF + SHENTSIZE * SHNUM)) \
    ) && \
    tail -c +$((ELF_END + 1)) orcaslicer.AppImage > squashfs.img && \
    unsquashfs -d squashfs-root squashfs.img && \
    rm orcaslicer.AppImage squashfs.img

# Stage 2: Build orca-headless from vendored OrcaSlicer source
FROM --platform=linux/amd64 ubuntu:24.04 AS cpp-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ninja-build \
    libboost-all-dev \
    libtbb-dev \
    libcurl4-openssl-dev \
    libssl-dev \
    libcgal-dev \
    libeigen3-dev \
    libnlopt-cxx-dev \
    libopenvdb-dev \
    libgmp-dev \
    libmpfr-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY vendor/OrcaSlicer vendor/OrcaSlicer
COPY cpp cpp

# OrcaSlicer's libslic3r expects some deps from its own deps/ superbuild. For
# the libs available in apt (boost, eigen, cgal, etc.) we let CMake pick them
# up from the system; for ones that aren't (or that have ABI issues), we fall
# back to the deps superbuild. Phase 1 starts with system deps only and adds
# the superbuild incrementally if libslic3r refuses to link.
RUN cmake -S cpp -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/orca-headless && \
    cmake --build build -j"$(nproc)" && \
    cmake --install build

# Stage 3: Runtime
FROM --platform=linux/amd64 ubuntu:24.04

RUN apt-get update && \
    echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    libcurl4t64 \
    libcairo2 \
    libdbus-1-3 \
    libglew2.2 \
    libglu1-mesa \
    libgtk-3-0t64 \
    libsecret-1-0 \
    libmspack0 \
    libsm6 \
    libsoup2.4-1 \
    libssl3t64 \
    libudev1 \
    libwayland-client0 \
    libwayland-egl1 \
    libwebkit2gtk-4.1-0 \
    libxkbcommon0 \
    libtbb12 \
    libcgal14 \
    libopenvdb10.0 \
    libnlopt-cxx0 \
    libgmp10 \
    libmpfr6 \
    locales \
    && rm -rf /var/lib/apt/lists/*

ENV LC_ALL=en_US.utf8
RUN locale-gen $LC_ALL
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Legacy AppImage binary (kept for back-compat in Phase 1)
COPY --from=builder /build/squashfs-root/bin/orca-slicer /opt/orcaslicer/bin/orca-slicer
COPY --from=builder /build/squashfs-root/resources/ /opt/resources/
COPY --from=builder /build/squashfs-root/resources/profiles/ /opt/orcaslicer/profiles/
RUN chmod +x /opt/orcaslicer/bin/orca-slicer

# New orca-headless binary
COPY --from=cpp-builder /opt/orca-headless/bin/orca-headless /opt/orca-headless/bin/orca-headless
RUN chmod +x /opt/orca-headless/bin/orca-headless

ENV PATH="/opt/orcaslicer/bin:/opt/orca-headless/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements-dev.txt

COPY app/ app/
COPY tests/ tests/
COPY conftest.py .

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Build the image**

Run: `docker compose build`
Expected: build succeeds. The cpp-builder stage will likely fail on first attempt because libslic3r's CMakeLists.txt requires features we haven't enabled. Iterate on the apt deps and CMake flags until the build is green. Common adjustments: add `-DSLIC3R_STATIC=0`, install extra deps, vendor specific OpenVDB version via OrcaSlicer's `deps/` superbuild.

If the build is intractable in Phase 1's first task, the alternative is to invoke OrcaSlicer's own `deps/build.sh` superbuild for missing libs and link against those. Document the resolution in this task's commit message.

- [ ] **Step 3: Verify hello-world binary runs**

Run: `docker compose run --rm orcaslicer-cli orca-headless --version`
Expected: prints `orca-headless 0.1.0 (libslic3r 2.3.2)` (or whatever `SLIC3R_VERSION` resolves to).

- [ ] **Step 4: Commit Dockerfile changes**

```bash
git add Dockerfile
git commit -m "Add cpp-builder stage to build orca-headless binary"
```

### Task 4: Smoke test for the build pipeline

**Files:**
- Create: `cpp/tests/smoke.sh`

- [ ] **Step 1: Write smoke test script**

Write `cpp/tests/smoke.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

BIN="${ORCA_HEADLESS_BINARY:-/opt/orca-headless/bin/orca-headless}"

echo "== version check =="
"$BIN" --version

echo "== unknown command exits non-zero =="
if "$BIN" totally-not-a-command 2>/dev/null; then
    echo "FAIL: unknown command should exit non-zero"
    exit 1
fi

echo "== slice without stdin exits non-zero with diagnostic =="
if "$BIN" slice </dev/null 2>/dev/null; then
    echo "FAIL: slice with empty stdin should exit non-zero"
    exit 1
fi

echo "OK"
```

- [ ] **Step 2: Make executable**

Run: `chmod +x cpp/tests/smoke.sh`

- [ ] **Step 3: Run smoke test in container**

Run: `docker compose run --rm orcaslicer-cli bash cpp/tests/smoke.sh`
Expected: prints "OK" and exits 0.

- [ ] **Step 4: Commit smoke test**

```bash
git add cpp/tests/smoke.sh
git commit -m "Add smoke test for orca-headless build pipeline"
```

---

## Milestone 2 — Binary slice mode

End state: `cat request.json | orca-headless slice > response.json 2> events.ndjson` produces a sliced 3MF at the path requested, with a JSON envelope on stdout and progress events on stderr. Verified against a small fixture 3MF.

### Task 5: Add nlohmann/json dependency

**Files:**
- Modify: `cpp/CMakeLists.txt`

OrcaSlicer's deps/ already includes nlohmann/json transitively. We expose it explicitly so our code uses it.

- [ ] **Step 1: Find the include path**

Run: `find vendor/OrcaSlicer -name "json.hpp" 2>/dev/null | head -3`
Expected: at least one path under `vendor/OrcaSlicer/deps/` or `vendor/OrcaSlicer/src/`.

- [ ] **Step 2: Add the include path to our target**

Edit `cpp/CMakeLists.txt`. After the `target_include_directories(orca-headless PRIVATE ...)` block add:

```cmake
# nlohmann/json comes vendored with OrcaSlicer's deps. Path resolved during
# Task 5 step 1.
target_include_directories(orca-headless PRIVATE
    ${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/deps/build/destdir/usr/local/include
)
```

(Adjust the literal path to match what step 1 found. If the include lives under `src/`, point there instead.)

- [ ] **Step 3: Verify nlohmann/json compiles**

Add a one-line test to `cpp/src/orca_headless.cpp`'s top: `#include <nlohmann/json.hpp>`. Rebuild.

Run: `docker compose build` (only the cpp-builder layer rebuilds)
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add cpp/CMakeLists.txt cpp/src/orca_headless.cpp
git commit -m "Wire nlohmann/json into orca-headless build"
```

### Task 6: JSON request/response I/O module

**Files:**
- Create: `cpp/src/json_io.h`, `cpp/src/json_io.cpp`
- Modify: `cpp/CMakeLists.txt`

- [ ] **Step 1: Define the request/response structs**

Write `cpp/src/json_io.h`:

```cpp
#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace orca_headless {

struct SliceRequest {
    std::string input_3mf;
    std::string output_3mf;
    std::string machine_profile;
    std::string process_profile;
    std::vector<std::string> filament_profiles;
    int plate_id = 1;
    bool recenter = true;
};

struct SliceResponseEstimate {
    double time_seconds = 0.0;
    double weight_g = 0.0;
    std::vector<double> filament_used_m;
};

struct SliceResponse {
    std::string status;            // "ok" or "error"
    std::string output_3mf;
    SliceResponseEstimate estimate;
    nlohmann::json settings_transfer = nlohmann::json::object();
    std::string error_code;        // present when status == "error"
    std::string error_message;
    nlohmann::json error_details = nlohmann::json::object();
};

SliceRequest parse_slice_request_from_stdin();
void write_slice_response_to_stdout(const SliceResponse& r);

}  // namespace orca_headless
```

Write `cpp/src/json_io.cpp`:

```cpp
#include "json_io.h"

#include <iostream>
#include <sstream>
#include <stdexcept>

using nlohmann::json;

namespace orca_headless {

SliceRequest parse_slice_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());

    SliceRequest req;
    req.input_3mf        = j.at("input_3mf").get<std::string>();
    req.output_3mf       = j.at("output_3mf").get<std::string>();
    req.machine_profile  = j.at("machine_profile").get<std::string>();
    req.process_profile  = j.at("process_profile").get<std::string>();
    req.filament_profiles = j.at("filament_profiles").get<std::vector<std::string>>();
    req.plate_id         = j.value("plate_id", 1);
    if (j.contains("options")) {
        req.recenter = j["options"].value("recenter", true);
    }
    return req;
}

void write_slice_response_to_stdout(const SliceResponse& r) {
    json out;
    out["status"] = r.status;
    if (r.status == "ok") {
        out["output_3mf"] = r.output_3mf;
        out["estimate"] = {
            {"time_seconds", r.estimate.time_seconds},
            {"weight_g", r.estimate.weight_g},
            {"filament_used_m", r.estimate.filament_used_m},
        };
        out["settings_transfer"] = r.settings_transfer;
    } else {
        out["code"] = r.error_code;
        out["message"] = r.error_message;
        out["details"] = r.error_details;
    }
    std::cout << out.dump() << std::endl;
}

}  // namespace orca_headless
```

- [ ] **Step 2: Add to CMake**

Edit `cpp/CMakeLists.txt`'s `add_executable` block:

```cmake
add_executable(orca-headless
    src/orca_headless.cpp
    src/json_io.cpp
)
```

- [ ] **Step 3: Build and verify it compiles**

Run: `docker compose build`
Expected: success.

- [ ] **Step 4: Commit**

```bash
git add cpp/src/json_io.h cpp/src/json_io.cpp cpp/CMakeLists.txt
git commit -m "Add JSON request/response module for orca-headless"
```

### Task 7: Progress event emitter

**Files:**
- Create: `cpp/src/progress.h`, `cpp/src/progress.cpp`
- Modify: `cpp/CMakeLists.txt`

- [ ] **Step 1: Write progress emitter**

Write `cpp/src/progress.h`:

```cpp
#pragma once
#include <string>

namespace orca_headless {

void emit_progress(const std::string& phase, int percent);

}  // namespace orca_headless
```

Write `cpp/src/progress.cpp`:

```cpp
#include "progress.h"
#include <nlohmann/json.hpp>
#include <iostream>

namespace orca_headless {

void emit_progress(const std::string& phase, int percent) {
    nlohmann::json e = {
        {"phase", phase},
        {"percent", percent},
    };
    // line-delimited JSON on stderr
    std::cerr << e.dump() << "\n";
    std::cerr.flush();
}

}  // namespace orca_headless
```

- [ ] **Step 2: Add to CMake**

Append `src/progress.cpp` to the `add_executable` source list.

- [ ] **Step 3: Verify compile and trivial smoke**

Add a temporary call `emit_progress("starting", 0);` to `main()`. Build + run `orca-headless slice </dev/null` (will still error because slice is unimplemented), and observe the progress line on stderr.

After verifying, remove the temporary call.

- [ ] **Step 4: Commit**

```bash
git add cpp/src/progress.h cpp/src/progress.cpp cpp/CMakeLists.txt
git commit -m "Add stderr progress event emitter"
```

### Task 8: Skeleton slice mode that loads the 3MF

**Files:**
- Create: `cpp/src/slice_mode.h`, `cpp/src/slice_mode.cpp`
- Modify: `cpp/src/orca_headless.cpp`, `cpp/CMakeLists.txt`

This task only loads the 3MF and reports basic info. No slicing yet. Verifies that libslic3r linking actually works at runtime, not just compile-time.

- [ ] **Step 1: Write slice mode skeleton**

Write `cpp/src/slice_mode.h`:

```cpp
#pragma once
#include "json_io.h"

namespace orca_headless {
int run_slice_mode(const SliceRequest& req);
}
```

Write `cpp/src/slice_mode.cpp`:

```cpp
#include "slice_mode.h"
#include "progress.h"

#include "libslic3r/Model.hpp"
#include "libslic3r/PrintConfig.hpp"

#include <stdexcept>

namespace orca_headless {

int run_slice_mode(const SliceRequest& req) {
    emit_progress("loading_3mf", 0);

    Slic3r::DynamicPrintConfig config;
    Slic3r::ConfigSubstitutionContext substitution_ctx(Slic3r::ForwardCompatibilitySubstitutionRule::Enable);
    Slic3r::PlateDataPtrs plate_data;
    std::vector<Slic3r::Preset*> project_presets;

    Slic3r::Model model;
    try {
        // NOTE: read_from_file's signature evolved across OrcaSlicer versions.
        // Match the v2.3.2 signature from vendor/OrcaSlicer/src/libslic3r/Model.hpp:1606.
        // If this call fails to compile, look up the exact signature there and adjust.
        model = Slic3r::Model::read_from_file(
            req.input_3mf,
            &config,
            &substitution_ctx,
            Slic3r::LoadStrategy::LoadModel | Slic3r::LoadStrategy::LoadConfig | Slic3r::LoadStrategy::LoadAuxiliary,
            &plate_data,
            &project_presets);
    } catch (const std::exception& e) {
        SliceResponse r;
        r.status = "error";
        r.error_code = "invalid_3mf";
        r.error_message = e.what();
        write_slice_response_to_stdout(r);
        return 1;
    }

    emit_progress("loaded_3mf", 10);

    // Stub: report plate count and exit. Real slicing in later tasks.
    SliceResponse r;
    r.status = "error";
    r.error_code = "not_implemented";
    r.error_message = "slice mode loaded "
                      + std::to_string(plate_data.size()) + " plate(s); "
                      + "slicing not yet implemented";
    write_slice_response_to_stdout(r);
    return 1;
}

}  // namespace orca_headless
```

- [ ] **Step 2: Wire slice command in main**

Update `cpp/src/orca_headless.cpp`:

```cpp
#include <cstdio>
#include <cstring>

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
```

- [ ] **Step 3: Add slice_mode.cpp to CMake**

Append `src/slice_mode.cpp` to `add_executable`.

- [ ] **Step 4: Build, then run with a small fixture**

Place a single-plate 3MF at `cpp/tests/fixtures/single_plate.3mf` (any plain unsliced 3MF will do; OrcaSlicer's own test fixtures or any small `.3mf` from `tests/fixtures/`).

Run inside the container:

```bash
echo '{"input_3mf":"/app/cpp/tests/fixtures/single_plate.3mf","output_3mf":"/tmp/out.3mf","machine_profile":"x","process_profile":"x","filament_profiles":["x"]}' \
  | orca-headless slice
```

Expected: stdout contains `{"status":"error","code":"not_implemented","message":"slice mode loaded N plate(s); slicing not yet implemented",...}` where N is the number of plates in the fixture. Stderr shows `{"phase":"loading_3mf","percent":0}` and `{"phase":"loaded_3mf","percent":10}`.

If the call instead fails with `code: invalid_3mf`, libslic3r is rejecting the file — try a different fixture (e.g. one produced by OrcaSlicer's GUI directly).

- [ ] **Step 5: Commit**

```bash
git add cpp/src/slice_mode.h cpp/src/slice_mode.cpp cpp/src/orca_headless.cpp cpp/CMakeLists.txt cpp/tests/fixtures/single_plate.3mf
git commit -m "Wire slice mode skeleton with 3MF loading via libslic3r"
```

### Task 9: Load profiles from JSON files into a PresetBundle

**Files:**
- Modify: `cpp/src/slice_mode.cpp`

The Python service writes resolved profile JSON files to a temp dir and passes their paths in the request. The binary loads them into a `PresetBundle` so libslic3r treats them as the selected presets.

- [ ] **Step 1: Add helper that loads a single preset JSON**

Insert into `cpp/src/slice_mode.cpp` (above `run_slice_mode`):

```cpp
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/Config.hpp"

namespace {

Slic3r::DynamicPrintConfig load_preset_json(const std::string& path) {
    // Mirrors what PresetBundle::load_external_config does for a single file.
    // Reads the JSON, materializes a DynamicPrintConfig with the keys.
    Slic3r::DynamicPrintConfig cfg;
    Slic3r::ConfigSubstitutionContext ctx(Slic3r::ForwardCompatibilitySubstitutionRule::Enable);
    cfg.load_from_json(path, ctx, true /*load_inherits=*/, /*ignore_nonexistent=*/false);
    return cfg;
}

}  // namespace
```

If `load_from_json` doesn't exist with that exact signature in v2.3.2, look at `vendor/OrcaSlicer/src/libslic3r/Preset.cpp` for `Preset::load_from_file` or `Preset::load`. The call should produce a `DynamicPrintConfig` whose keys match what's in the JSON.

- [ ] **Step 2: Replace the stub in `run_slice_mode` with profile loading**

Inside `run_slice_mode`, after the `model = Model::read_from_file(...)` call, add:

```cpp
emit_progress("loading_profiles", 15);

Slic3r::DynamicPrintConfig machine_cfg  = load_preset_json(req.machine_profile);
Slic3r::DynamicPrintConfig process_cfg  = load_preset_json(req.process_profile);
std::vector<Slic3r::DynamicPrintConfig> filament_cfgs;
for (const auto& fp : req.filament_profiles) {
    filament_cfgs.push_back(load_preset_json(fp));
}

// Compose the final config: machine ← process ← filament(s)
Slic3r::DynamicPrintConfig final_cfg;
final_cfg.apply(machine_cfg);
final_cfg.apply(process_cfg);
// Multi-filament: libslic3r expects per-filament keys to be vector-valued.
// For Phase 1 we only handle the single-filament case; multi-filament is a
// later task that uses the same per-filament normalization PresetBundle does.
if (!filament_cfgs.empty()) {
    final_cfg.apply(filament_cfgs[0]);
}
```

Replace the "not_implemented" return with a new stub that just confirms the profiles loaded:

```cpp
SliceResponse r;
r.status = "error";
r.error_code = "not_implemented";
r.error_message = "loaded " + std::to_string(plate_data.size()) + " plate(s) and "
                  + std::to_string(filament_cfgs.size()) + " filament profile(s); "
                  + "slicing pending";
write_slice_response_to_stdout(r);
return 1;
```

- [ ] **Step 3: Build and run with valid profile JSONs**

Inside the container, copy a few resolved-profile JSONs from `/opt/orcaslicer/profiles/BBL/` into `/tmp/profiles/` (a machine, a process, a filament). Use the existing AppImage's profiles for the test.

```bash
echo '{
  "input_3mf":"/app/cpp/tests/fixtures/single_plate.3mf",
  "output_3mf":"/tmp/out.3mf",
  "machine_profile":"/tmp/profiles/machine.json",
  "process_profile":"/tmp/profiles/process.json",
  "filament_profiles":["/tmp/profiles/filament.json"]
}' | orca-headless slice
```

Expected: stdout has `code:"not_implemented"` with message confirming the profile count. No JSON parse errors on the profile loads.

- [ ] **Step 4: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Load machine/process/filament profiles into DynamicPrintConfig"
```

### Task 10: Apply project overrides — the GUI-equivalent overlay

**Files:**
- Create: `cpp/src/preset_overrides.h`, `cpp/src/preset_overrides.cpp`
- Modify: `cpp/src/slice_mode.cpp`, `cpp/CMakeLists.txt`

This is the core fidelity bet. The 3MF carries `different_settings_to_system` (a list-of-comma-separated-keys), one entry per slot in the order `[process, filament_0, ..., filament_{N-1}, printer]`. We overlay each declared key from the project's bundled config onto the corresponding selected preset.

Reference: `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp` around `load_3mf_*` and `Preset::normalize` (Preset.cpp:370).

- [ ] **Step 1: Define the overrides API**

Write `cpp/src/preset_overrides.h`:

```cpp
#pragma once

#include "libslic3r/PrintConfig.hpp"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace orca_headless {

struct ProjectOverrideReport {
    nlohmann::json process_transferred  = nlohmann::json::array();
    nlohmann::json filament_transferred = nlohmann::json::array();
    nlohmann::json machine_transferred  = nlohmann::json::array();
    std::string status = "no_3mf_settings";   // applied | no_customizations | no_3mf_settings
};

// Overlays project-bundled customizations from `project_config` onto the
// per-slot configs (process/filaments/machine), guided by the project's
// `different_settings_to_system` fingerprint.
//
// `selected_filament_names[i]` should be the *currently selected* filament
// preset's name for slot i. If it differs from the project's
// `filament_settings_id[i]`, the per-filament overlay for that slot is
// discarded and reported as discarded (the user swapped filaments).
ProjectOverrideReport apply_project_overrides(
    const Slic3r::DynamicPrintConfig& project_config,
    Slic3r::DynamicPrintConfig& process_cfg,
    std::vector<Slic3r::DynamicPrintConfig>& filament_cfgs,
    const std::vector<std::string>& selected_filament_names,
    Slic3r::DynamicPrintConfig& machine_cfg);

}  // namespace orca_headless
```

- [ ] **Step 2: Implement the overlay logic**

Write `cpp/src/preset_overrides.cpp`:

```cpp
#include "preset_overrides.h"

#include "libslic3r/Config.hpp"
#include <algorithm>
#include <set>
#include <sstream>

namespace orca_headless {

namespace {

std::vector<std::string> split_csv(const std::string& s) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ';')) {
        // Some 3MFs separate by ';' (Slic3r format); some by comma. Accept both.
        std::stringstream ss2(item);
        std::string sub;
        while (std::getline(ss2, sub, ',')) {
            // trim whitespace
            auto a = sub.find_first_not_of(" \t");
            auto b = sub.find_last_not_of(" \t");
            if (a == std::string::npos) continue;
            out.push_back(sub.substr(a, b - a + 1));
        }
    }
    return out;
}

bool is_filament_key(const std::string& k) {
    return k.rfind("filament_", 0) == 0 || k.find("_filament") != std::string::npos;
}

void overlay_keys(const Slic3r::DynamicPrintConfig& src,
                  Slic3r::DynamicPrintConfig& dst,
                  const std::vector<std::string>& keys,
                  nlohmann::json& report) {
    for (const auto& k : keys) {
        if (!src.has(k)) continue;
        auto* opt = src.option(k);
        if (!opt) continue;
        // Capture the original value from dst for the report
        nlohmann::json entry = {{"key", k}};
        if (dst.has(k)) {
            entry["original"] = dst.opt_serialize(k);
        }
        // Overlay
        dst.set_key_value(k, opt->clone());
        entry["value"] = dst.opt_serialize(k);
        report.push_back(entry);
    }
}

}  // namespace

ProjectOverrideReport apply_project_overrides(
    const Slic3r::DynamicPrintConfig& project_config,
    Slic3r::DynamicPrintConfig& process_cfg,
    std::vector<Slic3r::DynamicPrintConfig>& filament_cfgs,
    const std::vector<std::string>& selected_filament_names,
    Slic3r::DynamicPrintConfig& machine_cfg) {

    ProjectOverrideReport report;

    if (!project_config.has("different_settings_to_system")) {
        report.status = "no_3mf_settings";
        return report;
    }

    // Field is a vector<string>: one slot per [process, filament_0, ..., printer]
    auto* opt = project_config.option<Slic3r::ConfigOptionStrings>("different_settings_to_system");
    if (!opt || opt->values.empty()) {
        report.status = "no_customizations";
        return report;
    }

    const auto& slots = opt->values;
    // slot[0] = process
    if (slots.size() >= 1 && !slots[0].empty()) {
        auto keys = split_csv(slots[0]);
        // Filament-like keys are excluded from process even if listed there
        keys.erase(std::remove_if(keys.begin(), keys.end(), is_filament_key), keys.end());
        overlay_keys(project_config, process_cfg, keys, report.process_transferred);
    }

    // slots 1..N = per-filament
    std::vector<std::string> proj_filament_names;
    if (project_config.has("filament_settings_id")) {
        if (auto* fopt = project_config.option<Slic3r::ConfigOptionStrings>("filament_settings_id")) {
            proj_filament_names = fopt->values;
        }
    }

    const size_t n_filaments = filament_cfgs.size();
    for (size_t i = 0; i < n_filaments; ++i) {
        nlohmann::json slot_report = {
            {"slot", static_cast<int>(i)},
            {"original_filament", i < proj_filament_names.size() ? proj_filament_names[i] : ""},
            {"selected_filament", i < selected_filament_names.size() ? selected_filament_names[i] : ""},
            {"status", "no_customizations"},
            {"transferred", nlohmann::json::array()},
            {"discarded", nlohmann::json::array()},
        };
        size_t slot_idx = 1 + i;
        if (slot_idx < slots.size() && !slots[slot_idx].empty()) {
            auto keys = split_csv(slots[slot_idx]);
            bool name_match = (i < proj_filament_names.size() && i < selected_filament_names.size())
                              && (proj_filament_names[i] == selected_filament_names[i]);
            if (name_match) {
                nlohmann::json transferred = nlohmann::json::array();
                overlay_keys(project_config, filament_cfgs[i], keys, transferred);
                slot_report["transferred"] = transferred;
                slot_report["status"] = transferred.empty() ? "no_customizations" : "applied";
            } else {
                slot_report["status"] = "filament_changed";
                for (const auto& k : keys) slot_report["discarded"].push_back(k);
            }
        }
        report.filament_transferred.push_back(slot_report);
    }

    // Last slot = printer/machine
    if (slots.size() == n_filaments + 2 && !slots.back().empty()) {
        auto keys = split_csv(slots.back());
        overlay_keys(project_config, machine_cfg, keys, report.machine_transferred);
    }

    bool any_applied = !report.process_transferred.empty() || !report.machine_transferred.empty();
    for (const auto& f : report.filament_transferred) {
        if (f["status"] == "applied") any_applied = true;
    }
    report.status = any_applied ? "applied" : "no_customizations";
    return report;
}

}  // namespace orca_headless
```

- [ ] **Step 3: Add to CMake**

Append `src/preset_overrides.cpp` to `add_executable`.

- [ ] **Step 4: Wire into slice_mode**

In `cpp/src/slice_mode.cpp`, after the profile-loading block, before the not-implemented return, add:

```cpp
#include "preset_overrides.h"

// ... inside run_slice_mode, after filament_cfgs are populated:
emit_progress("applying_overrides", 20);

// Read the selected-filament names from the request (Phase 1: derive from
// each filament profile's "name" key. Multi-filament requests will pass
// these explicitly in a later task.)
std::vector<std::string> selected_filament_names;
for (const auto& fc : filament_cfgs) {
    selected_filament_names.push_back(fc.has("name") ? fc.opt_string("name") : "");
}

auto report = orca_headless::apply_project_overrides(
    config /* project config from 3MF */,
    process_cfg,
    filament_cfgs,
    selected_filament_names,
    machine_cfg);
```

Stash `report` to be emitted in the response later (Task 14).

- [ ] **Step 5: Build, run against a known-customized 3MF**

Use a 3MF with `different_settings_to_system` populated (any 3MF saved from the OrcaSlicer GUI with non-default settings has this). Run slice mode and inspect stderr — add a temporary `std::cerr << report.status << "\n";` to confirm overrides are being applied. Remove the temp log after verification.

- [ ] **Step 6: Commit**

```bash
git add cpp/src/preset_overrides.h cpp/src/preset_overrides.cpp cpp/src/slice_mode.cpp cpp/CMakeLists.txt
git commit -m "Apply 3MF project overrides onto selected presets"
```

### Task 11: Set filament_map and AMS-related keys

**Files:**
- Modify: `cpp/src/json_io.h`, `cpp/src/json_io.cpp`, `cpp/src/slice_mode.cpp`

The Python service passes per-slot AMS slot assignments. The binary writes them into the final config so libslic3r threads them through to the output gcode and slice_info.

- [ ] **Step 1: Extend SliceRequest with optional filament_map**

In `cpp/src/json_io.h`, add to `SliceRequest`:

```cpp
std::vector<int> filament_map;          // per-slot AMS slot index, optional
std::vector<std::string> filament_settings_id;  // optional override of selected names
```

In `cpp/src/json_io.cpp` `parse_slice_request_from_stdin`, after the existing fields:

```cpp
if (j.contains("filament_map") && j["filament_map"].is_array())
    req.filament_map = j["filament_map"].get<std::vector<int>>();
if (j.contains("filament_settings_id") && j["filament_settings_id"].is_array())
    req.filament_settings_id = j["filament_settings_id"].get<std::vector<std::string>>();
```

- [ ] **Step 2: Apply filament_map in slice_mode**

In `cpp/src/slice_mode.cpp`, after `apply_project_overrides`, but before slicing:

```cpp
// Build the final config: machine ← process ← filaments ← per-filament overlays.
Slic3r::DynamicPrintConfig final_cfg;
final_cfg.apply(machine_cfg);
final_cfg.apply(process_cfg);
// per-filament keys must be vector-valued in libslic3r; emulate
// PresetBundle::full_config which folds per-filament configs together.
// (This is the simplified single-filament case; multi-filament unification
// happens via libslic3r's own PrintConfig::merge_full_config — call that if
// available, else open-code the join.)
for (auto& fc : filament_cfgs) final_cfg.apply(fc);

if (!req.filament_map.empty()) {
    auto* opt = final_cfg.opt<Slic3r::ConfigOptionInts>("filament_map", true);
    opt->values = req.filament_map;
}
if (!req.filament_settings_id.empty()) {
    auto* opt = final_cfg.opt<Slic3r::ConfigOptionStrings>("filament_settings_id", true);
    opt->values = req.filament_settings_id;
}
```

- [ ] **Step 3: Build, smoke-test**

Send a request with `"filament_map":[0,2]` and confirm the binary doesn't crash. Stub return still says not_implemented.

- [ ] **Step 4: Commit**

```bash
git add cpp/src/json_io.h cpp/src/json_io.cpp cpp/src/slice_mode.cpp
git commit -m "Wire filament_map and filament_settings_id into final config"
```

### Task 12: Recenter model on plate

**Files:**
- Modify: `cpp/src/slice_mode.cpp`

- [ ] **Step 1: Add recentering**

In `cpp/src/slice_mode.cpp`, after the final config is built:

```cpp
emit_progress("recentering", 25);
if (req.recenter) {
    // Plate center derived from machine_cfg's printable_area (rectangle).
    Slic3r::Vec2d center(0, 0);
    if (final_cfg.has("printable_area")) {
        auto* opt = final_cfg.option<Slic3r::ConfigOptionPoints>("printable_area");
        if (opt && !opt->values.empty()) {
            // printable_area is a polygon; centroid suffices for rect plates.
            double sx = 0, sy = 0;
            for (const auto& p : opt->values) { sx += p.x(); sy += p.y(); }
            center = Slic3r::Vec2d(sx / opt->values.size(), sy / opt->values.size());
        }
    }
    model.center_instances_around_point(center);
}
```

- [ ] **Step 2: Build, smoke-test**

Send a request, observe stderr for the `recentering` progress event.

- [ ] **Step 3: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Recenter model around plate center derived from printable_area"
```

### Task 13: Slice and export

**Files:**
- Modify: `cpp/src/slice_mode.cpp`

- [ ] **Step 1: Run Print::process and export to 3MF**

Add includes at the top of `slice_mode.cpp`:

```cpp
#include "libslic3r/Print.hpp"
#include "libslic3r/Format/3mf.hpp"
```

Replace the not-implemented stub at the bottom of `run_slice_mode` with:

```cpp
emit_progress("slicing", 30);

Slic3r::Print print;
print.apply(model, final_cfg);

// libslic3r's progress callback: forward to our stderr emitter.
print.set_status_callback([](const Slic3r::Print::Status& s) {
    int pct = 30 + static_cast<int>(s.percent * 0.65); // 30..95
    emit_progress(s.text, pct);
});

try {
    print.process();
} catch (const std::exception& e) {
    SliceResponse r;
    r.status = "error";
    r.error_code = "slice_failed";
    r.error_message = e.what();
    write_slice_response_to_stdout(r);
    return 1;
}

emit_progress("exporting", 95);

Slic3r::StoreParams sp;
sp.path = req.output_3mf.c_str();
sp.model = &model;
sp.plate_data_list = plate_data;
sp.project_presets = project_presets;
sp.config = &final_cfg;
sp.export_plate_idx = req.plate_id - 1;
sp.strategy = Slic3r::SaveStrategy::WithGcode;

bool ok = Slic3r::store_bbs_3mf(sp);
if (!ok) {
    SliceResponse r;
    r.status = "error";
    r.error_code = "export_failed";
    r.error_message = "store_bbs_3mf returned false";
    write_slice_response_to_stdout(r);
    return 1;
}

emit_progress("done", 100);

SliceResponse r;
r.status = "ok";
r.output_3mf = req.output_3mf;
// Estimate fields populated in Task 14.
r.settings_transfer = {
    {"status", report.status},
    {"process",  report.process_transferred},
    {"filament", report.filament_transferred},
    {"machine",  report.machine_transferred},
};
write_slice_response_to_stdout(r);
return 0;
```

The exact fields of `StoreParams` and `Print::Status` may differ in v2.3.2 — cross-check `vendor/OrcaSlicer/src/libslic3r/Format/3mf.hpp:60` and `Print.hpp` for the actual signatures and adjust.

- [ ] **Step 2: Build, slice a fixture end-to-end**

```bash
echo '{
  "input_3mf":"/app/cpp/tests/fixtures/single_plate.3mf",
  "output_3mf":"/tmp/out.3mf",
  "machine_profile":"/tmp/profiles/machine.json",
  "process_profile":"/tmp/profiles/process.json",
  "filament_profiles":["/tmp/profiles/filament.json"],
  "plate_id":1
}' | orca-headless slice
```

Expected: exit 0, stdout has `{"status":"ok","output_3mf":"/tmp/out.3mf",...}`. `/tmp/out.3mf` exists, and `unzip -l /tmp/out.3mf` shows `Metadata/plate_1.gcode`.

- [ ] **Step 3: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Wire Print::process and store_bbs_3mf to produce sliced output"
```

### Task 14: Extract estimate from sliced output for the response

**Files:**
- Modify: `cpp/src/slice_mode.cpp`

The simplest path: after `store_bbs_3mf` returns, re-open the output 3MF and read `Metadata/slice_info.config`. Or pull the values directly from `print` post-process.

- [ ] **Step 1: Pull from print object**

After `print.process()` succeeds and before the response is written, gather:

```cpp
double total_time = print.full_print_config().opt_float("estimated_print_time", 0.0);
// total_weight and per-filament use require introspecting print's stats:
// libslic3r exposes these via Print::print_statistics() in v2.3.2 — confirm signature.
const auto& stats = print.print_statistics();
double weight_g = stats.total_weight;
std::vector<double> filament_used_m;
for (const auto& f : stats.filament_stats) {
    filament_used_m.push_back(f.length / 1000.0);
}
r.estimate = {total_time, weight_g, filament_used_m};
```

If `print_statistics()` doesn't expose what we need in v2.3.2, fall back to re-parsing the output 3MF's `Metadata/slice_info.config` (tiny XML parse). Document which path was taken in the commit message.

- [ ] **Step 2: Build, run, confirm response includes estimate**

Run end-to-end and assert the JSON envelope contains non-zero `time_seconds`, `weight_g`, and a populated `filament_used_m`.

- [ ] **Step 3: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Include time/weight/filament-usage estimate in slice response"
```

---

## Milestone 3 — Python token cache

End state: `app/cache.py` exposes a `TokenCache` class with content-addressed storage, LRU eviction, and configurable size/file caps. Unit tests pass.

### Task 15: Token cache module — basic put/get

**Files:**
- Create: `app/cache.py`
- Create: `tests/test_cache.py`

- [ ] **Step 1: Write failing test for put/get**

Write `tests/test_cache.py`:

```python
import hashlib
from pathlib import Path

import pytest

from app.cache import TokenCache


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d


def test_put_returns_token_and_sha(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    payload = b"hello world"
    expected_sha = hashlib.sha256(payload).hexdigest()
    token, sha, size = cache.put(payload)
    assert isinstance(token, str) and len(token) > 0
    assert sha == expected_sha
    assert size == len(payload)


def test_get_returns_path_to_stored_bytes(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    payload = b"some 3mf bytes"
    token, _, _ = cache.put(payload)
    path = cache.path(token)
    assert path.exists()
    assert path.read_bytes() == payload


def test_unknown_token_raises(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    with pytest.raises(KeyError):
        cache.path("nonexistent")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cache.py -v`
Expected: ImportError or AttributeError — `app.cache` doesn't exist yet.

- [ ] **Step 3: Implement minimum to pass**

Write `app/cache.py`:

```python
"""Content-addressed token cache for 3MF files.

Files are stored on disk by SHA-256. Tokens are opaque IDs that map to a SHA.
Eviction is LRU, gated on configurable byte and file-count caps.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _Entry:
    token: str
    sha: str
    size: int
    last_access: float


class TokenCache:
    def __init__(self, cache_dir: Path, max_bytes: int, max_files: int) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._sha_to_token: dict[str, str] = {}

    def put(self, payload: bytes) -> tuple[str, str, int]:
        sha = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        with self._lock:
            if sha in self._sha_to_token:
                token = self._sha_to_token[sha]
                self._entries.move_to_end(token)
                return token, sha, size
            token = secrets.token_urlsafe(16)
            path = self._path_for_sha(sha)
            if not path.exists():
                path.write_bytes(payload)
            self._entries[token] = _Entry(token, sha, size, time.time())
            self._sha_to_token[sha] = token
            return token, sha, size

    def path(self, token: str) -> Path:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                raise KeyError(token)
            self._entries.move_to_end(token)
            entry.last_access = time.time()
            return self._path_for_sha(entry.sha)

    def _path_for_sha(self, sha: str) -> Path:
        return self.cache_dir / f"{sha}.3mf"
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_cache.py -v`
Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/cache.py tests/test_cache.py
git commit -m "Add TokenCache with content-addressed put/get"
```

### Task 16: LRU eviction by file count

**Files:**
- Modify: `app/cache.py`, `tests/test_cache.py`

- [ ] **Step 1: Write failing test for max_files eviction**

Append to `tests/test_cache.py`:

```python
def test_max_files_eviction(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=2)
    t1, _, _ = cache.put(b"a")
    t2, _, _ = cache.put(b"b")
    t3, _, _ = cache.put(b"c")
    # t1 should be evicted (oldest)
    with pytest.raises(KeyError):
        cache.path(t1)
    assert cache.path(t2).read_bytes() == b"b"
    assert cache.path(t3).read_bytes() == b"c"


def test_get_marks_most_recently_used(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=2)
    t1, _, _ = cache.put(b"a")
    t2, _, _ = cache.put(b"b")
    cache.path(t1)  # touch t1, making t2 the LRU
    t3, _, _ = cache.put(b"c")
    with pytest.raises(KeyError):
        cache.path(t2)
    assert cache.path(t1).read_bytes() == b"a"
    assert cache.path(t3).read_bytes() == b"c"
```

- [ ] **Step 2: Run, observe failure**

Run: `pytest tests/test_cache.py -v`
Expected: the two new tests fail (cache stores all 3, no eviction).

- [ ] **Step 3: Implement eviction in `put`**

In `app/cache.py`, add an `_evict_if_needed` method and call it at end of `put`:

```python
    def _evict_if_needed(self) -> list[str]:
        evicted: list[str] = []
        # File count cap
        while len(self._entries) > self.max_files:
            tok, entry = self._entries.popitem(last=False)
            self._sha_to_token.pop(entry.sha, None)
            self._path_for_sha(entry.sha).unlink(missing_ok=True)
            evicted.append(tok)
        # Byte cap
        total = sum(e.size for e in self._entries.values())
        while total > self.max_bytes and self._entries:
            tok, entry = self._entries.popitem(last=False)
            self._sha_to_token.pop(entry.sha, None)
            self._path_for_sha(entry.sha).unlink(missing_ok=True)
            total -= entry.size
            evicted.append(tok)
        return evicted
```

Update `put` to return evicted tokens too, and call `_evict_if_needed` after inserting:

```python
    def put(self, payload: bytes) -> tuple[str, str, int, list[str]]:
        sha = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        with self._lock:
            if sha in self._sha_to_token:
                token = self._sha_to_token[sha]
                self._entries.move_to_end(token)
                return token, sha, size, []
            token = secrets.token_urlsafe(16)
            path = self._path_for_sha(sha)
            if not path.exists():
                path.write_bytes(payload)
            self._entries[token] = _Entry(token, sha, size, time.time())
            self._sha_to_token[sha] = token
            evicted = self._evict_if_needed()
            return token, sha, size, evicted
```

Update existing test calls to unpack 4 values: `t1, _, _, _ = cache.put(b"a")`.

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_cache.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/cache.py tests/test_cache.py
git commit -m "Evict LRU entries when cache exceeds file or byte caps"
```

### Task 17: Explicit delete and stats

**Files:**
- Modify: `app/cache.py`, `tests/test_cache.py`

- [ ] **Step 1: Write failing tests for `delete`, `clear`, `stats`**

Append to `tests/test_cache.py`:

```python
def test_delete_token(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    t, _, _, _ = cache.put(b"abc")
    assert cache.delete(t) is True
    with pytest.raises(KeyError):
        cache.path(t)


def test_delete_unknown_returns_false(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    assert cache.delete("nope") is False


def test_clear_removes_all(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=1_000_000, max_files=10)
    cache.put(b"a")
    cache.put(b"b")
    evicted, freed = cache.clear()
    assert evicted == 2
    assert freed == 2
    assert cache.stats()["count"] == 0


def test_stats_shape(cache_dir: Path) -> None:
    cache = TokenCache(cache_dir=cache_dir, max_bytes=100, max_files=5)
    cache.put(b"hello")
    s = cache.stats()
    assert s == {"count": 1, "total_bytes": 5, "max_bytes": 100, "max_files": 5}
```

- [ ] **Step 2: Run, see failures**

Run: `pytest tests/test_cache.py -v`
Expected: 4 new failures.

- [ ] **Step 3: Implement `delete`, `clear`, `stats`**

Add to `app/cache.py`:

```python
    def delete(self, token: str) -> bool:
        with self._lock:
            entry = self._entries.pop(token, None)
            if entry is None:
                return False
            self._sha_to_token.pop(entry.sha, None)
            self._path_for_sha(entry.sha).unlink(missing_ok=True)
            return True

    def clear(self) -> tuple[int, int]:
        with self._lock:
            count = len(self._entries)
            freed = sum(e.size for e in self._entries.values())
            for entry in self._entries.values():
                self._path_for_sha(entry.sha).unlink(missing_ok=True)
            self._entries.clear()
            self._sha_to_token.clear()
            return count, freed

    def stats(self) -> dict:
        with self._lock:
            return {
                "count": len(self._entries),
                "total_bytes": sum(e.size for e in self._entries.values()),
                "max_bytes": self.max_bytes,
                "max_files": self.max_files,
            }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cache.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/cache.py tests/test_cache.py
git commit -m "Add delete/clear/stats to TokenCache"
```

---

## Milestone 4 — Cache HTTP endpoints

End state: `POST /3mf` accepts multipart, returns `{token, sha256, size}`. `GET /3mf/{token}` returns bytes. `DELETE /3mf/{token}` and `DELETE /3mf/cache` work. `GET /3mf/cache/stats` returns the cache's stats.

### Task 18: Wire TokenCache into FastAPI lifespan

**Files:**
- Modify: `app/config.py`, `app/main.py`

- [ ] **Step 1: Add cache config knobs**

Append to `app/config.py`:

```python
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data/cache"))
CACHE_MAX_BYTES = int(os.environ.get("CACHE_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))  # 10 GB
CACHE_MAX_FILES = int(os.environ.get("CACHE_MAX_FILES", "200"))
```

Add `from pathlib import Path` at the top if not already imported.

- [ ] **Step 2: Construct cache in lifespan**

In `app/main.py`, locate the `lifespan` function (line 110). Add at the top of the `with`/`yield` block:

```python
from .cache import TokenCache
from . import config as cfg

app.state.token_cache = TokenCache(
    cache_dir=cfg.CACHE_DIR,
    max_bytes=cfg.CACHE_MAX_BYTES,
    max_files=cfg.CACHE_MAX_FILES,
)
```

- [ ] **Step 3: Verify imports and basic startup**

Run: `pytest tests/ -k "test_health" -v` (if such a test exists; otherwise just `pytest -x` to make sure existing tests still pass).
Expected: existing tests still pass; no import errors.

- [ ] **Step 4: Commit**

```bash
git add app/config.py app/main.py
git commit -m "Construct TokenCache in FastAPI lifespan"
```

### Task 19: POST /3mf upload endpoint

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_3mf_endpoints.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_3mf_endpoints.py`:

```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CACHE_MAX_BYTES", "1000000")
    monkeypatch.setenv("CACHE_MAX_FILES", "10")
    # Reload config so env vars take effect
    from importlib import reload
    from app import config
    reload(config)
    from app import main
    reload(main)
    return TestClient(main.app)


def test_upload_returns_token(client: TestClient) -> None:
    payload = b"PK\x03\x04 fake 3mf bytes"
    resp = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert "sha256" in body
    assert body["size"] == len(payload)


def test_upload_same_file_returns_same_token(client: TestClient) -> None:
    payload = b"deterministic content"
    r1 = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    r2 = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    assert r1.json()["token"] == r2.json()["token"]
```

- [ ] **Step 2: Run, expect 404 (endpoint not registered)**

Run: `pytest tests/test_3mf_endpoints.py -v`
Expected: assertions fail with 404 status codes.

- [ ] **Step 3: Add endpoint to `app/main.py`**

Locate a good spot (after the `/profiles` endpoints, before `/slice`). Add:

```python
from fastapi import UploadFile, File, Request

@app.post("/3mf")
async def upload_3mf(request: Request, file: UploadFile = File(...)):
    payload = await file.read()
    cache: TokenCache = request.app.state.token_cache
    token, sha, size, evicted = cache.put(payload)
    return {"token": token, "sha256": sha, "size": size, "evicts": evicted}
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_3mf_endpoints.py -v`
Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_3mf_endpoints.py
git commit -m "Add POST /3mf upload endpoint backed by TokenCache"
```

### Task 20: GET /3mf/{token} download endpoint

**Files:**
- Modify: `app/main.py`, `tests/test_3mf_endpoints.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_3mf_endpoints.py`:

```python
def test_download_returns_bytes(client: TestClient) -> None:
    payload = b"some content"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]
    resp = client.get(f"/3mf/{token}")
    assert resp.status_code == 200
    assert resp.content == payload


def test_download_unknown_404(client: TestClient) -> None:
    resp = client.get("/3mf/nonexistent-token")
    assert resp.status_code == 404
    assert resp.json()["code"] == "token_unknown"
```

- [ ] **Step 2: Run, expect failures**

Run: `pytest tests/test_3mf_endpoints.py -v`
Expected: new tests fail.

- [ ] **Step 3: Add endpoint**

In `app/main.py`:

```python
from fastapi.responses import FileResponse, JSONResponse

@app.get("/3mf/{token}")
async def download_3mf(request: Request, token: str):
    cache: TokenCache = request.app.state.token_cache
    try:
        path = cache.path(token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": token},
        )
    return FileResponse(
        path,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_3mf_endpoints.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_3mf_endpoints.py
git commit -m "Add GET /3mf/{token} download endpoint"
```

### Task 21: DELETE endpoints

**Files:**
- Modify: `app/main.py`, `tests/test_3mf_endpoints.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_3mf_endpoints.py`:

```python
def test_delete_token(client: TestClient) -> None:
    up = client.post("/3mf", files={"file": ("a.3mf", b"x", "application/octet-stream")})
    token = up.json()["token"]
    resp = client.delete(f"/3mf/{token}")
    assert resp.status_code == 204
    assert client.get(f"/3mf/{token}").status_code == 404


def test_delete_unknown_404(client: TestClient) -> None:
    assert client.delete("/3mf/nonexistent").status_code == 404


def test_clear_cache(client: TestClient) -> None:
    client.post("/3mf", files={"file": ("a.3mf", b"a", "application/octet-stream")})
    client.post("/3mf", files={"file": ("b.3mf", b"b", "application/octet-stream")})
    resp = client.delete("/3mf/cache")
    assert resp.status_code == 200
    assert resp.json()["evicted"] == 2
    assert resp.json()["freed_bytes"] == 2


def test_cache_stats(client: TestClient) -> None:
    client.post("/3mf", files={"file": ("a.3mf", b"hello", "application/octet-stream")})
    resp = client.get("/3mf/cache/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["total_bytes"] == 5
    assert "max_bytes" in body
    assert "max_files" in body
```

- [ ] **Step 2: Run, see failures**

Run: `pytest tests/test_3mf_endpoints.py -v`
Expected: 4 new failures.

- [ ] **Step 3: Add endpoints**

```python
from fastapi import status as fastapi_status

# Order matters: /3mf/cache/* must be registered before /3mf/{token}/* if using
# path params, or use explicit routing to avoid conflicts. Putting cache stats
# under /3mf-cache avoids the conflict; pick whichever style fits.

@app.delete("/3mf/cache")
async def clear_cache(request: Request):
    cache: TokenCache = request.app.state.token_cache
    count, freed = cache.clear()
    return {"evicted": count, "freed_bytes": freed}


@app.get("/3mf/cache/stats")
async def cache_stats(request: Request):
    cache: TokenCache = request.app.state.token_cache
    return cache.stats()


@app.delete("/3mf/{token}", status_code=fastapi_status.HTTP_204_NO_CONTENT)
async def delete_token(request: Request, token: str):
    cache: TokenCache = request.app.state.token_cache
    if not cache.delete(token):
        return JSONResponse(status_code=404, content={"code": "token_unknown", "token": token})
    return None
```

Note: FastAPI/Starlette routes the most-specific path first when explicitly declared in this order. If a conflict arises (token literal `cache`), reserve `cache` and reject as a token name.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_3mf_endpoints.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_3mf_endpoints.py
git commit -m "Add DELETE /3mf/{token}, DELETE /3mf/cache, GET /3mf/cache/stats"
```

### Task 22: Mount cache volume in docker-compose

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add cache volume and env**

Update `docker-compose.yml` to include a cache volume bind and the new env vars:

```yaml
    volumes:
      - ./data:/data
      - ./data/cache:/data/cache
      - ./app:/app/app
      - ./tests:/app/tests
      - ./conftest.py:/app/conftest.py
    environment:
      - ORCA_BINARY=/opt/orcaslicer/bin/orca-slicer
      - ORCA_HEADLESS_BINARY=/opt/orca-headless/bin/orca-headless
      - PROFILES_DIR=/opt/orcaslicer/profiles
      - USER_PROFILES_DIR=/data
      - CACHE_DIR=/data/cache
      - LOG_LEVEL=DEBUG
```

- [ ] **Step 2: Verify the directory exists at startup**

Run: `docker compose up -d && docker compose exec orcaslicer-cli ls -la /data/cache`
Expected: directory exists. (`TokenCache.__init__` calls `mkdir(parents=True, exist_ok=True)`.)

- [ ] **Step 3: Hit the new endpoints in a live container**

```bash
curl -X POST http://localhost:8000/3mf -F "file=@cpp/tests/fixtures/single_plate.3mf"
# → {"token":"...", "sha256":"...", "size":N, "evicts":[]}

curl -s http://localhost:8000/3mf/cache/stats
# → {"count":1,...}
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "Bind cache volume and expose ORCA_HEADLESS_BINARY env"
```

---

## Milestone 5 — Binary subprocess wrapper + feature flag

End state: Python service can invoke `orca-headless slice` via subprocess, parse its JSON response, and stream stderr progress events. A feature flag (`USE_HEADLESS_BINARY`) selects this path; the legacy AppImage CLI path is still default.

### Task 23: BinaryClient module — invoke and parse response

**Files:**
- Create: `app/binary_client.py`
- Create: `tests/test_binary_client.py`

- [ ] **Step 1: Write failing test (subprocess mocked)**

Write `tests/test_binary_client.py`:

```python
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.binary_client import BinaryClient, BinaryError


@pytest.fixture
def client() -> BinaryClient:
    return BinaryClient(binary_path="/opt/orca-headless/bin/orca-headless")


@pytest.mark.asyncio
async def test_slice_returns_response(client: BinaryClient, tmp_path: Path) -> None:
    fake_response = {
        "status": "ok",
        "output_3mf": "/tmp/out.3mf",
        "estimate": {"time_seconds": 100, "weight_g": 5.0, "filament_used_m": [1.0]},
        "settings_transfer": {},
    }
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(json.dumps(fake_response).encode(), b""))
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        result = await client.slice(request={
            "input_3mf": "/tmp/in.3mf",
            "output_3mf": "/tmp/out.3mf",
            "machine_profile": "/tmp/m.json",
            "process_profile": "/tmp/p.json",
            "filament_profiles": ["/tmp/f.json"],
        })
    assert result["status"] == "ok"
    assert result["estimate"]["time_seconds"] == 100


@pytest.mark.asyncio
async def test_slice_raises_on_error_status(client: BinaryClient) -> None:
    err = {"status": "error", "code": "invalid_3mf", "message": "bad zip"}
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(json.dumps(err).encode(), b""))
    mock_proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        with pytest.raises(BinaryError) as excinfo:
            await client.slice(request={"input_3mf": "/x"})
    assert excinfo.value.code == "invalid_3mf"


@pytest.mark.asyncio
async def test_slice_raises_on_crash_no_json(client: BinaryClient) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"segfault\n"))
    mock_proc.returncode = -11
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        with pytest.raises(BinaryError) as excinfo:
            await client.slice(request={"input_3mf": "/x"})
    assert excinfo.value.code == "binary_crashed"
```

If `pytest-asyncio` isn't installed, add it to `requirements-dev.txt` and document the bump in this commit.

- [ ] **Step 2: Run, see import failures**

Run: `pytest tests/test_binary_client.py -v`
Expected: ImportError on `app.binary_client`.

- [ ] **Step 3: Implement BinaryClient**

Write `app/binary_client.py`:

```python
"""Async wrapper around the orca-headless binary."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class BinaryError(Exception):
    code: str
    message: str
    details: dict[str, Any]
    stderr_tail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class BinaryClient:
    def __init__(self, binary_path: str, slice_timeout_sec: int = 300) -> None:
        self.binary_path = binary_path
        self.slice_timeout_sec = slice_timeout_sec

    async def slice(self, request: dict[str, Any]) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            self.binary_path, "slice",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=json.dumps(request).encode()),
                timeout=self.slice_timeout_sec,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BinaryError(code="binary_timeout", message="slice exceeded timeout", details={})

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        if proc.returncode != 0 and not stdout.strip():
            raise BinaryError(
                code="binary_crashed",
                message=f"orca-headless exited {proc.returncode} with no stdout",
                details={},
                stderr_tail=stderr_text[-2000:],
            )

        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise BinaryError(
                code="binary_bad_response",
                message=f"could not parse stdout as JSON: {e}",
                details={"stdout_head": stdout[:500].decode("utf-8", errors="replace")},
                stderr_tail=stderr_text[-2000:],
            )

        if response.get("status") != "ok":
            raise BinaryError(
                code=response.get("code", "unknown"),
                message=response.get("message", ""),
                details=response.get("details", {}),
                stderr_tail=stderr_text[-2000:],
            )

        return response
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_binary_client.py -v`
Expected: all 3 pass.

- [ ] **Step 5: Commit**

```bash
git add app/binary_client.py tests/test_binary_client.py
git commit -m "Add BinaryClient async wrapper for orca-headless"
```

### Task 24: Streaming variant with progress events

**Files:**
- Modify: `app/binary_client.py`, `tests/test_binary_client.py`

- [ ] **Step 1: Write failing test for streaming progress**

Append to `tests/test_binary_client.py`:

```python
@pytest.mark.asyncio
async def test_slice_stream_yields_progress_then_result(client: BinaryClient) -> None:
    progress_lines = [
        b'{"phase":"loading_3mf","percent":0}\n',
        b'{"phase":"slicing","percent":50}\n',
        b'{"phase":"done","percent":100}\n',
    ]
    final_response = {
        "status": "ok",
        "output_3mf": "/tmp/out.3mf",
        "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
        "settings_transfer": {},
    }

    class FakeStream:
        def __init__(self, lines: list[bytes]):
            self._lines = list(lines)
        async def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""

    mock_proc = AsyncMock()
    mock_proc.stderr = FakeStream(progress_lines)
    mock_proc.stdout = AsyncMock()
    mock_proc.stdout.read = AsyncMock(return_value=json.dumps(final_response).encode())
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = 0
    mock_proc.stdin = AsyncMock()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        events = []
        async for ev in client.slice_stream(request={"input_3mf": "/x"}):
            events.append(ev)

    types = [e["type"] for e in events]
    assert types == ["progress", "progress", "progress", "result"]
    assert events[-1]["payload"]["status"] == "ok"
```

- [ ] **Step 2: Run, see failure (no `slice_stream` yet)**

Run: `pytest tests/test_binary_client.py::test_slice_stream_yields_progress_then_result -v`
Expected: AttributeError.

- [ ] **Step 3: Implement `slice_stream`**

Append to `app/binary_client.py`:

```python
    async def slice_stream(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        proc = await asyncio.create_subprocess_exec(
            self.binary_path, "slice",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(json.dumps(request).encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def pump_stderr() -> AsyncIterator[dict[str, Any]]:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    yield {"type": "progress", "payload": e}
                except json.JSONDecodeError:
                    logger.debug("non-JSON stderr line from binary: %r", line)

        async for ev in pump_stderr():
            yield ev

        stdout = await proc.stdout.read()
        rc = await proc.wait()

        if rc != 0 and not stdout.strip():
            yield {"type": "error", "payload": {"code": "binary_crashed", "message": f"exit {rc}"}}
            return
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as e:
            yield {"type": "error", "payload": {"code": "binary_bad_response", "message": str(e)}}
            return
        if response.get("status") != "ok":
            yield {"type": "error", "payload": response}
            return
        yield {"type": "result", "payload": response}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_binary_client.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/binary_client.py tests/test_binary_client.py
git commit -m "Add slice_stream async generator yielding progress/result/error events"
```

### Task 25: Feature flag in config

**Files:**
- Modify: `app/config.py`

- [ ] **Step 1: Add flag**

Append to `app/config.py`:

```python
USE_HEADLESS_BINARY = os.environ.get("USE_HEADLESS_BINARY", "0").lower() in ("1", "true", "yes")
ORCA_HEADLESS_BINARY = os.environ.get("ORCA_HEADLESS_BINARY", "/opt/orca-headless/bin/orca-headless")
```

- [ ] **Step 2: Verify with a quick sanity test**

Run: `python3 -c "import os; os.environ['USE_HEADLESS_BINARY']='1'; from app import config; print(config.USE_HEADLESS_BINARY)"`
Expected: `True`.

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "Add USE_HEADLESS_BINARY feature flag and ORCA_HEADLESS_BINARY path"
```

### Task 26: New JSON-body /slice endpoint that uses BinaryClient

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_slice_token_endpoint.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_slice_token_endpoint.py`:

```python
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("USE_HEADLESS_BINARY", "1")
    from importlib import reload
    from app import config
    reload(config)
    from app import main
    reload(main)
    return TestClient(main.app)


def test_slice_with_token_uses_binary(client: TestClient) -> None:
    payload = b"PK\x03\x04 fake 3mf"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    fake_result = {
        "status": "ok",
        "output_3mf": "/tmp/out.3mf",
        "estimate": {"time_seconds": 60, "weight_g": 1.0, "filament_used_m": [0.5]},
        "settings_transfer": {"status": "applied"},
    }

    async def fake_slice(self, request):
        # Verify the Python service handed the binary the cached file path
        assert request["input_3mf"].endswith(".3mf")
        return fake_result

    with patch("app.binary_client.BinaryClient.slice", new=fake_slice):
        # Stub out the file-write step that registers the output 3MF in the cache
        with patch("pathlib.Path.read_bytes", return_value=b"sliced bytes"):
            resp = client.post("/slice", json={
                "input_token": token,
                "machine_id": "GM014",
                "process_id": "GP001",
                "filament_settings_ids": ["GFSA00"],
                "plate_id": 1,
            })

    assert resp.status_code == 200
    body = resp.json()
    assert body["estimate"]["time_seconds"] == 60
    assert "output_token" in body
```

- [ ] **Step 2: Run, see failure**

Run: `pytest tests/test_slice_token_endpoint.py -v`
Expected: 404 or 422 (endpoint not registered for JSON body).

- [ ] **Step 3: Add JSON-body slice endpoint**

In `app/main.py`, add a new endpoint that handles JSON bodies (FastAPI routes by content-type when both multipart and JSON variants exist; alternatively use a different URL like `/slice/v2` and bridge from gateway side).

Cleanest path: add a new path `/slice/v2` for JSON-body variant during Phase 1; promote to `/slice` once legacy callers migrate (Phase 4).

```python
from pydantic import BaseModel
from .binary_client import BinaryClient, BinaryError
from . import config as cfg


class SliceTokenRequest(BaseModel):
    input_token: str
    machine_id: str
    process_id: str
    filament_settings_ids: list[str]
    filament_map: list[int] | None = None
    plate_id: int = 1
    recenter: bool = True


@app.post("/slice/v2")
async def slice_v2(request: Request, body: SliceTokenRequest):
    cache: TokenCache = request.app.state.token_cache
    try:
        input_path = cache.path(body.input_token)
    except KeyError:
        return JSONResponse(404, content={"code": "token_unknown", "token": body.input_token})

    if not cfg.USE_HEADLESS_BINARY:
        return JSONResponse(503, content={
            "code": "headless_disabled",
            "message": "USE_HEADLESS_BINARY is off; use legacy POST /slice",
        })

    # Resolve profiles → write JSON files for the binary to read.
    # This wraps app.profiles.materialize_profiles_for_slice (existing helper);
    # if no exact helper exists, write a thin wrapper that:
    #   - resolves the inheritance chain for machine_id, process_id, filament ids
    #   - writes each as flattened JSON to a tmp dir
    #   - returns the paths
    from .slicer import materialize_profiles_for_binary  # implemented below
    paths = await materialize_profiles_for_binary(
        machine_id=body.machine_id,
        process_id=body.process_id,
        filament_setting_ids=body.filament_settings_ids,
    )

    output_path = cache.cache_dir / f"sliced-{body.input_token[:8]}.3mf"

    binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
    try:
        result = await binary.slice(request={
            "input_3mf": str(input_path),
            "output_3mf": str(output_path),
            "machine_profile": paths["machine"],
            "process_profile": paths["process"],
            "filament_profiles": paths["filaments"],
            "plate_id": body.plate_id,
            "options": {"recenter": body.recenter},
            "filament_map": body.filament_map or [],
            "filament_settings_id": body.filament_settings_ids,
        })
    except BinaryError as e:
        return JSONResponse(500, content={"code": e.code, "message": e.message, "details": e.details})

    # Register the sliced output in the cache so gateway can fetch by token.
    out_token, out_sha, out_size, _ = cache.put(output_path.read_bytes())

    return {
        "input_token": body.input_token,
        "output_token": out_token,
        "output_sha256": out_sha,
        "estimate": result["estimate"],
        "settings_transfer": result["settings_transfer"],
        "thumbnail_urls": [],   # populated in a later task once thumbnail extraction lands in Python
        "download_url": f"/3mf/{out_token}",
    }
```

In `app/slicer.py`, add `materialize_profiles_for_binary`:

```python
async def materialize_profiles_for_binary(
    machine_id: str,
    process_id: str,
    filament_setting_ids: list[str],
) -> dict[str, Any]:
    """Resolve profile inheritance and write flattened JSONs the binary can load.

    Reuses the existing inheritance-resolution code in app/profiles.py. Writes
    each preset to a tempfile and returns paths.
    """
    import tempfile, json
    from . import profiles as profile_index

    tmp_dir = Path(tempfile.mkdtemp(prefix="orca-headless-profiles-"))

    machine_cfg = profile_index.resolve_chain_for_payload("machine", machine_id)
    process_cfg = profile_index.resolve_chain_for_payload("process", process_id)
    filament_paths: list[str] = []

    machine_path = tmp_dir / "machine.json"
    machine_path.write_text(json.dumps(machine_cfg))

    process_path = tmp_dir / "process.json"
    process_path.write_text(json.dumps(process_cfg))

    for i, fid in enumerate(filament_setting_ids):
        fcfg = profile_index.resolve_chain_for_payload("filament", fid)
        fpath = tmp_dir / f"filament-{i}.json"
        fpath.write_text(json.dumps(fcfg))
        filament_paths.append(str(fpath))

    return {"machine": str(machine_path), "process": str(process_path), "filaments": filament_paths}
```

If `resolve_chain_for_payload` doesn't exist with that name, find the actual helper used by `app/main.py`'s `slice_file` (line 872) and call that.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_slice_token_endpoint.py -v`
Expected: pass (with the patched BinaryClient).

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/slicer.py tests/test_slice_token_endpoint.py
git commit -m "Add POST /slice/v2 JSON-body endpoint backed by orca-headless"
```

### Task 27: SSE variant of /slice/v2

**Files:**
- Modify: `app/main.py`, `tests/test_slice_token_endpoint.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_slice_token_endpoint.py`:

```python
def test_slice_stream_v2_emits_progress_and_result(client: TestClient) -> None:
    payload = b"PK\x03\x04"
    up = client.post("/3mf", files={"file": ("a.3mf", payload, "application/octet-stream")})
    token = up.json()["token"]

    async def fake_stream(self, request):
        yield {"type": "progress", "payload": {"phase": "loading_3mf", "percent": 0}}
        yield {"type": "progress", "payload": {"phase": "done", "percent": 100}}
        yield {"type": "result", "payload": {
            "status": "ok",
            "output_3mf": "/tmp/out.3mf",
            "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
            "settings_transfer": {},
        }}

    with patch("app.binary_client.BinaryClient.slice_stream", new=fake_stream):
        with patch("pathlib.Path.read_bytes", return_value=b"sliced"):
            resp = client.post("/slice-stream/v2", json={
                "input_token": token,
                "machine_id": "GM014",
                "process_id": "GP001",
                "filament_settings_ids": ["GFSA00"],
                "plate_id": 1,
            })
    assert resp.status_code == 200
    text = resp.text
    assert "progress" in text
    assert "result" in text
```

- [ ] **Step 2: Run, see failure**

Expected: 404.

- [ ] **Step 3: Implement SSE endpoint**

In `app/main.py`:

```python
from fastapi.responses import StreamingResponse

@app.post("/slice-stream/v2")
async def slice_stream_v2(request: Request, body: SliceTokenRequest):
    cache: TokenCache = request.app.state.token_cache
    try:
        input_path = cache.path(body.input_token)
    except KeyError:
        return JSONResponse(404, content={"code": "token_unknown", "token": body.input_token})

    paths = await materialize_profiles_for_binary(
        machine_id=body.machine_id,
        process_id=body.process_id,
        filament_setting_ids=body.filament_settings_ids,
    )
    output_path = cache.cache_dir / f"sliced-{body.input_token[:8]}.3mf"
    binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)

    async def event_gen():
        async for ev in binary.slice_stream(request={
            "input_3mf": str(input_path),
            "output_3mf": str(output_path),
            "machine_profile": paths["machine"],
            "process_profile": paths["process"],
            "filament_profiles": paths["filaments"],
            "plate_id": body.plate_id,
            "options": {"recenter": body.recenter},
            "filament_map": body.filament_map or [],
            "filament_settings_id": body.filament_settings_ids,
        }):
            if ev["type"] == "result":
                # Register output in cache, augment payload with token
                out_token, out_sha, out_size, _ = cache.put(output_path.read_bytes())
                ev["payload"] = {
                    "input_token": body.input_token,
                    "output_token": out_token,
                    "output_sha256": out_sha,
                    "estimate": ev["payload"]["estimate"],
                    "settings_transfer": ev["payload"]["settings_transfer"],
                    "download_url": f"/3mf/{out_token}",
                }
            yield f"event: {ev['type']}\ndata: {json.dumps(ev['payload'])}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

Add `import json` at top if missing.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_slice_token_endpoint.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_slice_token_endpoint.py
git commit -m "Add SSE /slice-stream/v2 wrapping orca-headless slice_stream"
```

---

## Milestone 6 — Reference fidelity test

End state: `pytest -m integration` slices a known fixture 3MF through both the legacy AppImage path and the new orca-headless path, and asserts gcode equivalence (modulo timestamps and version-stamped comments).

### Task 28: Add integration marker and fidelity fixture

**Files:**
- Modify: `pytest.ini` (or `pyproject.toml`)
- Create: `tests/integration/test_slice_fidelity.py`
- Create: `tests/integration/__init__.py`
- Reuse: `cpp/tests/fixtures/single_plate.3mf` and the AppImage profiles already in the container

- [ ] **Step 1: Register the marker**

Edit `pytest.ini`. If the file doesn't exist, create:

```ini
[pytest]
markers =
    integration: requires Docker container with both AppImage CLI and orca-headless binary
```

- [ ] **Step 2: Write the fidelity test**

Write `tests/integration/__init__.py` (empty).

Write `tests/integration/test_slice_fidelity.py`:

```python
"""Slice the same 3MF through legacy AppImage CLI and orca-headless; diff gcode."""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path("cpp/tests/fixtures/single_plate.3mf")
LEGACY_BINARY = "/opt/orcaslicer/bin/orca-slicer"
NEW_BINARY = "/opt/orca-headless/bin/orca-headless"

# Lines that legitimately differ between runs and should be ignored when
# comparing gcode for fidelity.
_NOISE_RE = re.compile(
    r"^(; generated by|; estimated printing time|; total filament|; HEADER_BLOCK_(START|END)|; CONFIG_BLOCK_(START|END))",
    re.IGNORECASE,
)


def _strip_noise(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not _NOISE_RE.match(ln.strip())]


@pytest.mark.integration
def test_legacy_and_headless_produce_equivalent_gcode(tmp_path: Path) -> None:
    if not FIXTURE.exists():
        pytest.skip(f"fixture not found: {FIXTURE}")
    if not Path(LEGACY_BINARY).exists() or not Path(NEW_BINARY).exists():
        pytest.skip("requires container with both binaries available")

    # Slice via legacy: shell out to existing slicer.py path. For this Phase 1
    # fidelity check we invoke the binaries directly with the same profile JSONs.
    machine = "/tmp/profiles/machine.json"
    process = "/tmp/profiles/process.json"
    filament = "/tmp/profiles/filament.json"
    for p in (machine, process, filament):
        assert Path(p).exists(), f"profile JSON missing: {p}"

    legacy_out = tmp_path / "legacy.3mf"
    new_out = tmp_path / "new.3mf"

    legacy_cmd = [
        LEGACY_BINARY,
        "--load-settings", f"{machine};{process}",
        "--load-filaments", filament,
        "--slice", "1",
        "--export-3mf", str(legacy_out),
        str(FIXTURE),
    ]
    subprocess.run(legacy_cmd, check=True, capture_output=True)

    import json
    new_request = json.dumps({
        "input_3mf": str(FIXTURE),
        "output_3mf": str(new_out),
        "machine_profile": machine,
        "process_profile": process,
        "filament_profiles": [filament],
        "plate_id": 1,
    })
    proc = subprocess.run([NEW_BINARY, "slice"], input=new_request.encode(),
                          capture_output=True, check=True)
    response = json.loads(proc.stdout)
    assert response["status"] == "ok", response

    # Extract gcode from each output 3MF (Metadata/plate_1.gcode)
    import zipfile
    def read_gcode(zip_path: Path) -> str:
        with zipfile.ZipFile(zip_path) as zf:
            return zf.read("Metadata/plate_1.gcode").decode("utf-8", errors="replace")

    legacy_gcode = _strip_noise(read_gcode(legacy_out))
    new_gcode = _strip_noise(read_gcode(new_out))

    # Tolerance: identical line count and >= 99% of lines match exactly.
    assert len(legacy_gcode) == len(new_gcode), (
        f"line count differs: legacy={len(legacy_gcode)} new={len(new_gcode)}"
    )
    matching = sum(1 for a, b in zip(legacy_gcode, new_gcode) if a == b)
    ratio = matching / max(len(legacy_gcode), 1)
    assert ratio >= 0.99, f"gcode similarity {ratio:.4f} below 0.99 threshold"
```

- [ ] **Step 3: Run on host (expect skip)**

Run: `pytest tests/integration -v -m integration`
Expected: SKIPPED (binaries don't exist on host).

- [ ] **Step 4: Run inside container**

```bash
docker compose run --rm orcaslicer-cli pytest tests/integration -v -m integration
```

Expected: pass. If gcode similarity is below 0.99, that's a fidelity regression — investigate which preset overrides aren't being applied or which libslic3r call diverges. Do NOT lower the threshold to make it pass; fix the binary.

- [ ] **Step 5: Commit**

```bash
git add pytest.ini tests/integration/__init__.py tests/integration/test_slice_fidelity.py
git commit -m "Add integration test comparing legacy and orca-headless gcode output"
```

### Task 29: CI / docs hookup

**Files:**
- Modify: `README.md` (add a section)

- [ ] **Step 1: Document running the new binary and fidelity tests**

Append a section to `README.md`:

```markdown
## Phase 1: orca-headless binary

A C++ binary that links libslic3r and slices via the GUI's project-loading path, replacing the AppImage CLI for our use case.

**Build:** `docker compose build` (vendors OrcaSlicer source, builds libslic3r + binary).

**Enable for slicing:** set `USE_HEADLESS_BINARY=1` in `docker-compose.yml`'s environment.

**Endpoints (Phase 1):**
- `POST /3mf` — multipart upload, returns `{token, sha256, size}`
- `GET /3mf/{token}` — download cached bytes
- `DELETE /3mf/{token}` — evict one token
- `DELETE /3mf/cache` — clear the cache
- `GET /3mf/cache/stats` — cache size info
- `POST /slice/v2` — JSON-body slice (token-referencing)
- `POST /slice-stream/v2` — SSE variant

The legacy multipart `POST /slice` endpoint is unchanged.

**Fidelity test:** `docker compose run --rm orcaslicer-cli pytest tests/integration -v -m integration`. Diffs gcode between legacy AppImage and orca-headless paths; threshold ≥ 99% line match.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Document Phase 1 orca-headless endpoints and fidelity test"
```

### Task 30: End-to-end smoke against a live container

**Files:** none new

Manual verification step. No code changes — just confirms the whole stack works before declaring Phase 1 done.

- [ ] **Step 1: Bring up the container with the flag on**

```bash
USE_HEADLESS_BINARY=1 docker compose up -d --build
```

- [ ] **Step 2: Upload a 3MF, slice via /slice/v2, download result**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/3mf -F "file=@cpp/tests/fixtures/single_plate.3mf" | jq -r .token)
echo "input token: $TOKEN"

OUT=$(curl -s -X POST http://localhost:8000/slice/v2 \
  -H "Content-Type: application/json" \
  -d "{
    \"input_token\": \"$TOKEN\",
    \"machine_id\": \"GM014\",
    \"process_id\": \"GP001\",
    \"filament_settings_ids\": [\"GFSA00\"],
    \"plate_id\": 1
  }")
echo "$OUT" | jq

OUT_TOKEN=$(echo "$OUT" | jq -r .output_token)
curl -s -o /tmp/sliced.3mf "http://localhost:8000/3mf/$OUT_TOKEN"
unzip -l /tmp/sliced.3mf | grep "plate_1.gcode"
```

Expected: `plate_1.gcode` is in the output 3MF; the `slice/v2` response contains a non-zero estimate.

- [ ] **Step 3: Stream variant**

```bash
curl -N -X POST http://localhost:8000/slice-stream/v2 \
  -H "Content-Type: application/json" \
  -d "{...same body...}"
```

Expected: SSE events for `progress` then a final `result` with the output token.

- [ ] **Step 4: Cleanup**

```bash
curl -X DELETE http://localhost:8000/3mf/cache
docker compose down
```

- [ ] **Step 5: Tag the milestone**

```bash
git tag phase-1-complete
```

No further commit — the tag marks the end of Phase 1.

---

## Self-Review

**Spec coverage:**
- Phase 1 in the spec ("Bring up orca-headless slice mode in parallel with existing AppImage CLI. Both supported via a feature flag. Token cache + new endpoints land in Python service. Reference fidelity tests pass.") — ✅ covered by Tasks 1–30.
- Spec section "orca-headless binary > Mode `slice`" — ✅ Tasks 5–14.
- Spec section "Token cache" — ✅ Tasks 15–17.
- Spec section "Python service > New endpoints" (the upload/download/cache subset) — ✅ Tasks 18–22.
- Spec section "Internal structure" pseudocode — ✅ implemented across Tasks 8–14.
- Spec section "Build and deployment" — ✅ Tasks 1, 3, 22.
- Spec section "Testing strategy" (smoke harness, fidelity comparison) — ✅ Tasks 4, 28.
- Phase 1 explicitly excludes: `use-set` mode, `/3mf/inspect`, gateway `OrcaslicerClient` migration, deletion of `app/normalize.py` etc. These are Phase 2/3 (separate plans).

**Placeholder scan:**
- "If `load_from_json` doesn't exist with that exact signature in v2.3.2, look at..." (Task 9 step 1) — this is acceptable plan-time guidance because the exact API surface of libslic3r v2.3.2 isn't fully knowable without compiling against it. Each such note tells the engineer where to look.
- "If gcode similarity is below 0.99, that's a fidelity regression — investigate" (Task 28) — actionable, not a placeholder.
- No "TODO" or "TBD" in the document.

**Type consistency:**
- `TokenCache.put` returns 4-tuple `(token, sha, size, evicted)` after Task 16 — Task 19's endpoint unpacks 4 values. Tasks 26 and 27 also unpack 4 values. ✅
- `BinaryClient.slice` returns dict; `BinaryClient.slice_stream` is async generator yielding `{type, payload}` events. ✅ Used consistently in Tasks 26 and 27.
- `BinaryError.code` / `.message` / `.details` / `.stderr_tail` attributes — used consistently in test (Task 23) and endpoints (Task 26). ✅
- `SliceRequest` C++ struct fields (`filament_map`, `filament_settings_id`) added in Task 11 — referenced by Task 13's slicing logic. ✅
- `materialize_profiles_for_binary` returns `{"machine": str, "process": str, "filaments": list[str]}` — consumed identically in Tasks 26 and 27. ✅

No issues found.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-02-phase-1-orca-headless.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
