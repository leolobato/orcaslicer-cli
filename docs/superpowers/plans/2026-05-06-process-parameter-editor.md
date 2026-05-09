# Process Parameter Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose process-parameter metadata, GUI layout, project-modified keys, and a per-slice override path so the gateway iOS and webui sibling apps can present a GUI-parity process editor.

**Architecture:** Two new C++ extractors (`orca-headless dump-options` runtime mode + `scripts/extract_tab_layout.py` build-time tool) feed a new `app/options.py` module that serves three endpoints. `/3mf/{token}/inspect` is extended with a `process_modifications` block. `/slice/v2` gains an optional `process_overrides` JSON field whose values overlay onto the resolved process config in `slice_mode.cpp` after the existing 3MF transfer.

**Tech Stack:** C++17 + libslic3r + nlohmann::json on the binary side; Python 3 + FastAPI + Pydantic + pytest on the API side; Python regex extractor at build time.

**Reference docs:** Spec at `docs/superpowers/specs/2026-05-06-process-parameter-editor-design.md`. CLAUDE.md is the source of truth for project conventions; in particular: GUI parity over reimplementation, build via `docker compose build`, tests via `docker exec`.

---

## File Structure

| Path | Status | Purpose |
|---|---|---|
| `cpp/src/dump_options_mode.h` | new | header for the new runtime mode |
| `cpp/src/dump_options_mode.cpp` | new | walks `print_config_def`, emits per-option JSON |
| `cpp/src/json_io.h` | modified | adds `DumpOptionsRequest` and `process_overrides` to `SliceRequest` |
| `cpp/src/json_io.cpp` | modified | parses both new request shapes |
| `cpp/src/slice_mode.cpp` | modified | applies `process_overrides` overlay; populates `process_overrides_applied` in `settings_transfer` |
| `cpp/src/orca_headless.cpp` | modified | registers the `dump-options` subcommand |
| `cpp/CMakeLists.txt` | modified | adds `dump_options_mode.cpp` to sources |
| `scripts/extract_tab_layout.py` | new | parses Tab.cpp's `TabPrint::build()`, emits `process_pages.json` |
| `scripts/check_allowlist.py` | new | drift checks across allowlist / pages.json / dump-options |
| `cpp/src/generated/process_pages.json` | new (generated, committed) | extracted page→optgroup→option layout |
| `app/process_allowlist.json` | new | curated set of editable process keys |
| `app/options.py` | new | metadata + layout loader and cache |
| `app/binary_client.py` | modified | adds `BinaryClient.dump_options()` |
| `app/inspect.py` | modified | adds `process_modifications` block to inspect payload |
| `app/main.py` | modified | mounts new endpoints; adds `process_overrides` field to `SliceTokenRequest`; calls options loader from `lifespan` |
| `Makefile` | modified (or new) | adds `extract-layout` target |
| `tests/test_options_module.py` | new | unit tests for the loader and filter |
| `tests/test_extract_tab_layout.py` | new | unit tests for the regex extractor |
| `tests/test_check_allowlist.py` | new | unit tests for drift detection |
| `tests/test_dump_options_smoke.py` | new | shells out to built binary, sanity-checks the dump |
| `tests/test_inspect_process_modifications.py` | new | unit test for the inspect extension |
| `tests/integration/test_process_overrides.py` | new | end-to-end slice with overrides |
| `.github/workflows/ci.yml` | modified | runs `check_allowlist.py --check` and the extractor `--check` |

---

## Phase 1 — `dump-options` C++ mode

### Task 1: Add `DumpOptionsRequest` to `json_io`

**Files:**
- Modify: `cpp/src/json_io.h`
- Modify: `cpp/src/json_io.cpp`

- [ ] **Step 1: Extend `json_io.h` with the request struct and parser declaration**

In `cpp/src/json_io.h`, after the existing `DumpProfilesRequest` struct (around the bottom of the file, before the function declarations), add:

```cpp
// Walks libslic3r's static ``print_config_def`` registry and writes a JSON
// catalogue of every process-domain option's metadata (label, type,
// min/max, enum values, tooltip, mode, gui_type) to ``out_path``. No
// PresetBundle, no profiles_dir — the registry is statically initialised
// inside libslic3r on link.
struct DumpOptionsRequest {
    std::string out_path;       // /tmp/options-manifest.json
};

DumpOptionsRequest parse_dump_options_request_from_stdin();
```

Place the struct adjacent to `DumpProfilesRequest` (same section). Place the function declaration after `parse_dump_profiles_request_from_stdin();`.

- [ ] **Step 2: Implement the parser in `json_io.cpp`**

In `cpp/src/json_io.cpp`, after `parse_dump_profiles_request_from_stdin()` (last function in the file), append:

```cpp
DumpOptionsRequest parse_dump_options_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());
    DumpOptionsRequest req;
    req.out_path = j.value("out_path", std::string{});
    if (req.out_path.empty())
        throw std::runtime_error("dump-options: out_path is required");
    return req;
}
```

- [ ] **Step 3: Commit**

```bash
git add cpp/src/json_io.h cpp/src/json_io.cpp
git commit -m "cpp: add DumpOptionsRequest plumbing to json_io"
```

---

### Task 2: Implement `dump_options_mode`

**Files:**
- Create: `cpp/src/dump_options_mode.h`
- Create: `cpp/src/dump_options_mode.cpp`

- [ ] **Step 1: Create the header**

`cpp/src/dump_options_mode.h`:

```cpp
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
```

- [ ] **Step 2: Create the implementation**

`cpp/src/dump_options_mode.cpp`:

```cpp
#include "dump_options_mode.h"

#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <set>
#include <string>

#include <nlohmann/json.hpp>

#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Config.hpp"

namespace orca_headless {

namespace {

// Stringify ConfigOptionType to match the names used in libslic3r's
// header (coBool, coFloat, coInt, coString, coEnum, coPercent,
// coFloatOrPercent, coPoint, coPoints, coBools, coFloats, coInts,
// coStrings, coPercents, coNone). Anything we don't recognise gets
// passed through as the integer representation; that's fine for clients
// who only care about the common cases.
const char* config_option_type_name(Slic3r::ConfigOptionType t) {
    using Slic3r::ConfigOptionType;
    switch (t) {
        case Slic3r::coNone:             return "coNone";
        case Slic3r::coFloat:            return "coFloat";
        case Slic3r::coFloats:           return "coFloats";
        case Slic3r::coInt:              return "coInt";
        case Slic3r::coInts:             return "coInts";
        case Slic3r::coString:           return "coString";
        case Slic3r::coStrings:          return "coStrings";
        case Slic3r::coPercent:          return "coPercent";
        case Slic3r::coPercents:         return "coPercents";
        case Slic3r::coFloatOrPercent:   return "coFloatOrPercent";
        case Slic3r::coFloatsOrPercents: return "coFloatsOrPercents";
        case Slic3r::coPoint:            return "coPoint";
        case Slic3r::coPoints:           return "coPoints";
        case Slic3r::coPoint3:           return "coPoint3";
        case Slic3r::coBool:             return "coBool";
        case Slic3r::coBools:            return "coBools";
        case Slic3r::coEnum:             return "coEnum";
        default:                         return "coUnknown";
    }
}

const char* config_option_mode_name(Slic3r::ConfigOptionMode m) {
    switch (m) {
        case Slic3r::comSimple:   return "simple";
        case Slic3r::comAdvanced: return "advanced";
        case Slic3r::comDevelop:  return "develop";
        default:                  return "unknown";
    }
}

// Returns true when this option belongs to the process domain. We exclude:
//   - options without a category (libslic3r registers many internal /
//     extruder-side options with no GUI surface)
//   - filament-domain options (key starts with "filament_" or ends with
//     "_filament", or category is "Filament"). These belong to the
//     filament editor, not the process editor.
//   - machine-domain options (category is "Machine limits", or key is
//     a known printer-only key). The process editor doesn't expose them.
//
// The category-based filter alone is mostly correct because Tab.cpp's
// TabPrint::build() only references options with TabPrint-side categories
// (Quality / Strength / Speed / Support / Multimaterial / Others /
// Advanced). The key-prefix filter belt-and-suspenders that.
bool is_process_domain_option(const std::string& key,
                              const Slic3r::ConfigOptionDef& def) {
    if (def.category.empty()) return false;
    auto starts_with = [](const std::string& s, const char* p) {
        const size_t n = std::strlen(p);
        return s.size() >= n && std::memcmp(s.data(), p, n) == 0;
    };
    auto ends_with = [](const std::string& s, const char* p) {
        const size_t n = std::strlen(p);
        return s.size() >= n &&
            std::memcmp(s.data() + s.size() - n, p, n) == 0;
    };
    if (starts_with(key, "filament_") || ends_with(key, "_filament"))
        return false;
    if (def.category == "Filament") return false;
    if (def.category == "Machine limits") return false;
    return true;
}

// Serialize the default_value through the option's own ``serialize()``.
// This is the same path the GUI uses to render initial values, so vector
// and percent options come out in the canonical config-string form
// (matches what Python's project_settings.config produces).
std::string serialize_default(const Slic3r::ConfigOptionDef& def) {
    if (!def.default_value) return "";
    return def.default_value->serialize();
}

// Min/max sentinels in libslic3r are FLT_MIN / FLT_MAX (not infinity).
// Compare against the actual sentinels used in PrintConfig.cpp; if the
// option didn't set a bound, the field is the type's full range.
bool has_finite_min(double v) {
    // libslic3r uses ``-FLT_MAX`` for "no min set" on float options
    // (see ConfigOptionDef default).
    return v > -std::numeric_limits<float>::max() * 0.99;
}
bool has_finite_max(double v) {
    return v < std::numeric_limits<float>::max() * 0.99;
}

void emit_options(nlohmann::json& out) {
    const auto& defs = Slic3r::print_config_def.options;
    for (const auto& [key, def] : defs) {
        if (!is_process_domain_option(key, def)) continue;
        nlohmann::json e;
        e["key"]      = key;
        e["label"]    = def.label;
        e["category"] = def.category;
        e["tooltip"]  = def.tooltip;
        e["type"]     = config_option_type_name(def.type);
        e["sidetext"] = def.sidetext;
        e["default"]  = serialize_default(def);

        if (has_finite_min(def.min))
            e["min"] = def.min;
        else
            e["min"] = nullptr;
        if (has_finite_max(def.max))
            e["max"] = def.max;
        else
            e["max"] = nullptr;

        if (!def.enum_values.empty())
            e["enum_values"] = def.enum_values;
        else
            e["enum_values"] = nullptr;

        // enum_labels may be empty (libslic3r falls back to enum_values
        // for display in that case); preserve the distinction.
        if (!def.enum_labels.empty())
            e["enum_labels"] = def.enum_labels;
        else
            e["enum_labels"] = nullptr;

        e["mode"]     = config_option_mode_name(def.mode);
        e["gui_type"] = def.gui_type;
        e["nullable"] = def.nullable;
        e["readonly"] = def.readonly;
        out.push_back(std::move(e));
    }
}

}  // namespace

int run_dump_options_mode(const DumpOptionsRequest& req) {
    auto fail = [&](const char* code, const std::string& msg) {
        nlohmann::json err = {
            {"status",  "error"},
            {"code",    code},
            {"message", msg},
            {"details", nlohmann::json::object()},
        };
        std::cout << err.dump() << std::endl;
        return 1;
    };

    nlohmann::json catalogue;
    catalogue["options"] = nlohmann::json::array();
    try {
        emit_options(catalogue["options"]);
    } catch (const std::exception& e) {
        return fail("emit_failed", e.what());
    }

    std::ofstream ofs(req.out_path);
    if (!ofs) return fail("io_error",
                          "cannot open out_path for write: " + req.out_path);
    ofs << catalogue.dump();

    nlohmann::json ok = {
        {"status",  "ok"},
        {"code",    "done"},
        {"message", ""},
        {"details", {
            {"out_path", req.out_path},
            {"count",    catalogue["options"].size()},
        }},
    };
    std::cout << ok.dump() << std::endl;
    return 0;
}

}  // namespace orca_headless
```

