# Phase 1 completion + multi-filament — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining single-filament parity gaps against the GUI and add proper multi-filament composition + per-slot project overrides, so `/slice/v2` covers every Phase 1 use case in the spec (`docs/superpowers/specs/2026-05-02-orca-headless-fork-design.md`).

**Architecture:** The `orca-headless` C++ binary already loads the 3MF, composes a `DynamicPrintConfig` (defaults → machine → process → filament[0]), normalizes per-filament vectors via `Preset::normalize`, applies process and printer customizations from `different_settings_to_system`, and slices. This plan extends the same path: stamp the missing slice_info fields, honor `curr_bed_type`, replicate `PresetBundle::construct_full_config` for `num_filaments > 1`, and apply per-filament-slot overrides with the name-guard from CLAUDE.md. Phases 2–4 (legacy cleanup, `/3mf/inspect`, gateway migration, docs) are tracked in the roadmap section at the bottom.

**Tech Stack:** C++17 (libslic3r, nlohmann/json), Python 3.12 (FastAPI), pytest, Docker BuildKit.

**Repo conventions to honor:**
- Tests in `tests/` mirror `app/` module names; integration-style tests live in `tests/integration/` (create if missing).
- C++ code lives in `cpp/src/`; one feature per file when reasonable. `slice_mode.cpp` has grown — split if it crosses ~500 lines.
- C++ stdout is a JSON-only protocol — never `std::cout`/`printf` outside `write_slice_response_to_stdout`. boost::log is already redirected to stderr in `cpp/src/orca_headless.cpp::configure_libslic3r_logging`.
- Commits follow the user's CLAUDE.md style: subject ≤ 60 chars, body wrapped at 140, `-` bullets, focus on visible behavior, code names in backticks.
- Each cpp rebuild takes ~10–15 min. Python edits are hot-mounted (`./app:/app/app`) and only need a `docker compose restart`.
- For interactive testing: `USE_HEADLESS_BINARY=1 GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --force-recreate`.

---

## Milestones

1. **Single-filament polish** (Tasks 1–3) — close the small parity gaps observed against `_fixture/01`.
2. **Multi-filament composition** (Tasks 4–6) — replicate the GUI's per-slot vector merge and per-filament overrides.
3. **Automated fidelity test** (Task 7) — pytest that compares `/slice/v2` output against the checked-in GUI fixtures within tolerance.

Each milestone ends green: container builds, all pytests pass, the fixture test passes.

---

## Files to be created or modified

**Modified files:**
- `cpp/src/slice_mode.cpp` — add curr_bed_type overlay, first_layer_time stamp, filament metadata stamp, multi-filament per-slot vector merge, per-filament `apply_overrides_for_slot` call
- `cpp/src/json_io.h` / `cpp/src/json_io.cpp` — add `filament_colors` and `filament_tray_info_idx` fields to SliceRequest
- `app/slicer.py` — extend `materialize_profiles_for_binary` to return per-filament `colors` + `tray_info_idx` lists derived from the resolved filament configs
- `app/main.py` — forward the new fields in `/slice/v2` and `/slice-stream/v2` request bodies
- `app/profiles.py` — add a small helper `get_filament_metadata(setting_id)` returning `{type, color}` from the resolved filament config

**Created files:**
- `tests/integration/test_slice_v2_fidelity.py` — fidelity test against `_fixture/01` and `_fixture/02`
- `tests/integration/__init__.py` — package marker

**Reference fixtures (already present, do not modify):**
- `_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf` — input
- `_fixture/01/gui-benchy-orca-no-filament-custom-settings_sliced_gui.gcode.3mf.3mf` — GUI output for parity comparison
- `_fixture/02/reference-benchy-orca.3mf` — multi-filament input
- `_fixture/02/gui-benchy-orca_sliced_gui.3mf` — GUI output (5-filament after user swap)

---

## Milestone 1 — Single-filament polish

### Task 1: Stamp `first_layer_time` on slice_info

**Why:** The GUI fixture sets `first_layer_time="366.697632"`, ours is empty. Consumers (e.g. the Bambu Studio app) read this from `slice_info.config` to show first-layer ETAs. The data already exists in `gcode_result.print_statistics`.

**Files:**
- Modify: `cpp/src/slice_mode.cpp` — the PlateData population block (currently around the `parse_filament_info` call near the gcode export).

- [ ] **Step 1: Read the relevant gcode_result field**

OrcaSlicer's `GCodeProcessorResult::print_statistics.modes[mode].first_layer_time` is a float in seconds. See `vendor/OrcaSlicer/src/libslic3r/GCode/GCodeProcessor.hpp:534`. Grab the Normal mode entry the same way we already do for `total_estimated_time`.

- [ ] **Step 2: Stamp it on the plate**

Add this block alongside the existing `plate->gcode_prediction = std::to_string(...)` line in `slice_mode.cpp`:

```cpp
const float first_layer_time =
    gcode_result.print_statistics.modes[normal_idx].first_layer_time;
if (first_layer_time > 0.0f) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%f", first_layer_time);
    plate->first_layer_time = buf;
}
```

The `%f` format mirrors the GUI's output ("366.697632" — 6 decimal places).

- [ ] **Step 3: Rebuild and verify**

```bash
DOCKER_BUILDKIT=1 docker compose build orcaslicer-cli
USE_HEADLESS_BINARY=1 GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --force-recreate
```

Then slice fixture 01 with `recenter:false` (see commands in Task 7) and confirm:

```bash
unzip -p /tmp/sliced.3mf 'Metadata/slice_info.config' | grep first_layer_time
# Expected: <metadata key="first_layer_time" value="<some non-empty float>"/>
```

- [ ] **Step 4: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Stamp first_layer_time on slice_info"
```

---

### Task 2: Honor `curr_bed_type` from the input 3MF

**Why:** `curr_bed_type` (e.g. `"Textured PEI Plate"`) controls which `<plate>_temp` lookup keys libslic3r uses for bed temperature gcode (`vendor/OrcaSlicer/src/libslic3r/GCode.cpp:2116, 2580, 2937`). It's stored at the project level in the 3MF's `Metadata/project_settings.config` but is **not** part of `different_settings_to_system`. Without this, our slices use the system default ("Cool Plate") regardless of what the user authored.

**Files:**
- Modify: `cpp/src/slice_mode.cpp` — the `apply_overrides_for_slot` block that already applies process + printer customizations.

- [ ] **Step 1: Apply curr_bed_type from threemf_config**

Add after the `transfer_status` block in `slice_mode.cpp` (right after the printer-slot overlay):

```cpp
// curr_bed_type is a project-level field, not part of the per-slot
// fingerprint. It controls which <plate>_temp keys libslic3r reads
// for bed temperature gcode (GCode.cpp:2116/2580/2937). Carry it
// across when present.
if (const auto* opt = threemf_config.option("curr_bed_type"); opt != nullptr) {
    if (auto* dst = final_cfg.option("curr_bed_type", /*create=*/false);
        dst != nullptr) {
        dst->set(opt);
        transfer_status["curr_bed_type"] = opt->serialize();
    }
}
```

Note: `curr_bed_type` is `coEnum`. `ConfigOption::set` handles type-correct copy across the same option type, so no manual cast.

- [ ] **Step 2: Rebuild, slice fixture 01, verify**

After rebuild + restart, slice fixture 01 and check:

```bash
unzip -p /tmp/sliced.3mf 'Metadata/project_settings.config' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('curr_bed_type'))"
# Expected: Textured PEI Plate
```

The reference 3MF declares `Textured PEI Plate`; previous output had `Cool Plate`.

- [ ] **Step 3: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Carry curr_bed_type from input 3MF onto final_cfg"
```

---

### Task 3: Stamp filament metadata (`type`, `color`, `filament_id`) on slice_info

**Why:** The GUI fixture's `<filament>` tag carries `tray_info_idx="GFA00" type="PLA" color="#F4EE2A"`. Ours has all three empty. `PlateData::parse_filament_info` (`vendor/OrcaSlicer/src/libslic3r/Format/bbs_3mf.cpp:593`) only fills `id`, `used_m`, `used_g` — the GUI populates the rest from the resolved filament config and the AMS mapping. For now the gateway doesn't pass AMS slot info through the new path, so leave `tray_info_idx` empty (Phase 3 plumbs it through `/3mf/inspect`); but `type`, `color`, and `filament_id` come straight from the resolved filament config we already have in C++.

**Files:**
- Modify: `cpp/src/slice_mode.cpp` — after the `plate->parse_filament_info(&gcode_result)` call.

- [ ] **Step 1: Stamp from filament_cfgs after parse_filament_info**