- [ ] **Step 3: Commit**

```bash
git add cpp/src/dump_options_mode.h cpp/src/dump_options_mode.cpp
git commit -m "cpp: add dump_options_mode emitting print_config_def metadata"
```

---

### Task 3: Wire `dump-options` into the binary

**Files:**
- Modify: `cpp/src/orca_headless.cpp`
- Modify: `cpp/CMakeLists.txt`

- [ ] **Step 1: Register the include and dispatch arm**

In `cpp/src/orca_headless.cpp`, add the include after the existing `dump_profiles_mode.h` include (line 17):

```cpp
#include "dump_profiles_mode.h"
#include "dump_options_mode.h"
```

In the same file, extend the usage string in `print_usage` (around line 38–48) by adding a line under the existing commands:

```cpp
"  dump-options         Read JSON {out_path} on stdin, emit option metadata catalogue\n",
```

After the `dump-profiles` dispatch arm (around line 72–80 in `main`), add:

```cpp
    if (std::strcmp(argv[1], "dump-options") == 0) {
        try {
            auto req = orca_headless::parse_dump_options_request_from_stdin();
            return orca_headless::run_dump_options_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            return 1;
        }
    }
```

- [ ] **Step 2: Add the new source to CMake**

In `cpp/CMakeLists.txt`, in the `add_executable(orca-headless ...)` block (around line 38–46), insert `src/dump_options_mode.cpp` next to `src/dump_profiles_mode.cpp`:

```cmake
add_executable(orca-headless
    src/orca_headless.cpp
    src/json_io.cpp
    src/progress.cpp
    src/slice_mode.cpp
    src/use_set_mode.cpp
    src/dump_profiles_mode.cpp
    src/dump_options_mode.cpp
    src/nanosvg_impl.cpp
)
```

- [ ] **Step 3: Build the binary**

The dev-shell-cpp container (per `docs/dev-shell-cpp.md`) gives 30s–8min incremental rebuilds. Use it instead of `docker compose build`:

```bash
# inside the dev shell, from /workspace/cpp/build:
cmake --build . --target orca-headless -j
```

Expected: target builds without errors. If `print_config_def.options` doesn't exist as a `std::map`, check `vendor/OrcaSlicer/src/libslic3r/PrintConfig.hpp` for the actual member name (it has been `options` for years; only adjust if the vendor bumped).

- [ ] **Step 4: Smoke the new subcommand**

```bash
echo '{"out_path":"/tmp/opts.json"}' | /opt/orca-headless/bin/orca-headless dump-options
```

Expected stdout: `{"status":"ok","code":"done","message":"","details":{"out_path":"/tmp/opts.json","count":NNN}}` where `NNN > 100`.

```bash
python3 -c "import json; d=json.load(open('/tmp/opts.json')); print(len(d['options'])); print(d['options'][0])"
```

Expected: prints the option count (matches `count` from the envelope) and the first option as a dict with keys `key`, `label`, `category`, `tooltip`, `type`, etc. Verify `layer_height` is present:

```bash
python3 -c "import json; d=json.load(open('/tmp/opts.json'));
opts={o['key']:o for o in d['options']};
lh=opts['layer_height'];
assert lh['category']=='Quality', lh
assert lh['type']=='coFloat', lh
assert lh['min']==0.0, lh
print('layer_height OK')"
```

- [ ] **Step 5: Commit**

```bash
git add cpp/src/orca_headless.cpp cpp/CMakeLists.txt
git commit -m "cpp: register dump-options subcommand"
```

---

### Task 4: Pin a Python smoke test against the built binary

**Files:**
- Create: `tests/test_dump_options_smoke.py`

- [ ] **Step 1: Write the test**

`tests/test_dump_options_smoke.py`:

```python
"""Smoke test: orca-headless dump-options runs and emits sensible metadata.

Runs against the actual binary built into the container — guards against
regressions in the C++ extractor without faking the call.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import config as cfg


@pytest.mark.skipif(
    not Path(cfg.ORCA_HEADLESS_BINARY).exists(),
    reason="orca-headless binary not present (run inside container)",
)
def test_dump_options_emits_layer_height(tmp_path: Path) -> None:
    out = tmp_path / "opts.json"
    proc = subprocess.run(
        [cfg.ORCA_HEADLESS_BINARY, "dump-options"],
        input=json.dumps({"out_path": str(out)}).encode(),
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    envelope = json.loads(proc.stdout)
    assert envelope["status"] == "ok"
    catalogue = json.loads(out.read_text())
    by_key = {o["key"]: o for o in catalogue["options"]}

    assert "layer_height" in by_key
    lh = by_key["layer_height"]
    assert lh["category"] == "Quality"
    assert lh["type"] == "coFloat"
    assert lh["sidetext"]  # non-empty unit string
    assert lh["min"] is not None and lh["min"] >= 0.0
    assert lh["max"] is not None and lh["max"] > lh["min"]
    assert lh["mode"] in {"simple", "advanced", "develop"}
    assert isinstance(lh["default"], str)

    # Filament-domain keys should be excluded.
    assert not any(k.startswith("filament_") for k in by_key), \
        "process dump leaked filament_* keys"
    assert not any(k.endswith("_filament") for k in by_key), \
        "process dump leaked *_filament keys"

    # An enum option should round-trip enum_values + enum_labels.
    assert "seam_position" in by_key
    sp = by_key["seam_position"]
    assert sp["type"] == "coEnum"
    assert sp["enum_values"], "seam_position should have enum_values"
```

- [ ] **Step 2: Run the test inside the container**

```bash
docker exec orcaslicer-cli pytest tests/test_dump_options_smoke.py -v
```

Expected: PASS. If `seam_position` has changed type or enum names upstream, the assertion will tell us — adjust to match the current libslic3r definition (the test is a parity check, not a contract).

- [ ] **Step 3: Commit**

```bash
git add tests/test_dump_options_smoke.py
git commit -m "tests: smoke test orca-headless dump-options output"
```

---

## Phase 2 — Tab.cpp layout extractor

### Task 5: Write `extract_tab_layout.py` with unit tests

**Files:**
- Create: `scripts/extract_tab_layout.py`
- Create: `tests/test_extract_tab_layout.py`

- [ ] **Step 1: Write the failing unit tests**

`tests/test_extract_tab_layout.py`:

```python
"""Tests for scripts/extract_tab_layout.py.

The extractor runs a regex pass over Tab.cpp's TabPrint::build() function
to harvest the page → optgroup → option ordering. We test it against
synthetic Tab.cpp snippets so the test doesn't depend on the vendored
source's exact contents.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.extract_tab_layout import extract_print_layout


def test_simple_one_page_one_optgroup() -> None:
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            auto page = add_options_page(L("Quality"), "empty");
                auto optgroup = page->new_optgroup(L("Layer height"));
                optgroup->append_single_option_line("layer_height","anchor1");
                optgroup->append_single_option_line("initial_layer_print_height","anchor2");
        }
        """).strip()
    layout = extract_print_layout(src)
    assert layout == [
        {
            "label": "Quality",
            "optgroups": [
                {
                    "label": "Layer height",
                    "options": ["layer_height", "initial_layer_print_height"],
                },
            ],
        },
    ]


def test_multiple_pages_and_optgroups() -> None:
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            page = add_options_page(L("Quality"), "empty");
                optgroup = page->new_optgroup(L("Layer height"));
                optgroup->append_single_option_line("layer_height","a");
                optgroup = page->new_optgroup(L("Line width"));
                optgroup->append_single_option_line("line_width","b");
            page = add_options_page(L("Strength"), "empty");
                optgroup = page->new_optgroup(L("Walls"));
                optgroup->append_single_option_line("wall_loops","c");
        }
        void TabPrint::other_method()
        {
            // Should NOT be picked up — outside TabPrint::build().
            auto page = add_options_page(L("Bogus"), "empty");
        }
        """).strip()
    layout = extract_print_layout(src)
    page_labels = [p["label"] for p in layout]
    assert page_labels == ["Quality", "Strength"], \
        "must scope to TabPrint::build, must not leak from other_method"
    quality = layout[0]
    assert [og["label"] for og in quality["optgroups"]] == \
        ["Layer height", "Line width"]


def test_skips_commented_lines() -> None:
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            auto page = add_options_page(L("Quality"), "empty");
                auto optgroup = page->new_optgroup(L("Layer height"));
                // optgroup->append_single_option_line("commented_out","x");
                optgroup->append_single_option_line("layer_height","y");
        }
        """).strip()
    layout = extract_print_layout(src)
    keys = layout[0]["optgroups"][0]["options"]
    assert keys == ["layer_height"]
    assert "commented_out" not in keys


def test_orphan_option_before_any_optgroup_is_dropped() -> None:
    """A safety net for malformed Tab.cpp shapes — we should not crash."""
    src = textwrap.dedent("""
        void TabPrint::build()
        {
            optgroup->append_single_option_line("orphan","x");
            auto page = add_options_page(L("Quality"), "empty");
                auto optgroup = page->new_optgroup(L("Layer height"));
                optgroup->append_single_option_line("layer_height","y");
        }
        """).strip()
    layout = extract_print_layout(src)
    # The orphan must not appear anywhere.
    all_keys = [k for p in layout for og in p["optgroups"] for k in og["options"]]
    assert "orphan" not in all_keys
    assert all_keys == ["layer_height"]
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
docker exec orcaslicer-cli pytest tests/test_extract_tab_layout.py -v
```

Expected: FAIL with `ModuleNotFoundError: scripts.extract_tab_layout`.

- [ ] **Step 3: Implement the extractor**

`scripts/extract_tab_layout.py`:

```python
"""Extract TabPrint::build() page → optgroup → option layout from Tab.cpp.

Runs at vendor-bump time. Output goes to ``cpp/src/generated/process_pages.json``
which is checked into git so devs and CI don't re-run extraction unless
Tab.cpp itself changed.

Usage:
    python scripts/extract_tab_layout.py            # write generated JSON
    python scripts/extract_tab_layout.py --check    # exit non-zero if stale
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TAB_CPP = REPO_ROOT / "vendor" / "OrcaSlicer" / "src" / "slic3r" / "GUI" / "Tab.cpp"
OUT_PATH = REPO_ROOT / "cpp" / "src" / "generated" / "process_pages.json"

# Regexes target the call shape inside TabPrint::build(). Tab.cpp has
# been stable on these names for years; if upstream restructures the
# file, the call sites are the things to fix in this script.
RE_PAGE     = re.compile(r'add_options_page\s*\(\s*L\("([^"]+)"\)')
RE_OPTGROUP = re.compile(r'new_optgroup\s*\(\s*L\("([^"]+)"\)')
RE_APPEND   = re.compile(r'append_single_option_line\s*\(\s*"([^"]+)"')

# Identifies the start of TabPrint::build()'s body. We scope extraction
# to lines from this signature up to the matching closing brace at
# column 0. The extractor is deliberately strict about the signature
# shape — if it changes upstream we want to know.
RE_BUILD_START = re.compile(r'^void\s+TabPrint::build\s*\(\s*\)')


def _strip_line_comment(line: str) -> str:
    """Drop everything from `//` onwards. We don't strip block comments
    because Tab.cpp doesn't use them inside build(); add support if needed."""
    idx = line.find("//")
    return line if idx == -1 else line[:idx]


def _scope_to_build_function(src: str) -> str:
    """Return only the body of TabPrint::build(), or empty string if absent.

    Brace counting starts at the first `{` after the signature; the
    body ends when the depth returns to 0.
    """
    lines = src.splitlines()
    in_build = False
    depth = 0
    body: list[str] = []
    for line in lines:
        if not in_build:
            if RE_BUILD_START.search(line):
                in_build = True
                # If `{` is on the signature line, count it; otherwise
                # depth stays 0 until we see the opening brace.
                depth = line.count("{") - line.count("}")
                if depth > 0:
                    body.append(line)
            continue
        body.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
    return "\n".join(body)


def extract_print_layout(src: str) -> list[dict]:
    """Parse a Tab.cpp source string and return the page layout.

    Output: a list of pages. Each page has ``label`` and ``optgroups``.
    Each optgroup has ``label`` and ``options`` (list of option keys in
    Tab.cpp order). Options before any page or optgroup are dropped.
    """
    body = _scope_to_build_function(src)
    pages: list[dict] = []
    cur_page: dict | None = None
    cur_optgroup: dict | None = None
    for raw in body.splitlines():
        line = _strip_line_comment(raw)
        m_page = RE_PAGE.search(line)
        if m_page:
            cur_page = {"label": m_page.group(1), "optgroups": []}
            cur_optgroup = None
            pages.append(cur_page)
            continue
        m_og = RE_OPTGROUP.search(line)
        if m_og and cur_page is not None:
            cur_optgroup = {"label": m_og.group(1), "options": []}
            cur_page["optgroups"].append(cur_optgroup)
            continue
        m_app = RE_APPEND.search(line)
        if m_app and cur_optgroup is not None:
            cur_optgroup["options"].append(m_app.group(1))
            continue
    return pages


def _git_blob_sha(path: Path) -> str:
    """Return the SHA git would record for this file's current content."""
    out = subprocess.run(
        ["git", "hash-object", str(path)],
        capture_output=True, check=True, text=True,
    )
    return out.stdout.strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true",
                   help="exit non-zero if generated JSON is stale")
    args = p.parse_args()

    if not TAB_CPP.exists():
        print(f"error: {TAB_CPP} not found", file=sys.stderr)
        return 2

    src = TAB_CPP.read_text(encoding="utf-8")
    layout = extract_print_layout(src)
    if not layout:
        print("error: extraction yielded zero pages — regex needs work",
              file=sys.stderr)
        return 2

    new_doc = {
        "extracted_from_path": str(TAB_CPP.relative_to(REPO_ROOT)),
        "extracted_from_sha":  _git_blob_sha(TAB_CPP),
        "pages": layout,
    }

    if args.check:
        if not OUT_PATH.exists():
            print(f"error: {OUT_PATH} missing; run without --check to generate",
                  file=sys.stderr)
            return 1
        existing = json.loads(OUT_PATH.read_text())
        # Compare on the actual layout + source SHA, ignoring extracted_at-
        # style fields (none today, but future-proof).
        for k in ("extracted_from_sha", "pages"):
            if existing.get(k) != new_doc[k]:
                print(f"error: {OUT_PATH} is stale (key {k!r} differs); "
                      "run scripts/extract_tab_layout.py", file=sys.stderr)
                return 1
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(new_doc, indent=2) + "\n")
    print(f"wrote {OUT_PATH} ({len(layout)} pages, "
          f"{sum(len(p['optgroups']) for p in layout)} optgroups, "
          f"{sum(len(og['options']) for p in layout for og in p['optgroups'])} options)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the unit tests**

```bash
docker exec orcaslicer-cli pytest tests/test_extract_tab_layout.py -v
```

Expected: all four tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/extract_tab_layout.py tests/test_extract_tab_layout.py
git commit -m "tools: add Tab.cpp print layout regex extractor + tests"
```

---

### Task 6: Run the extractor and commit `process_pages.json`

**Files:**
- Create: `cpp/src/generated/process_pages.json` (committed output)
- Modify: `Makefile` (create if absent)

- [ ] **Step 1: Run the extractor against the vendored Tab.cpp**

```bash
docker exec orcaslicer-cli python scripts/extract_tab_layout.py
```

Expected stdout: `wrote .../process_pages.json (N pages, M optgroups, K options)` where N is around 6 (Quality / Strength / Speed / Support / Multimaterial / Others) and K is in the hundreds.

- [ ] **Step 2: Sanity-check the output**

```bash
docker exec orcaslicer-cli python3 -c "
import json
d = json.load(open('cpp/src/generated/process_pages.json'))
print('pages:', [p['label'] for p in d['pages']])
print('quality optgroups:', [og['label'] for og in d['pages'][0]['optgroups']])
quality_keys = [k for og in d['pages'][0]['optgroups'] for k in og['options']]
assert 'layer_height' in quality_keys, quality_keys
print('layer_height in Quality:', True)
"
```

Expected: page list begins with `Quality`, `Strength`, …; Quality contains `Layer height`, `Line width`, etc.; `layer_height` is found.

- [ ] **Step 3: Add a Makefile target**

If `Makefile` doesn't exist at the repo root, create it with:

```makefile
.PHONY: extract-layout extract-layout-check

extract-layout:
	python scripts/extract_tab_layout.py

extract-layout-check:
	python scripts/extract_tab_layout.py --check
```

If a Makefile exists, append the same two targets and `.PHONY` line to it (don't duplicate `.PHONY:`).

- [ ] **Step 4: Commit**

```bash
git add cpp/src/generated/process_pages.json Makefile
git commit -m "tools: extract initial process_pages.json from Tab.cpp 2.3.2"
```

---

## Phase 3 — Python options module + endpoints

### Task 7: Add `BinaryClient.dump_options()`

**Files:**
- Modify: `app/binary_client.py`
- Modify: `tests/test_binary_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_binary_client.py`:

```python
async def test_dump_options_returns_catalogue(client: BinaryClient, tmp_path) -> None:
    """The wrapper writes its own out_path, reads back the file."""
    catalogue = {"options": [{"key": "layer_height", "label": "Layer height"}]}
    fake_envelope = {
        "status": "ok",
        "code": "done",
        "message": "",
        "details": {"out_path": "ignored", "count": 1},
    }

    captured: dict = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        proc = AsyncMock()
        # Wrapper writes the request to stdin; we ignore its content
        # because the wrapper sets out_path to a tempfile it created.
        async def communicate(input: bytes):
            req = json.loads(input)
            Path(req["out_path"]).write_text(json.dumps(catalogue))
            return (json.dumps(fake_envelope).encode(), b"")
        proc.communicate = communicate
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        result = await client.dump_options(timeout_s=10.0)

    assert result == catalogue
    assert "dump-options" in captured["args"]


async def test_dump_options_raises_on_error_envelope(client: BinaryClient) -> None:
    err = {"status": "error", "code": "io_error", "message": "no path"}

    async def fake_exec(*args, **kwargs):
        proc = AsyncMock()
        async def communicate(input: bytes):
            return (json.dumps(err).encode(), b"")
        proc.communicate = communicate
        proc.returncode = 1
        return proc

    with patch("asyncio.create_subprocess_exec", fake_exec):
        with pytest.raises(BinaryError) as exc:
            await client.dump_options(timeout_s=10.0)
    assert exc.value.code == "io_error"
```

- [ ] **Step 2: Run to confirm failure**

```bash
docker exec orcaslicer-cli pytest tests/test_binary_client.py::test_dump_options_returns_catalogue -v
```

Expected: FAIL with `AttributeError: 'BinaryClient' object has no attribute 'dump_options'`.

- [ ] **Step 3: Implement `BinaryClient.dump_options`**

In `app/binary_client.py`, add the following method to the `BinaryClient` class (place it after `use_set`, before `slice_stream`):

```python
    async def dump_options(self, *, timeout_s: float = 30.0) -> dict[str, Any]:
        """Invoke ``orca-headless dump-options`` and return the catalogue dict.

        The binary writes the JSON catalogue to a temp file we create here,
        and only emits the success/error envelope on stdout. Returns the
        parsed catalogue (``{"options": [...]}``); raises ``BinaryError``
        on a non-OK envelope or subprocess failure.
        """
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            out_path = tf.name
        try:
            request = {"out_path": out_path}
            proc = await asyncio.create_subprocess_exec(
                self.binary_path, "dump-options",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=json.dumps(request).encode()),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise BinaryError(
                    code="binary_timeout",
                    message=f"dump-options timed out after {timeout_s}s",
                    details={},
                )

            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

            if proc.returncode != 0 and not stdout.strip():
                raise BinaryError(
                    code="binary_crashed",
                    message=f"dump-options exited {proc.returncode} with no stdout",
                    details={},
                    stderr_tail=stderr_text[-2000:],
                )

            try:
                envelope = json.loads(stdout)
            except json.JSONDecodeError as e:
                raise BinaryError(
                    code="binary_bad_response",
                    message=f"could not parse stdout as JSON: {e}",
                    details={"stdout_head": stdout[:500].decode("utf-8", errors="replace")},
                    stderr_tail=stderr_text[-2000:],
                )

            if envelope.get("status") != "ok":
                raise BinaryError(
                    code=envelope.get("code", "unknown"),
                    message=envelope.get("message", ""),
                    details=envelope.get("details", {}),
                    stderr_tail=stderr_text[-2000:],
                )

            with open(out_path, "r", encoding="utf-8") as f:
                return json.load(f)
        finally:
            Path(out_path).unlink(missing_ok=True)
```

Add the `Path` import at the top if not already present:

```python
from pathlib import Path
```

- [ ] **Step 4: Run the tests to confirm they pass**

```bash
docker exec orcaslicer-cli pytest tests/test_binary_client.py -v -k dump_options
```

Expected: both new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/binary_client.py tests/test_binary_client.py
git commit -m "binary_client: add dump_options() wrapper"
```

---

### Task 8: Seed `app/process_allowlist.json`

**Files:**
- Create: `app/process_allowlist.json`

- [ ] **Step 1: Create the seed allowlist**

`app/process_allowlist.json`:

```json
{
  "revision": "2026-05-06.1",
  "options": [
    "layer_height",
    "initial_layer_print_height",
    "wall_loops",
    "top_shell_layers",
    "bottom_shell_layers",
    "sparse_infill_density",
    "sparse_infill_pattern",
    "enable_support",
    "support_type",
    "support_threshold_angle",
    "brim_type",
    "brim_width"
  ]
}
```

These keys are all confirmed present in the OrcaSlicer 2.3.2 process Tab. The list is intentionally small; it grows by editing this file and bumping `revision`.

- [ ] **Step 2: Commit**

```bash
git add app/process_allowlist.json
git commit -m "options: seed process allowlist with twelve common knobs"
```

---

### Task 9: Create `app/options.py`

**Files:**
- Create: `app/options.py`
- Create: `tests/test_options_module.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_options_module.py`:

```python
"""Unit tests for app.options — loader, allowlist filter, and cache."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import options


@pytest.fixture
def fake_layout(tmp_path: Path) -> Path:
    p = tmp_path / "process_pages.json"
    p.write_text(json.dumps({
        "extracted_from_path": "vendor/.../Tab.cpp",
        "extracted_from_sha": "deadbeef",
        "pages": [
            {
                "label": "Quality",
                "optgroups": [
                    {"label": "Layer height",
                     "options": ["layer_height", "initial_layer_print_height"]},
                    {"label": "Seam",
                     "options": ["seam_position"]},
                ],
            },
            {
                "label": "Strength",
                "optgroups": [
                    {"label": "Walls",
                     "options": ["wall_loops"]},
                ],
            },
        ],
    }))
    return p


@pytest.fixture
def fake_allowlist(tmp_path: Path) -> Path:
    p = tmp_path / "process_allowlist.json"
    p.write_text(json.dumps({
        "revision": "test.1",
        "options": ["layer_height", "wall_loops"],
    }))
    return p


@pytest.fixture
def fake_catalogue() -> dict:
    return {
        "options": [
            {"key": "layer_height", "label": "Layer height", "category": "Quality",
             "type": "coFloat", "min": 0.0, "max": 0.6, "default": "0.2",
             "tooltip": "", "sidetext": "mm", "enum_values": None,
             "enum_labels": None, "mode": "simple", "gui_type": "",
             "nullable": False, "readonly": False},
            {"key": "wall_loops", "label": "Wall loops", "category": "Strength",
             "type": "coInt", "min": 0, "max": 1000, "default": "2",
             "tooltip": "", "sidetext": "", "enum_values": None,
             "enum_labels": None, "mode": "simple", "gui_type": "",
             "nullable": False, "readonly": False},
            {"key": "seam_position", "label": "Seam position", "category": "Quality",
             "type": "coEnum", "min": None, "max": None, "default": "aligned",
             "tooltip": "", "sidetext": "",
             "enum_values": ["nearest", "aligned", "back", "random"],
             "enum_labels": ["Nearest", "Aligned", "Back", "Random"],
             "mode": "simple", "gui_type": "", "nullable": False, "readonly": False},
        ],
    }


def test_filter_layout_drops_non_allowlisted_options(
    fake_layout: Path, fake_allowlist: Path,
) -> None:
    layout_doc = json.loads(fake_layout.read_text())
    allowlist = json.loads(fake_allowlist.read_text())
    filtered = options.filter_layout(layout_doc["pages"], set(allowlist["options"]))

    # Only Quality > Layer height (with layer_height kept) and
    # Strength > Walls (with wall_loops) survive.
    assert [p["label"] for p in filtered] == ["Quality", "Strength"]
    quality = filtered[0]
    assert [og["label"] for og in quality["optgroups"]] == ["Layer height"]
    assert quality["optgroups"][0]["options"] == ["layer_height"]
    assert filtered[1]["optgroups"][0]["options"] == ["wall_loops"]


def test_filter_layout_drops_empty_optgroups_and_pages(
    fake_layout: Path,
) -> None:
    layout_doc = json.loads(fake_layout.read_text())
    # Allowlist that knocks out everything in Strength.
    filtered = options.filter_layout(layout_doc["pages"], {"layer_height"})
    assert [p["label"] for p in filtered] == ["Quality"]
    assert [og["label"] for og in filtered[0]["optgroups"]] == ["Layer height"]


def test_filter_layout_preserves_option_order_within_optgroup(
    fake_layout: Path,
) -> None:
    layout_doc = json.loads(fake_layout.read_text())
    filtered = options.filter_layout(
        layout_doc["pages"],
        {"layer_height", "initial_layer_print_height"},
    )
    assert filtered[0]["optgroups"][0]["options"] == \
        ["layer_height", "initial_layer_print_height"]


async def test_load_into_cache_populates_metadata_and_layout(
    monkeypatch, fake_layout: Path, fake_allowlist: Path, fake_catalogue: dict,
) -> None:
    monkeypatch.setattr(options, "_LAYOUT_PATH", fake_layout)
    monkeypatch.setattr(options, "_ALLOWLIST_PATH", fake_allowlist)

    fake_client = AsyncMock()
    fake_client.dump_options = AsyncMock(return_value=fake_catalogue)
    cache = await options.load_options_cache(binary_client=fake_client)

    # /options/process payload — unfiltered; all three keys present.
    assert set(cache.metadata["options"]) == \
        {"layer_height", "wall_loops", "seam_position"}

    # /options/process/layout payload — filtered to allowlist.
    layout = cache.layout
    assert layout["allowlist_revision"] == "test.1"
    page_labels = [p["label"] for p in layout["pages"]]
    assert page_labels == ["Quality", "Strength"]
    layer_optgroup = layout["pages"][0]["optgroups"][0]
    assert layer_optgroup["options"] == ["layer_height"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
docker exec orcaslicer-cli pytest tests/test_options_module.py -v
```

Expected: FAIL with `ModuleNotFoundError: app.options`.

- [ ] **Step 3: Implement `app/options.py`**

`app/options.py`:

```python
"""Process-domain option metadata and layout for the parameter editor.

Loads two pieces of data at startup (and on /profiles/reload):

  1. The full per-option metadata catalogue, by shelling out to
     ``orca-headless dump-options``. Served verbatim at
     ``GET /options/process``.

  2. The page → optgroup → option layout extracted at build time from
     ``Tab.cpp::TabPrint::build()`` (lives at
     ``cpp/src/generated/process_pages.json``). Filtered by
     ``app/process_allowlist.json``: only allowlisted keys survive,
     empty optgroups and pages are dropped. Served at
     ``GET /options/process/layout``.

Both pieces are cached in module-level state. The metadata catalogue is
unfiltered so iOS/web can render labels for non-allowlisted modified
keys (which the editor shows read-only).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as cfg
from .binary_client import BinaryClient

logger = logging.getLogger(__name__)

_PKG_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_ROOT.parent

_LAYOUT_PATH = _REPO_ROOT / "cpp" / "src" / "generated" / "process_pages.json"
_ALLOWLIST_PATH = _PKG_ROOT / "process_allowlist.json"


@dataclass
class OptionsCache:
    """Module-level snapshot served by the /options/* endpoints."""
    metadata: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)


_cache = OptionsCache()


def filter_layout(
    pages: list[dict[str, Any]], allowed: set[str],
) -> list[dict[str, Any]]:
    """Drop options not in ``allowed``; drop optgroups and pages that empty out.

    Preserves option ordering within each surviving optgroup, and optgroup
    ordering within each surviving page.
    """
    result: list[dict[str, Any]] = []
    for page in pages:
        survived_optgroups: list[dict[str, Any]] = []
        for og in page.get("optgroups", []):
            kept = [k for k in og.get("options", []) if k in allowed]
            if kept:
                survived_optgroups.append({
                    "label": og.get("label", ""),
                    "options": kept,
                })
        if survived_optgroups:
            result.append({
                "label": page.get("label", ""),
                "optgroups": survived_optgroups,
            })
    return result


def _build_metadata(catalogue: dict[str, Any], api_version: str) -> dict[str, Any]:
    """Reshape the dump-options catalogue into the /options/process payload.

    The C++ side emits a list ``options: [{key,...}, ...]`` for streaming
    friendliness; the API serves it as a dict keyed by ``key`` for cheap
    client-side lookup.
    """
    return {
        "version": api_version,
        "options": {opt["key"]: opt for opt in catalogue.get("options", [])},
    }


def _build_layout(
    layout_doc: dict[str, Any],
    allowlist_doc: dict[str, Any],
    api_version: str,
) -> dict[str, Any]:
    pages = filter_layout(
        layout_doc.get("pages", []),
        set(allowlist_doc.get("options", [])),
    )
    return {
        "version": api_version,
        "allowlist_revision": allowlist_doc.get("revision", ""),
        "pages": pages,
    }


async def load_options_cache(*, binary_client: BinaryClient) -> OptionsCache:
    """Refresh the module-level cache. Call from app startup and /reload."""
    api_version = f"{cfg.ORCA_VERSION}-{cfg.API_REVISION}"

    catalogue = await binary_client.dump_options()
    metadata = _build_metadata(catalogue, api_version)

    if not _LAYOUT_PATH.exists():
        raise RuntimeError(
            f"process_pages.json missing at {_LAYOUT_PATH}; "
            "run scripts/extract_tab_layout.py")
    layout_doc = json.loads(_LAYOUT_PATH.read_text())
    if not _ALLOWLIST_PATH.exists():
        raise RuntimeError(f"process_allowlist.json missing at {_ALLOWLIST_PATH}")
    allowlist_doc = json.loads(_ALLOWLIST_PATH.read_text())

    layout = _build_layout(layout_doc, allowlist_doc, api_version)

    # Drop a warning for any allowlist key that doesn't appear in the
    # metadata dump — script check_allowlist.py is the strict gate; here
    # we only log so a curated allowlist mistake doesn't crash startup.
    metadata_keys = set(metadata["options"].keys())
    for key in allowlist_doc.get("options", []):
        if key not in metadata_keys:
            logger.warning(
                "allowlist references key %r which is not in dump-options "
                "(typo, removed upstream, or non-process-domain key)", key)

    _cache.metadata = metadata
    _cache.layout = layout
    logger.info(
        "options cache loaded: %d metadata entries, %d allowlisted keys "
        "across %d pages",
        len(metadata["options"]),
        sum(len(og["options"])
            for p in layout["pages"] for og in p["optgroups"]),
        len(layout["pages"]),
    )
    return _cache


def get_metadata() -> dict[str, Any]:
    """Return the cached /options/process payload."""
    return _cache.metadata


def get_layout() -> dict[str, Any]:
    """Return the cached /options/process/layout payload."""
    return _cache.layout
```

- [ ] **Step 4: Run the tests**

```bash
docker exec orcaslicer-cli pytest tests/test_options_module.py -v
```

Expected: all four tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/options.py tests/test_options_module.py
git commit -m "options: add metadata cache and allowlist-filtered layout loader"
```

---

### Task 10: Wire endpoints into FastAPI

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add the loader call to `lifespan`**

In `app/main.py`, locate the existing `lifespan` async context manager (around the top-level `lifespan` definition; search for `async def lifespan` in the file). After the existing profile load but before the `yield`, add:

```python
    # Load process-option metadata catalogue + allowlist-filtered layout.
    # Sources: orca-headless dump-options + cpp/src/generated/process_pages.json
    # + app/process_allowlist.json. See docs/.../process-parameter-editor-design.md.
    from app import options as options_module
    binary_for_options = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
    try:
        await options_module.load_options_cache(binary_client=binary_for_options)
    except Exception:
        logger.exception("failed to load options cache; /options/* endpoints will 503")
```

(If `BinaryClient` and `cfg` are not already imported at module scope, leave the existing imports alone — they are imported elsewhere in this file. The `from app import options as options_module` is local-import to keep startup ordering explicit.)

- [ ] **Step 2: Add `/options/process` endpoint**

Locate the `/profiles/reload` endpoint (around line 675) — group the new endpoints near it for cohesion. Insert before the `/profiles/reload` definition:

```python
@app.get("/options/process", tags=["Options"])
async def get_process_options() -> dict[str, Any]:
    """Per-option metadata catalogue for process-domain options.

    Sourced from libslic3r's print_config_def via orca-headless dump-options.
    Unfiltered — every process-domain option is included so iOS/web can render
    labels for keys that are modified on a project but not in the allowlist
    (those are shown read-only in the Modified view).
    """
    from app import options as options_module
    payload = options_module.get_metadata()
    if not payload.get("options"):
        return JSONResponse(status_code=503, content={
            "code": "options_not_loaded",
            "message": "options metadata cache empty; check startup logs",
        })
    return payload


@app.get("/options/process/layout", tags=["Options"])
async def get_process_options_layout() -> dict[str, Any]:
    """Page → optgroup → option layout for the process editor's All view.

    Filtered server-side by app/process_allowlist.json — only allowlisted
    keys survive, empty optgroups and pages are dropped. Layout sourced
    from cpp/src/generated/process_pages.json (extracted at build time
    from Tab.cpp::TabPrint::build()).
    """
    from app import options as options_module
    payload = options_module.get_layout()
    if not payload.get("pages"):
        return JSONResponse(status_code=503, content={
            "code": "options_layout_not_loaded",
            "message": "options layout cache empty; check startup logs",
        })
    return payload
```

- [ ] **Step 3: Refresh on `/profiles/reload`**

In the existing `/profiles/reload` handler (around line 675), find the `await reload_profiles()` (or equivalent) call and add an options reload after it:

```python
    from app import options as options_module
    try:
        await options_module.load_options_cache(
            binary_client=BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY))
    except Exception:
        logger.exception("options cache reload failed")