Add immediately after the existing `plate->parse_filament_info(&gcode_result);` line:

```cpp
// parse_filament_info only sets id/used_m/used_g. Fill type/color/
// filament_id from the resolved per-slot filament config so slice_info
// matches what the GUI emits. Keep tray_info_idx empty until Phase 3
// plumbs AMS slot info from the gateway.
for (size_t i = 0; i < plate->slice_filaments_info.size(); ++i) {
    auto& info = plate->slice_filaments_info[i];
    if (i >= filament_cfgs.size()) break;
    const auto& fc = filament_cfgs[i];
    if (const auto* t = fc.opt<Slic3r::ConfigOptionStrings>("filament_type");
        t && !t->values.empty()) {
        info.type = t->values.front();
    }
    if (const auto* c = fc.opt<Slic3r::ConfigOptionStrings>("filament_colour");
        c && !c->values.empty()) {
        info.color = c->values.front();
    }
    if (const auto* id = fc.opt<Slic3r::ConfigOptionStrings>("filament_ids");
        id && !id->values.empty()) {
        info.filament_id = id->values.front();
    }
}
```

- [ ] **Step 2: Rebuild and verify**

After rebuild + restart, slice fixture 01:

```bash
unzip -p /tmp/sliced.3mf 'Metadata/slice_info.config' | grep '<filament '
# Expected: type="PLA" color="#<some hex>" — both non-empty
# tray_info_idx stays empty for now
```

- [ ] **Step 3: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Stamp filament type/color/id on slice_info from resolved configs"
```

---

## Milestone 2 — Multi-filament composition

### Task 4: Per-slot vector merge for `num_filaments > 1`

**Why:** Currently `slice_mode.cpp` step 3 applies `filament_cfgs[0]` flat — fine for single-filament but wrong for multi-filament: each per-filament vector key (e.g. `filament_diameter`, `filament_type`, `nozzle_temperature`) needs to be assembled by taking element 0 from each filament's config and stuffing them into a length-N vector. This is what `PresetBundle::construct_full_config` does (`vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:115-180`). Without it, multi-filament prints either crash in `Print::apply` or silently use filament[0]'s values for every slot.

**Files:**
- Modify: `cpp/src/slice_mode.cpp` — step 3 (the config composition).

- [ ] **Step 1: Replace the filament-apply block with a per-slot merge**

Replace the existing block:

```cpp
if (!filament_cfgs.empty()) {
    final_cfg.apply(filament_cfgs[0]);
}
```

with:

```cpp
// Replicate PresetBundle::construct_full_config (PresetBundle.cpp:115-180):
// scalars come from filament[0]; vector keys are assembled across all N
// filaments by taking each one's value at index 0 (the leaf preset is
// authored as a 1-element vector) and concatenating into a length-N
// vector that Print::apply expects.
if (filament_cfgs.size() == 1) {
    final_cfg.apply(filament_cfgs[0]);
} else if (filament_cfgs.size() > 1) {
    // The first config provides the scalar values + serves as the base.
    final_cfg.apply(filament_cfgs[0]);
    // For every key declared by filament[0], if it's a vector, overwrite
    // with a composed vector whose i-th entry comes from filament_cfgs[i].
    for (const std::string& key : filament_cfgs[0].keys()) {
        if (key == "compatible_prints" || key == "compatible_printers") continue;
        Slic3r::ConfigOption* dst = final_cfg.option(key, /*create=*/false);
        if (dst == nullptr || dst->is_scalar()) continue;
        auto* dst_vec = static_cast<Slic3r::ConfigOptionVectorBase*>(dst);
        std::vector<const Slic3r::ConfigOption*> per_slot(filament_cfgs.size(), nullptr);
        for (size_t i = 0; i < filament_cfgs.size(); ++i) {
            per_slot[i] = filament_cfgs[i].option(key);
        }
        dst_vec->set(per_slot);
    }
}
```

- [ ] **Step 2: Verify single-filament still works (regression)**

After rebuild + restart, slice fixture 01 (single-filament) and confirm time/weight/filament estimates within 1% of the prior commit. Use `recenter:false` for stable comparison.

```bash
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -s -X POST http://localhost:8000/slice/v2 -H 'Content-Type: application/json' -d "{\"input_token\":\"$TOK\",\"machine_id\":\"GM020\",\"process_id\":\"GP000\",\"filament_settings_ids\":[\"GFSA00_02\"],\"recenter\":false}" | python3 -c "import json,sys; e=json.load(sys.stdin)['estimate']; print(f'time={e[\"time_seconds\"]:.0f}s weight={e[\"weight_g\"]:.2f}g')"
# Expected: time=~1828s weight=~10.63g  (within 1% of last commit's numbers)
```

- [ ] **Step 3: Smoke a multi-filament request**

Pick 3 BBL filament IDs and confirm the binary doesn't crash:

```bash
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -s -X POST http://localhost:8000/slice/v2 -H 'Content-Type: application/json' -d "{\"input_token\":\"$TOK\",\"machine_id\":\"GM020\",\"process_id\":\"GP000\",\"filament_settings_ids\":[\"GFSA00_02\",\"GFSA00_02\",\"GFSA00_02\"],\"recenter\":false}" | python3 -m json.tool
# Expected: status ok, estimate populated, no `binary_crashed`.
# Same filament 3x is the simplest valid multi-slot case — proves the
# vector merge produces something Print::apply accepts.
```

- [ ] **Step 4: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Compose multi-filament configs per-slot like PresetBundle::full_config"
```