```

(Look for the existing `BinaryClient` instantiation pattern in the file; reuse the same idiom.)

- [ ] **Step 4: Smoke the endpoints inside the container**

```bash
docker exec orcaslicer-cli sh -c "curl -sf http://localhost:8070/options/process | python3 -c 'import json,sys; d=json.load(sys.stdin); print(\"metadata options:\", len(d[\"options\"])); print(\"layer_height:\", d[\"options\"][\"layer_height\"][\"category\"])'"
docker exec orcaslicer-cli sh -c "curl -sf http://localhost:8070/options/process/layout | python3 -c 'import json,sys; d=json.load(sys.stdin); print(\"pages:\", [p[\"label\"] for p in d[\"pages\"]]); print(\"allowlist_revision:\", d[\"allowlist_revision\"])'"
```

Expected:
- First call: prints `metadata options: NNN` (in the hundreds) and `layer_height: Quality`.
- Second call: prints something like `pages: ['Quality', 'Strength', 'Support']` (depending on which pages have allowlisted keys) and `allowlist_revision: 2026-05-06.1`.

- [ ] **Step 5: Commit**

```bash
git add app/main.py
git commit -m "main: mount /options/process and /options/process/layout"
```

---

## Phase 4 — `process_modifications` in `/3mf/{token}/inspect`

### Task 11: Extend `parse_inspect_data` with `process_modifications`

**Files:**
- Modify: `app/inspect.py`
- Create: `tests/test_inspect_process_modifications.py`

- [ ] **Step 1: Write the failing test**

`tests/test_inspect_process_modifications.py`:

```python
"""Test that parse_inspect_data exposes the project's modified process keys."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.inspect import parse_inspect_data


def _make_3mf_with_project_settings(settings: dict) -> bytes:
    """Build a minimal 3MF whose Metadata/project_settings.config matches."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(settings))
        # Minimal model relationships so the 3MF parser doesn't choke.
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("3D/3dmodel.model",
                    '<?xml version="1.0"?><model unit="millimeter"/>')
    return buf.getvalue()