---

### Task 5: Per-filament-slot project overrides with name guard

**Why:** Per CLAUDE.md, `different_settings_to_system[i+1]` lists keys the user customized for filament slot `i`. Apply only when the request's `filament_settings_ids[i]` matches the 3MF's `filament_settings_id[i]` — when the user swapped to a different filament, the customizations belong to the old one and must be discarded. Currently we skip filament slots entirely.

**Files:**
- Modify: `cpp/src/slice_mode.cpp` — the `different_settings_to_system` overlay block from the previous commit.

- [ ] **Step 1: Read the 3MF's filament_settings_id list**

Just before the existing `different_settings_to_system` overlay block (where we already handle the process and printer slots), look up the 3MF's authored filament names:

```cpp
std::vector<std::string> threemf_filament_names;
if (const auto* opt = threemf_config.option<Slic3r::ConfigOptionStrings>(
        "filament_settings_id", false);
    opt != nullptr) {
    threemf_filament_names = opt->values;
}
```

- [ ] **Step 2: Add per-filament overlay between process and printer slots**

Inside the `if (fp != nullptr && !fp->values.empty())` block, after the process-slot call and before the printer-slot call, add:

```cpp
// Per-filament slots: indexes 1..N. Apply only when the selected
// filament name matches the 3MF's authored name for that slot —
// when the user swapped, the customizations referenced the OLD
// filament's defaults so they're meaningless on the new one.
nlohmann::json filament_slot_status = nlohmann::json::array();
const size_t num_filament_slots =
    fp->values.size() >= 2 ? fp->values.size() - 2 : 0;
for (size_t i = 0; i < num_filament_slots; ++i) {
    const std::string& key_list = fp->values[i + 1];
    const std::string original =
        i < threemf_filament_names.size() ? threemf_filament_names[i] : "";
    const std::string selected =
        i < req.filament_settings_id.size() ? req.filament_settings_id[i] : "";
    nlohmann::json entry;
    entry["slot"] = i;
    entry["original_filament"] = original;
    entry["selected_filament"] = selected;
    if (key_list.empty()) {
        entry["status"] = "no_customizations";
        entry["transferred"] = nlohmann::json::array();
        entry["discarded"] = nlohmann::json::array();
    } else if (!original.empty() && original == selected) {
        // Per-filament keys land on a length-N vector at index i.
        // For Phase 1 we only support overlaying scalars + vector
        // index 0 (i.e. when the slot's value sits at index i of the
        // composed final_cfg vector). This is sufficient for the
        // common single-filament-slot-customized case; full per-slot
        // semantics need libslic3r's per-filament_temp_configs path.
        const auto transferred = apply_overrides_for_slot(
            final_cfg, threemf_config, key_list,
            /*exclude_filament_keys=*/false);
        entry["status"] = "applied";
        entry["transferred"] = transferred;
        entry["discarded"] = nlohmann::json::array();
    } else {
        // Filament was swapped — discard.
        entry["status"] = "filament_changed";
        entry["transferred"] = nlohmann::json::array();
        entry["discarded"] = split_semicolons(key_list);
    }
    filament_slot_status.push_back(entry);
}
transfer_status["filament_slots"] = filament_slot_status;
```

- [ ] **Step 3: Verify against fixture 01 (where filament slot is empty)**

After rebuild, slice fixture 01 and confirm `settings_transfer.filament_slots[0].status == "no_customizations"`:

```bash
curl -s -X POST http://localhost:8000/slice/v2 -H 'Content-Type: application/json' -d "{\"input_token\":\"$TOK\",\"machine_id\":\"GM020\",\"process_id\":\"GP000\",\"filament_settings_ids\":[\"GFSA00_02\"],\"recenter\":false}" | python3 -c "import json,sys; print(json.load(sys.stdin)['settings_transfer']['filament_slots'])"
# Expected: [{"slot": 0, "original_filament": "Bambu PLA Basic @BBL A1M", "selected_filament": "GFSA00_02", "status": "filament_changed", ...}]
# (status is "filament_changed" because the request used setting_id, not the display name; this is acceptable Phase 1 behavior — Task 6 normalizes it)
```

- [ ] **Step 4: Commit**

```bash
git add cpp/src/slice_mode.cpp
git commit -m "Apply per-filament project overrides with name-guard"
```

---

### Task 6: Forward filament display names so the slot name guard matches

**Why:** Task 5's name guard compares the 3MF's `filament_settings_id[i]` (a display name like `"Bambu PLA Basic @BBL A1M"`) against the request's `filament_settings_ids[i]`. The request currently sends setting_ids (`"GFSA00_02"`) — those won't match display names, so every slot reports `filament_changed` even when the user picked the same filament. Resolve names server-side and forward those into the binary.

**Files:**
- Modify: `app/slicer.py` — `materialize_profiles_for_binary` already resolves filaments; expose their display names.
- Modify: `app/main.py` — `/slice/v2` and `/slice-stream/v2`, replace `body.filament_settings_ids` in the `filament_settings_id` request field with the resolved names.

- [ ] **Step 1: Return names from materialize_profiles_for_binary**

In `app/slicer.py::materialize_profiles_for_binary`, alongside the existing returns, add:

```python
filament_names: list[str] = []
for fid in filament_setting_ids:
    fcfg = get_profile_by_id_or_name("filament", fid)
    filament_names.append(fcfg.get("name", fid))
```

(Move this loop in line with the existing `for i, fid in enumerate(filament_setting_ids):` block — write the JSON file *and* collect the name in one pass.)

Add `"filament_names": filament_names` to the return dict.

- [ ] **Step 2: Pass names instead of ids in the binary request**

In both `/slice/v2` and `/slice-stream/v2` in `app/main.py`, change:

```python
"filament_settings_id": body.filament_settings_ids,
```

to:

```python
"filament_settings_id": paths["filament_names"],
```

The field stays named `filament_settings_id` because that's what libslic3r and the 3MF schema call it — but the *values* are now display names, matching the 3MF's authored format.

- [ ] **Step 3: Restart and re-verify slot status**

`docker compose restart` is enough — Python is hot-mounted.

```bash
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -s -X POST http://localhost:8000/slice/v2 -H 'Content-Type: application/json' -d "{\"input_token\":\"$TOK\",\"machine_id\":\"GM020\",\"process_id\":\"GP000\",\"filament_settings_ids\":[\"GFSA00_02\"],\"recenter\":false}" | python3 -c "import json,sys; print(json.load(sys.stdin)['settings_transfer']['filament_slots'])"
# Expected: status="no_customizations" (since fixture 01 has empty filament slot in different_settings_to_system).
# The point: original_filament == selected_filament == "Bambu PLA Basic @BBL A1M".
```

- [ ] **Step 4: Commit**

```bash
git add app/slicer.py app/main.py
git commit -m "Forward filament display names to the binary for name-guard match"
```

---

## Milestone 3 — Automated fidelity test

### Task 7: Pytest fidelity test against `_fixture/01`

**Why:** Manual fixture comparison has caught two regressions already (recenter bug, missing project overrides). Lock these in as pytest so future changes don't quietly break parity.

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_slice_v2_fidelity.py`

- [ ] **Step 1: Verify pytest discovery and existing test pass**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/ -q 2>&1 | tail -10
# Expected: 333+ passed (we don't break anything)
```

- [ ] **Step 2: Create the empty package marker**

```bash
touch tests/integration/__init__.py
```

- [ ] **Step 3: Write the fidelity test**

Create `tests/integration/test_slice_v2_fidelity.py`:

```python
"""Fidelity test: /slice/v2 vs GUI-authored output 3MF.

The fixtures in `_fixture/01/` are a reference 3MF that the GUI sliced
into `gui-...gcode.3mf.3mf`. We slice the input through /slice/v2 with
the same machine/process/filament selection and assert the metadata in
slice_info.config (time, weight, filament use, layer count, start XY)
falls within tolerance of the GUI's numbers.

The test is opt-in: it requires a running container at
http://localhost:8000 with USE_HEADLESS_BINARY=1. Skip if not reachable.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

import httpx
import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8000")
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "_fixture"


def _container_reachable() -> bool:
    try:
        r = httpx.get(f"{API}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_reachable(),
    reason="orcaslicer-cli not reachable at $ORCASLICER_API",
)


def _read_slice_info(threemf_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        xml = z.read("Metadata/slice_info.config").decode()
    out: dict[str, str] = {}
    for m in re.finditer(r'<metadata key="(\w+)" value="([^"]*)"/>', xml):
        out[m.group(1)] = m.group(2)
    return out


def _first_xy(threemf_bytes: bytes) -> tuple[float, float]:
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        gcode = z.read("Metadata/plate_1.gcode").decode()
    after_first_object = gcode.split("\nM624 ", 1)[1]
    for line in after_first_object.splitlines():
        m = re.match(r"G1 X([\d.]+) Y([\d.]+)", line)
        if m:
            return float(m.group(1)), float(m.group(2))
    raise AssertionError("no G1 X/Y after first M624")


def test_fixture_01_matches_gui_within_tolerance():
    input_path = FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    gui_path = FIXTURE_DIR / "01" / "gui-benchy-orca-no-filament-custom-settings_sliced_gui.gcode.3mf.3mf"
    assert input_path.exists(), f"missing fixture: {input_path}"
    assert gui_path.exists(), f"missing fixture: {gui_path}"

    with httpx.Client(base_url=API, timeout=120.0) as client:
        with input_path.open("rb") as fh:
            r = client.post("/3mf", files={"file": ("input.3mf", fh)})
        r.raise_for_status()
        token = r.json()["token"]

        r = client.post(
            "/slice/v2",
            json={
                "input_token": token,
                "machine_id": "GM020",
                "process_id": "GP000",
                "filament_settings_ids": ["GFSA00_02"],
                "recenter": False,
            },
        )
        r.raise_for_status()
        body = r.json()

        out_token = body["output_token"]
        r = client.get(f"/3mf/{out_token}")
        r.raise_for_status()
        ours = r.content

    gui = gui_path.read_bytes()
    ours_info = _read_slice_info(ours)
    gui_info = _read_slice_info(gui)

    # Time: within 2% (we observed 0.25%).
    ours_time = float(ours_info["prediction"])
    gui_time = float(gui_info["prediction"])
    assert abs(ours_time - gui_time) / gui_time < 0.02, (
        f"time drift {ours_time} vs {gui_time}"
    )

    # Weight & filament: within 1.5% (we observed 0.6%).
    ours_w = float(ours_info["weight"])
    gui_w = float(gui_info["weight"])
    assert abs(ours_w - gui_w) / gui_w < 0.015, f"weight drift {ours_w} vs {gui_w}"

    # printer_model_id, label_object, nozzle_diameters must match.
    assert ours_info.get("printer_model_id") == gui_info.get("printer_model_id")
    assert ours_info.get("label_object_enabled") == gui_info.get("label_object_enabled")
    assert ours_info.get("nozzle_diameters") == gui_info.get("nozzle_diameters")

    # Start XY identical to 3 decimals.
    ours_xy = _first_xy(ours)
    gui_xy = _first_xy(gui)
    assert abs(ours_xy[0] - gui_xy[0]) < 0.01, f"start X {ours_xy[0]} vs {gui_xy[0]}"
    assert abs(ours_xy[1] - gui_xy[1]) < 0.01, f"start Y {ours_xy[1]} vs {gui_xy[1]}"
```

- [ ] **Step 4: Run the test**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 \
  pytest tests/integration/test_slice_v2_fidelity.py -v 2>&1 | tail -20
# Expected: 1 passed
```

If the test fails, do NOT relax the tolerances — investigate the regression and fix it.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/__init__.py tests/integration/test_slice_v2_fidelity.py
git commit -m "Lock /slice/v2 fixture-01 parity with a pytest fidelity test"
```