def test_process_modifications_lists_modified_keys_and_values() -> None:
    settings = {
        "print_settings_id": "Custom 0.20mm Standard",
        "different_settings_to_system": [
            "layer_height;wall_loops",       # process slot
            "",                               # filament slot 0
            "",                               # printer slot
        ],
        "layer_height": "0.16",
        "wall_loops": "3",
        "sparse_infill_density": "20%",      # NOT in fingerprint, so not modified
    }
    data = parse_inspect_data(_make_3mf_with_project_settings(settings))

    assert "process_modifications" in data
    pm = data["process_modifications"]
    assert pm["process_setting_id"] == "Custom 0.20mm Standard"
    assert sorted(pm["modified_keys"]) == ["layer_height", "wall_loops"]
    assert pm["values"] == {"layer_height": "0.16", "wall_loops": "3"}


def test_process_modifications_empty_when_no_fingerprint() -> None:
    settings = {
        "print_settings_id": "0.20mm Standard @BBL P1S",
        # No different_settings_to_system at all.
        "layer_height": "0.20",
    }
    data = parse_inspect_data(_make_3mf_with_project_settings(settings))
    pm = data["process_modifications"]
    assert pm["modified_keys"] == []
    assert pm["values"] == {}
    # process_setting_id is still surfaced — it's a separate fact.
    assert pm["process_setting_id"] == "0.20mm Standard @BBL P1S"