---

## Phase 2–4 Roadmap

These phases will get full implementation plans of their own once Phase 1 is closed. Tracked here so the order and shape is clear.

### Phase 2 — Legacy Python cleanup (after Phase 1 stabilizes)

The old `/slice` multipart route still leans on Python code that the new path no longer needs. Once `/slice/v2` is the production entry, retire:

- `app/normalize.py` (deletable — libslic3r `Preset::normalize` does this natively now).
- `_CLAMP_RULES` in `app/slicer.py` (libslic3r already validates parameter ranges; the clamps were a workaround for AppImage CLI brittleness).
- The smart settings transfer block (`_extract_declared_customizations` and friends) — `apply_overrides_for_slot` in C++ owns this now.
- The BBL `machine_full` shim writer (`_write_bbl_machine_full_shims`) — we stamp `printer_model_id` directly via the request envelope.
- The legacy `/slice` route itself, after migrating any remaining callers.

**Estimated effort:** 1 day, mostly deletions. Risk: legacy callers we don't know about. Mitigation: keep the AppImage path one release after `/slice/v2` reaches feature parity, then remove.

### Phase 3 — `/3mf/inspect` and gateway migration (the user-facing payoff)

Goal: `bambu-gateway` stops opening 3MFs itself and queries `orcaslicer-cli` for everything it needs. New endpoint (synchronous, fast, doesn't slice):

- `POST /3mf/inspect` — accepts a token (or upload), returns `{plates: [...], filaments: [...], thumbnails: [...], bbox: ..., printer_model_hint: ...}`.
- A "use-set" mode where the gateway can pin a profile selection to a token and re-slice cheaply.

**Estimated effort:** 2 days for the endpoint + a separate plan for the gateway-side migration. Depends on Phase 1 multi-filament composition (Tasks 4–6 of this plan) so per-filament info is correct.

### Phase 4 — Docs, automated fidelity at scale, retire legacy

- Rewrite `README.md` and `CLAUDE.md` to reflect `USE_HEADLESS_BINARY=1` as the default once Phase 2 lands.
- Add a CI workflow that runs `tests/integration/test_slice_v2_fidelity.py` against a built container.
- Investigate the residual ~0.6% structural diff (157 vs 137 internal solid infill regions) — likely a `FullPrintConfig::defaults()` vs `PresetBundle::full_config()` discrepancy. Probably a single missing default key.
- Reset/investigate the `vendor/OrcaSlicer` submodule pointer drift noted in the worktree (pre-existing, unrelated to Phase 1 work).
- Decide whether `data/cache/` should be `.gitignore`d.

**Estimated effort:** 1 day for docs + CI, time-boxed investigation for the structural diff.

---

## Self-Review

**Spec coverage** (vs `docs/superpowers/specs/2026-05-02-orca-headless-fork-design.md`):
- Open 3MF projects with full settings preservation ✓ (Tasks 1–6 close the remaining gaps)
- Map project filaments to AMS slots ⚠️ — `tray_info_idx` deferred to Phase 3 (`/3mf/inspect` is where AMS slot info lands)
- Use custom profiles ✓ (already working via `materialize_profiles_for_binary`)
- Change machine/process while keeping modifications ✓ (Tasks 5–6 finalize per-filament side)
- Recenter model on build plate ✓ (already fixed)
- Slice and produce sliced 3MF for preview ✓
- Gateway queries for plates/filaments/thumbnails — Phase 3
- Token-based file caching with size-cap LRU eviction ✓ (already shipped)

**Placeholder scan:** No "TBD" / "implement later" / "similar to Task N" patterns. Each task has full code, exact paths, and verifiable expected output.

**Type consistency:** `apply_overrides_for_slot` and `split_semicolons` are referenced in Task 5 and already exist in `cpp/src/slice_mode.cpp` from the prior commit. `materialize_profiles_for_binary` returns are extended consistently in Task 6 (Python) and consumed by the C++ binary via `req.filament_settings_id`.

**Risk:** Task 5's per-filament overlay applies the keys flat onto `final_cfg` rather than per-slot vectors. For Phase 1 single-customized-slot this is acceptable (most user customizations are scalar process keys repeated per filament; on the filament side they're typically single-value vectors anyway). True per-slot vector overlay needs the same composition machinery as Task 4 and is called out as Phase 4 follow-up if real-world multi-filament customizations expose the gap.