def test_process_modifications_handles_missing_project_settings() -> None:
    """A 3MF without project_settings.config — process_modifications is empty."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("3D/3dmodel.model",
                    '<?xml version="1.0"?><model unit="millimeter"/>')
    data = parse_inspect_data(buf.getvalue())
    pm = data["process_modifications"]
    assert pm == {"process_setting_id": "", "modified_keys": [], "values": {}}
```

- [ ] **Step 2: Run the test to confirm failure**

```bash
docker exec orcaslicer-cli pytest tests/test_inspect_process_modifications.py -v
```

Expected: FAIL — `process_modifications` not in `data`.

- [ ] **Step 3: Implement the extension**

In `app/inspect.py`, locate the function `parse_inspect_data` (search for `def parse_inspect_data`). Locate where `project_settings` is parsed (around the existing `printer_settings_id` extraction near line 433). Add a helper above `parse_inspect_data` (or alongside the existing helpers in the same file):

```python
def _extract_process_modifications(
    project_settings: dict | None,
) -> dict:
    """Build the ``process_modifications`` block exposed by /inspect.

    Reads ``different_settings_to_system[0]`` from the 3MF's project settings
    (slot 0 is process per OrcaSlicer's PresetBundle::load_3mf_* layout)
    and pairs the modified keys with their current values.
    """
    if not project_settings:
        return {"process_setting_id": "", "modified_keys": [], "values": {}}

    process_setting_id = str(project_settings.get("print_settings_id", "") or "")
    diff_list = project_settings.get("different_settings_to_system") or []
    if not isinstance(diff_list, list) or not diff_list:
        return {
            "process_setting_id": process_setting_id,
            "modified_keys": [],
            "values": {},
        }

    raw = diff_list[0]
    if not isinstance(raw, str) or not raw:
        return {
            "process_setting_id": process_setting_id,
            "modified_keys": [],
            "values": {},
        }
    keys = [k for k in raw.split(";") if k]
    values = {
        k: project_settings[k]
        for k in keys
        if k in project_settings and isinstance(project_settings[k], (str, int, float, bool))
    }
    # Stringify non-strings so the API contract is stable
    # (project_settings.config encodes everything as strings, but a 3MF
    # produced by some tools may carry typed values).
    values = {k: str(v) for k, v in values.items()}
    return {
        "process_setting_id": process_setting_id,
        "modified_keys": keys,
        "values": values,
    }
```

In `parse_inspect_data` itself (around lines 384–445), the result dict is initialised at line 384 with default values for un-zippable inputs, then populated inside the `with zf:` block. Add the new key in two places:

1. Initial defaults (around line 384–398), inside the `out` dict literal:

```python
        "process_modifications": {
            "process_setting_id": "",
            "modified_keys": [],
            "values": {},
        },
```

2. Inside the `with zf:` block, after the existing `out["print_settings_id"] = project_settings.get(...)` assignment (around line 434–435), add:

```python
        out["process_modifications"] = _extract_process_modifications(project_settings)
```

Both placements ensure the field is always present regardless of whether the input was a valid zip — matching the existing pattern for `printer_settings_id`, `print_settings_id`, etc.

- [ ] **Step 4: Run the test**

```bash
docker exec orcaslicer-cli pytest tests/test_inspect_process_modifications.py -v
```

Expected: all three tests PASS.

- [ ] **Step 5: Verify the existing inspect tests still pass**

```bash
docker exec orcaslicer-cli pytest tests/test_inspect.py tests/integration/test_inspect_endpoint.py -v
```

Expected: PASS. If `INSPECT_SCHEMA_VERSION` is bumped on schema changes, locate it (search `INSPECT_SCHEMA_VERSION` in `app/inspect.py`) and increment the version number.

- [ ] **Step 6: Commit**

```bash
git add app/inspect.py tests/test_inspect_process_modifications.py
git commit -m "inspect: add process_modifications block from different_settings_to_system[0]"
```

---

## Phase 5 — `process_overrides` slice path

### Task 12: Plumb `process_overrides` through `SliceRequest`

**Files:**
- Modify: `cpp/src/json_io.h`
- Modify: `cpp/src/json_io.cpp`

- [ ] **Step 1: Extend the struct**

In `cpp/src/json_io.h`, add `<map>` to the includes if not already present:

```cpp
#include <map>
```

In the `SliceRequest` struct (around lines 9–51), add the new field after `plate_type`:

```cpp
    // Optional client-side process-domain customisations applied AFTER
    // the 3MF's own different_settings_to_system[0] overlay. Highest
    // priority — these win over both the system process profile and the
    // 3MF's customisations.
    //
    // Keys must be process-domain options (filament_*/__filament keys
    // are silently dropped by the overlay). String values match the
    // OrcaSlicer config-string convention used in project_settings.config.
    //
    // Reported back via ``settings_transfer.process_overrides_applied``.
    std::map<std::string, std::string> process_overrides;
```

- [ ] **Step 2: Parse the field in `parse_slice_request_from_stdin`**

In `cpp/src/json_io.cpp`, in `parse_slice_request_from_stdin` (lines 10–36), before the `return req;` add:

```cpp
    if (j.contains("process_overrides") && j["process_overrides"].is_object()) {
        for (const auto& [k, v] : j["process_overrides"].items()) {
            if (v.is_string()) {
                req.process_overrides.emplace(k, v.get<std::string>());
            }
            // Non-string values are silently dropped — the contract
            // requires stringified config values (matches project_settings.config).
        }
    }
```

- [ ] **Step 3: Build the binary**

```bash
# inside the dev shell:
cmake --build . --target orca-headless -j
```

Expected: builds clean. (No tests yet — just verify the C++ compiles.)

- [ ] **Step 4: Commit**

```bash
git add cpp/src/json_io.h cpp/src/json_io.cpp
git commit -m "cpp: extend SliceRequest with process_overrides map"
```

---

### Task 13: Apply the overlay in `slice_mode.cpp`

**Files:**
- Modify: `cpp/src/slice_mode.cpp`

- [ ] **Step 1: Locate the existing 3MF process-slot overlay**

In `cpp/src/slice_mode.cpp`, find the existing 3MF process overlay around line 527–538:

```cpp
    std::vector<std::string> process_override_keys;
    std::vector<std::string> printer_override_keys;
    if (const auto* fp = threemf_config.option<Slic3r::ConfigOptionStrings>(
            "different_settings_to_system", false);
        fp != nullptr && !fp->values.empty()) {
        process_override_keys = apply_threemf_slot_overrides(
            final_cfg, threemf_config, fp->values[0],
            /*exclude_filament_keys=*/true,
            /*excluded_keys=*/{});
        emit_progress("process_overrides_applied", 22);
        ...
```

The destination config the overlay writes into is `final_cfg` (a local `Slic3r::DynamicPrintConfig` constructed earlier in the function from `bundle.full_config()`). We will overlay our client values onto the same `final_cfg`.

- [ ] **Step 2: Hoist a report buffer and add the client-overrides overlay**

Near the top of `run_slice_mode` (or wherever local `nlohmann::json` reports for `settings_transfer` are declared — search for an existing `nlohmann::json` local that flows into `r.settings_transfer`), declare:

```cpp
    nlohmann::json process_overrides_report = nlohmann::json::array();
```

After the closing brace of the if-block that contains the 3MF process and per-filament overlays (search for the brace that closes the `if (... fp != nullptr && !fp->values.empty()) { ... }` block — it is also the brace right before the section that handles the printer slot or step 10), insert:

```cpp
    // 9b. Apply the client-supplied process_overrides on top of the
    //     resolved process config. Highest-priority overlay: these win
    //     over both the system process profile and the 3MF's authored
    //     customisations. Reuses ``final_cfg`` (same destination as the
    //     3MF overlay above) so downstream Print build sees the merged
    //     result.
    //
    // The "previous" snapshot is read BEFORE deserialize() so the response
    // can report what the value was immediately before the client override
    // took effect (3MF-customised value if the 3MF touched the key,
    // otherwise the resolved system default).
    if (!req.process_overrides.empty()) {
        for (const auto& [key, value_str] : req.process_overrides) {
            // Filter out filament-domain keys defensively (the iOS UI
            // shouldn't send them, but the same guard the 3MF overlay
            // applies belongs here too).
            auto starts_with_ = [](const std::string& s, const char* p) {
                const size_t n = std::strlen(p);
                return s.size() >= n && std::memcmp(s.data(), p, n) == 0;
            };
            auto ends_with_ = [](const std::string& s, const char* p) {
                const size_t n = std::strlen(p);
                return s.size() >= n &&
                    std::memcmp(s.data() + s.size() - n, p, n) == 0;
            };
            if (starts_with_(key, "filament_") || ends_with_(key, "_filament"))
                continue;

            Slic3r::ConfigOption* dst_opt = final_cfg.option(key, /*create=*/false);
            if (dst_opt == nullptr) continue;  // unknown key — silently drop

            // Snapshot the value before we overwrite it.
            std::string previous = dst_opt->serialize();

            // Deserialize the string into the option's typed slot. The
            // base ConfigOption interface (Config.hpp) declares
            //   bool deserialize(const std::string& str, bool append=false)
            // — no substitution context. Vector / percent / enum
            // encoding round-trip via each subclass's override.
            if (!dst_opt->deserialize(value_str)) {
                // Bad value for this option type — drop and move on.
                continue;
            }

            nlohmann::json entry;
            entry["key"]      = key;
            entry["value"]    = value_str;
            entry["previous"] = previous;
            process_overrides_report.push_back(std::move(entry));
        }
        emit_progress("client_overrides_applied", 23);
    }
```

(Adjust the `bundle.prints.get_edited_preset().config` access if the slice_mode.cpp file uses a different local reference — search for the variable name passed to `apply_threemf_slot_overrides` in the existing call and reuse it.)

- [ ] **Step 3: Surface the report in `settings_transfer`**

Locate where `r.settings_transfer` is populated near the end of `run_slice_mode` (search `r.settings_transfer` in the file — it is assembled before the function returns). Add this assignment alongside the existing keys:

```cpp
    r.settings_transfer["process_overrides_applied"] = process_overrides_report;
```

`process_overrides_report` was declared at function scope in Step 2 so it is visible here whether or not the request had any overrides (in which case it is the empty array).

- [ ] **Step 4: Build and run the existing slice integration tests**

```bash
# inside dev shell:
cmake --build . --target orca-headless -j

# from host:
docker exec orcaslicer-cli pytest tests/integration/test_slice_v2_fidelity.py -v
```

Expected: existing tests PASS — no behavioural regression when `process_overrides` is empty.

- [ ] **Step 5: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "cpp: apply process_overrides overlay after 3MF transfer"
```

---

### Task 14: Forward `process_overrides` from FastAPI to the binary

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add the field to `SliceTokenRequest`**

In `app/main.py`, locate `class SliceTokenRequest(BaseModel)` (line 1034). Add the field after `plate_type`:

```python
class SliceTokenRequest(BaseModel):
    input_token: str
    machine_id: str
    process_id: str
    filament_settings_ids: list[str]
    filament_map: list[int] | None = None
    plate_id: int = 1
    auto_center: bool = True
    plate_type: str | None = None
    # Stringified process-domain values overlaid AFTER the 3MF's transfer.
    # iOS / web sends e.g. {"layer_height": "0.16", "wall_loops": "3"}.
    # Server is permissive — the C++ side filters filament-domain keys
    # and silently drops unknown keys. See
    # docs/superpowers/specs/2026-05-06-process-parameter-editor-design.md.
    process_overrides: dict[str, str] | None = None
```

- [ ] **Step 2: Forward into the binary request**

Locate the `binary.slice(request={...})` call in `slice_v2` (around lines 1147–1161). Add the new key:

```python
        result = await binary.slice(request={
            "input_3mf": str(input_path),
            "output_3mf": str(output_path),
            "machine_chain_dir": paths["machine_chain_dir"],
            "process_chain_dir": paths["process_chain_dir"],
            "filament_chain_dir": paths["filament_chain_dir"],
            "machine_leaf_name": paths["machine_leaf_name"],
            "process_leaf_name": paths["process_leaf_name"],
            "plate_id": body.plate_id,
            "options": {"auto_center": body.auto_center},
            "filament_map": body.filament_map or [],
            "filament_settings_id": paths["filament_leaf_names"],
            "printer_model_id": paths.get("printer_model_id", ""),
            "plate_type": _resolve_plate_type_label(body.machine_id, body.plate_type),
            "process_overrides": body.process_overrides or {},
        })
```

- [ ] **Step 3: Do the same in `slice_stream_v2`**

Locate `slice_stream_v2` (around line 1190). Find its `binary.slice_stream(request=...)` call (around lines 1230–1240) and add the same `process_overrides` key in the same way.

- [ ] **Step 4: Commit**

```bash
git add app/main.py
git commit -m "main: forward process_overrides into the binary slice request"
```

---

### Task 15: End-to-end integration test

**Files:**
- Create: `tests/integration/test_process_overrides.py`

- [ ] **Step 1: Write the integration test**

`tests/integration/test_process_overrides.py`:

```python
"""End-to-end: client process_overrides overlay onto a real fixture slice.

Runs against the live container. Uses fixture 01 (single-filament benchy)
because its small and well-understood by the existing tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

API = "http://localhost:8070"

FIXTURE_INPUT = (
    Path(__file__).resolve().parents[2]
    / "_fixture" / "01"
    / "reference-benchy-orca-no-filament-custom-settings.3mf"
)


@pytest.mark.skipif(
    not FIXTURE_INPUT.exists(),
    reason=f"fixture missing: {FIXTURE_INPUT}",
)
def test_slice_v2_applies_process_override_layer_height() -> None:
    # 1. Upload the 3MF.
    with FIXTURE_INPUT.open("rb") as f:
        up = requests.post(
            f"{API}/3mf",
            files={"file": ("input.3mf", f, "application/octet-stream")},
            timeout=30,
        )
    up.raise_for_status()
    input_token = up.json()["token"]

    # 2. Pick reasonable defaults from /profiles/* — match the test_slice_v2_fidelity
    # pattern: A1 mini, 0.20mm Standard process, default PLA filament.
    machines = requests.get(f"{API}/profiles/machines", timeout=10).json()
    a1m = next(m for m in machines["machines"]
               if "A1 mini" in m["name"] and m.get("nozzle_diameter") == "0.4")

    processes = requests.get(f"{API}/profiles/processes", timeout=10).json()
    proc = next(p for p in processes["processes"]
                if p["name"].startswith("0.20mm Standard")
                and a1m["setting_id"] in p.get("compatible_printers", []))

    filaments = requests.get(
        f"{API}/profiles/filaments?ams_assignable=true", timeout=10).json()
    fil = next(f for f in filaments["filaments"]
               if f["filament_type"] == "PLA"
               and a1m["setting_id"] in f.get("compatible_printers", []))

    # 3. Slice with a process_overrides that changes layer_height.
    payload = {
        "input_token": input_token,
        "machine_id": a1m["setting_id"],
        "process_id": proc["setting_id"],
        "filament_settings_ids": [fil["setting_id"]],
        "process_overrides": {"layer_height": "0.16"},
    }
    sl = requests.post(f"{API}/slice/v2", json=payload, timeout=180)
    sl.raise_for_status()
    body = sl.json()

    # 4. The settings_transfer carries the process_overrides_applied report.
    applied = body["settings_transfer"]["process_overrides_applied"]
    assert isinstance(applied, list)
    assert any(e["key"] == "layer_height" and e["value"] == "0.16"
               for e in applied), applied
    # `previous` should be the resolved system default (not "0.16").
    layer_entry = next(e for e in applied if e["key"] == "layer_height")
    assert layer_entry["previous"] != "0.16"

    # 5. Sanity: the slice succeeded (download URL works).
    out_token = body["output_token"]
    dl = requests.get(f"{API}/3mf/{out_token}", timeout=30, stream=True)
    dl.raise_for_status()
    assert int(dl.headers.get("Content-Length", "0")) > 1000


@pytest.mark.skipif(
    not FIXTURE_INPUT.exists(),
    reason=f"fixture missing: {FIXTURE_INPUT}",
)
def test_slice_v2_omitting_process_overrides_is_a_noop() -> None:
    """Existing callers that don't pass process_overrides still work."""
    with FIXTURE_INPUT.open("rb") as f:
        up = requests.post(
            f"{API}/3mf",
            files={"file": ("input.3mf", f, "application/octet-stream")},
            timeout=30,
        )
    up.raise_for_status()
    input_token = up.json()["token"]

    machines = requests.get(f"{API}/profiles/machines", timeout=10).json()
    a1m = next(m for m in machines["machines"]
               if "A1 mini" in m["name"] and m.get("nozzle_diameter") == "0.4")
    processes = requests.get(f"{API}/profiles/processes", timeout=10).json()
    proc = next(p for p in processes["processes"]
                if p["name"].startswith("0.20mm Standard")
                and a1m["setting_id"] in p.get("compatible_printers", []))
    filaments = requests.get(
        f"{API}/profiles/filaments?ams_assignable=true", timeout=10).json()
    fil = next(f for f in filaments["filaments"]
               if f["filament_type"] == "PLA"
               and a1m["setting_id"] in f.get("compatible_printers", []))

    payload = {
        "input_token": input_token,
        "machine_id": a1m["setting_id"],
        "process_id": proc["setting_id"],
        "filament_settings_ids": [fil["setting_id"]],
        # No process_overrides at all.
    }
    sl = requests.post(f"{API}/slice/v2", json=payload, timeout=180)
    sl.raise_for_status()
    body = sl.json()

    # The new field should be present and empty.
    applied = body["settings_transfer"].get("process_overrides_applied", [])
    assert applied == []
```

- [ ] **Step 2: Run the integration test**

```bash
docker exec orcaslicer-cli pytest tests/integration/test_process_overrides.py -v
```

Expected: both tests PASS. If the test fails because the fixture name isn't found in `/profiles/processes` for the A1 mini, mirror what `tests/integration/test_slice_v2_fidelity.py` uses for the same fixture — the test is meant to consume the same setup as the existing fidelity test.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_process_overrides.py
git commit -m "tests: integration coverage for process_overrides slice overlay"
```

---

## Phase 6 — Drift checks + CI

### Task 16: `scripts/check_allowlist.py`

**Files:**
- Create: `scripts/check_allowlist.py`
- Create: `tests/test_check_allowlist.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_check_allowlist.py`:

```python
"""Tests for scripts.check_allowlist drift detection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_allowlist import check_drift


def _write_layout(path: Path, options_per_optgroup: list[list[str]]) -> None:
    pages = [{
        "label": "Quality",
        "optgroups": [
            {"label": f"og{i}", "options": opts}
            for i, opts in enumerate(options_per_optgroup)
        ],
    }]
    path.write_text(json.dumps({
        "extracted_from_path": "vendor/.../Tab.cpp",
        "extracted_from_sha": "0",
        "pages": pages,
    }))


def _write_allowlist(path: Path, options: list[str]) -> None:
    path.write_text(json.dumps({"revision": "test.1", "options": options}))


def _write_catalogue(path: Path, keys: list[str]) -> None:
    path.write_text(json.dumps({
        "options": [{"key": k, "label": k, "category": "Quality"} for k in keys],
    }))


def test_clean_state_returns_no_errors(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height", "wall_loops"]])
    _write_allowlist(allow, ["layer_height", "wall_loops"])
    _write_catalogue(cat, ["layer_height", "wall_loops"])
    errors = check_drift(layout, allow, cat)
    assert errors == []


def test_allowlist_key_missing_from_catalogue(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height"]])
    _write_allowlist(allow, ["layer_height", "typo_key"])
    _write_catalogue(cat, ["layer_height"])
    errors = check_drift(layout, allow, cat)
    assert any("typo_key" in e for e in errors)
    assert any("not in dump-options" in e for e in errors)


def test_allowlist_key_missing_from_layout(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height"]])
    _write_allowlist(allow, ["layer_height", "wall_loops"])
    _write_catalogue(cat, ["layer_height", "wall_loops"])
    errors = check_drift(layout, allow, cat)
    assert any("wall_loops" in e for e in errors)
    assert any("not surfaced in process_pages.json" in e for e in errors)


def test_layout_key_missing_from_catalogue(tmp_path: Path) -> None:
    layout = tmp_path / "process_pages.json"
    allow = tmp_path / "allowlist.json"
    cat = tmp_path / "options.json"
    _write_layout(layout, [["layer_height", "stale_key"]])
    _write_allowlist(allow, ["layer_height"])
    _write_catalogue(cat, ["layer_height"])
    errors = check_drift(layout, allow, cat)
    assert any("stale_key" in e for e in errors)
    assert any("Tab.cpp references" in e for e in errors)
```

- [ ] **Step 2: Run to confirm failure**

```bash
docker exec orcaslicer-cli pytest tests/test_check_allowlist.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the script**

`scripts/check_allowlist.py`:

```python
"""Detect drift between process_pages.json, process_allowlist.json, and dump-options.

Three drift modes detected:

1. Allowlist key not in dump-options output (typo, removed upstream, or
   filament/machine-domain leak).
2. Allowlist key in dump-options but not in process_pages.json (key
   exists in libslic3r but isn't surfaced in the GUI's process Tab —
   nowhere to render it).
3. process_pages.json references a key not in dump-options (Tab.cpp
   regex caught a stale reference, or upstream renamed the key).

Usage:
    python scripts/check_allowlist.py --check
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAYOUT = REPO_ROOT / "cpp" / "src" / "generated" / "process_pages.json"
DEFAULT_ALLOWLIST = REPO_ROOT / "app" / "process_allowlist.json"
DEFAULT_BINARY = os.environ.get(
    "ORCA_HEADLESS_BINARY", "/opt/orca-headless/bin/orca-headless")


def _layout_keys(layout_path: Path) -> set[str]:
    doc = json.loads(layout_path.read_text())
    return {
        k
        for p in doc.get("pages", [])
        for og in p.get("optgroups", [])
        for k in og.get("options", [])
    }


def _allowlist_keys(allowlist_path: Path) -> set[str]:
    return set(json.loads(allowlist_path.read_text()).get("options", []))


def _catalogue_keys(catalogue_path: Path) -> set[str]:
    doc = json.loads(catalogue_path.read_text())
    return {o["key"] for o in doc.get("options", [])}


def _generate_catalogue(binary_path: str, dest: Path) -> None:
    """Shell out to orca-headless dump-options, write the catalogue to dest."""
    proc = subprocess.run(
        [binary_path, "dump-options"],
        input=json.dumps({"out_path": str(dest)}).encode(),
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"orca-headless dump-options failed: rc={proc.returncode} "
            f"stderr={proc.stderr.decode(errors='replace')[-500:]}"
        )
    envelope = json.loads(proc.stdout)
    if envelope.get("status") != "ok":
        raise RuntimeError(f"dump-options error envelope: {envelope}")


def check_drift(
    layout_path: Path, allowlist_path: Path, catalogue_path: Path,
) -> list[str]:
    """Return a list of human-readable error messages; empty list = clean."""
    layout = _layout_keys(layout_path)
    allow  = _allowlist_keys(allowlist_path)
    cat    = _catalogue_keys(catalogue_path)
    errors: list[str] = []

    # Mode 1: allowlist key not in dump-options.
    for k in sorted(allow - cat):
        errors.append(
            f"allowlist key {k!r} is not in dump-options (typo, "
            f"removed upstream, or filament/machine-domain leak)"
        )

    # Mode 2: allowlist key in dump-options but not in pages.json.
    for k in sorted(allow & cat - layout):
        errors.append(
            f"allowlist key {k!r} is not surfaced in process_pages.json "
            f"(key exists in libslic3r but Tab.cpp doesn't expose it)"
        )

    # Mode 3: process_pages.json references a key not in dump-options.
    for k in sorted(layout - cat):
        errors.append(
            f"process_pages.json (Tab.cpp references) {k!r} but it is "
            f"not in dump-options — Tab.cpp may carry a stale reference"
        )

    return errors


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT)
    p.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    p.add_argument("--binary", default=DEFAULT_BINARY,
                   help="path to orca-headless (used to regenerate catalogue)")
    p.add_argument("--catalogue", type=Path,
                   help="reuse an existing catalogue JSON instead of running the binary")
    p.add_argument("--check", action="store_true",
                   help="exit non-zero on any drift (alias of default behaviour)")
    args = p.parse_args()

    if args.catalogue:
        catalogue_path = args.catalogue
    else:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            catalogue_path = Path(tf.name)
        try:
            _generate_catalogue(args.binary, catalogue_path)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    errors = check_drift(args.layout, args.allowlist, catalogue_path)
    if errors:
        for e in errors:
            print(f"drift: {e}", file=sys.stderr)
        return 1
    print("allowlist drift check: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

```bash
docker exec orcaslicer-cli pytest tests/test_check_allowlist.py -v
```

Expected: all four tests PASS.

- [ ] **Step 5: Run against the live tree**

```bash
docker exec orcaslicer-cli python scripts/check_allowlist.py
```

Expected: prints `allowlist drift check: clean` and exits 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_allowlist.py tests/test_check_allowlist.py
git commit -m "tools: add allowlist drift check script + tests"
```

---

### Task 17: Wire CI checks

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the drift check steps**

In `.github/workflows/ci.yml`, after the `Run unit tests` step (around line 50–53), add:

```yaml
      - name: Check Tab.cpp layout extraction is fresh
        run: docker exec orcaslicer-cli python scripts/extract_tab_layout.py --check

      - name: Check process allowlist drift
        run: docker exec orcaslicer-cli python scripts/check_allowlist.py
```

- [ ] **Step 2: Push the branch and watch CI** (skip if working locally only)

```bash
git push -u origin feat/process-parameter-editor
```

(Per project memory: don't push without explicit instruction. Skip this step unless the user has asked for it.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: gate on Tab.cpp extraction freshness and allowlist drift"
```

---

## Final verification

- [ ] **Step 1: Run the entire unit test suite**

```bash
docker exec orcaslicer-cli pytest tests/ -q --ignore=tests/integration
```

Expected: all PASS.

- [ ] **Step 2: Run the integration tests**

```bash
docker exec orcaslicer-cli pytest tests/integration/ -q
```

Expected: all PASS, including the new `test_process_overrides.py`.

- [ ] **Step 3: Smoke the new endpoints one more time**

```bash
docker exec orcaslicer-cli sh -c '
curl -sf http://localhost:8070/options/process | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"metadata:\", len(d[\"options\"]))"
curl -sf http://localhost:8070/options/process/layout | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"pages:\", [p[\"label\"] for p in d[\"pages\"]], \"rev:\", d[\"allowlist_revision\"])"
'
```

Expected: both endpoints return successfully.

- [ ] **Step 4: Bump the API revision**

In `app/config.py`, find `API_REVISION` and increment it (e.g., `37` → `38`). The full version string `{ORCA_VERSION}-{API_REVISION}` flows through the new payloads' `version` field, so iOS/web cache invalidation works on schema changes.

- [ ] **Step 5: Commit and pause for review**

```bash
git add app/config.py
git commit -m "config: bump API_REVISION for process editor endpoints"
```

Stop here. The branch is ready for the user to review or merge. Don't push without explicit instruction.
