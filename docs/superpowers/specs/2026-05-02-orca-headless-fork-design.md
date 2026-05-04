# Design: `orca-headless` binary fork and bambu-gateway migration

**Date:** 2026-05-02
**Branch:** `feat/orca-headless` (both `orcaslicer-cli` and `bambu-gateway`)
**Status:** Approved design — pending implementation plan

## Problem

The current architecture has two compounding problems:

1. **OrcaSlicer's CLI diverges from the GUI** for project loading, preset normalization, and settings transfer. We've been working around this with Python code in `orcaslicer-cli` (`app/normalize.py`, `_CLAMP_RULES`, smart settings transfer in `app/slicer.py`, BBL `machine_full` shim writer in `app/profiles.py`, plate-rebuilding in `app/threemf.py`). Each workaround re-implements logic that already exists inside OrcaSlicer's GUI codepath.
2. **`bambu-gateway` has its own 3MF parser** (`app/parse_3mf.py`, `app/print_estimate.py`, plate/thumbnail/filament logic in `app/slice_jobs.py` and `app/filament_selection.py`). This duplicates parsing already done in `orcaslicer-cli` and drifts from OrcaSlicer's 3MF format as it evolves.

## Goal

Replace the AppImage `orca-slicer` binary with a small purpose-built C++ binary (`orca-headless`) that links `libslic3r` directly and mirrors the GUI's project-loading path. Migrate all 3MF-reading logic out of `bambu-gateway` into `orcaslicer-cli`'s HTTP API. Gateway should never open a `.3mf` ZIP again.

## Non-goals

- Replacing the existing OrcaSlicer CLI upstream — we maintain a private fork.
- Long-running binary daemon — subprocess-per-call is sufficient given the token cache amortizes inspect cold-starts and slice cold-start is noise relative to slice time.
- Removing AMS/printer-protocol logic from gateway — FTPS, MQTT, AMS tray validation stay there. Gateway's role becomes printer-comms + UI orchestration.
- Migrating off Python in `orcaslicer-cli` — Python remains the API surface; only the underlying slicing binary changes.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  bambu-gateway  (no 3MF parsing; no ZIP open; no XML reads)      │
│  - HTTP client to orcaslicer-cli                                 │
│  - AMS tray selection / printer LAN comms (FTPS, MQTT)           │
│  - Web UI                                                        │
└──────────────────────────────────────────────────────────────────┘
                            │ HTTP (token-based)
┌──────────────────────────────────────────────────────────────────┐
│  orcaslicer-cli  (this repo)                                     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Python FastAPI service (app/)                           │    │
│  │  - Upload + token cache (SHA-keyed, LRU, size-capped)    │    │
│  │  - Cheap ZIP/XML reads: plate metadata, thumbnails,      │    │
│  │    slice_info.config estimate, sliced-state detection    │    │
│  │  - Profiles index + custom user filament storage         │    │
│  │  - Subprocess invocation of orca-headless                │    │
│  │  - SSE streaming of slice progress                       │    │
│  └──────────────────────────────────────────────────────────┘    │
│                            │ subprocess + JSON stdio              │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  orca-headless  (new C++ binary, ~500–1000 LOC)          │    │
│  │  - Links libslic3r (vendored at OrcaSlicer 2.3.2 tag)    │    │
│  │  - Mirrors GUI's load-3mf-as-project path                │    │
│  │  - Modes: --slice, --use-set                             │    │
│  │  - Progress events on stderr (line-delimited JSON)       │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

### Key invariants

- Gateway never opens a `.3mf` ZIP or parses 3MF XML.
- Python service is the API surface. Binary is invoked per-call (no long-running daemon).
- Binary's preset-loading mirrors the GUI's path (not the existing CLI's). This is the core fidelity bet.
- `libslic3r` is vendored as a git submodule pinned to OrcaSlicer 2.3.2; rebases are explicit version bumps, not implicit drift.
- The GUI is our reference implementation, not OrcaSlicer's existing CLI.

### Net code movement

- **Deleted from bambu-gateway:** `app/parse_3mf.py`, `app/print_estimate.py`, plate/thumbnail parsing in `app/slice_jobs.py`, filament-padding in `app/filament_selection.py`. Replaced with a thin `OrcaslicerClient`.
- **Deleted from orcaslicer-cli (Python side):** `app/normalize.py` entirely (libslic3r normalizes natively), `_CLAMP_RULES` in `app/slicer.py`, BBL `machine_full` shim writer in `app/profiles.py`, the smart settings-transfer logic in `app/slicer.py`, most of `app/threemf.py` (libslic3r handles plate extraction).
- **Survives in Python:** profile loading/inheritance resolution, endpoint routing, custom filament import, token cache, cheap ZIP reads.
- **New in C++:** `orca-headless` binary (~500–1000 LOC).
- **Estimated totals:** ~1500 LOC deleted across both repos, ~500–1000 LOC C++ added, ~300–500 LOC Python added.

## Components

### `orca-headless` binary

Subprocess invocation, JSON over stdio. Two modes.

#### Mode `slice`

```
$ orca-headless slice < request.json > response.json   2> events.ndjson
```

**Request (stdin):**

```json
{
  "input_3mf": "/cache/3mf/<sha>.3mf",
  "output_3mf": "/cache/sliced/<job-id>.3mf",
  "machine_profile": "/tmp/profiles/<job>/machine.json",
  "process_profile": "/tmp/profiles/<job>/process.json",
  "filament_profiles": ["/tmp/profiles/<job>/filament-0.json", "..."],
  "plate_id": 1,
  "options": { "recenter": true, "save_thumbnails": true }
}
```

**Response (stdout, success):**

```json
{
  "status": "ok",
  "output_3mf": "/cache/sliced/<job-id>.3mf",
  "estimate": {
    "time_seconds": 1234,
    "weight_g": 12.3,
    "filament_used_m": [1.2, 0.0, 3.4]
  },
  "settings_transfer": { "...": "same shape we emit today" }
}
```

Thumbnails are embedded in the output 3MF via `store_bbs_3mf`'s thumbnail params. The binary does not write separate thumbnail files; Python extracts them from the cached output 3MF on demand when the `/thumbnail` endpoint is hit.

**Progress events (stderr, line-delimited JSON):**

```json
{"phase": "loading_3mf", "percent": 0}
{"phase": "applying_overrides", "percent": 5}
{"phase": "slicing", "percent": 30}
{"phase": "exporting", "percent": 95}
{"phase": "done", "percent": 100}
```

**Error response (stdout, exit non-zero):**

```json
{"status": "error", "code": "build_volume_exceeded", "message": "...", "details": {"...": "..."}}
```

#### Mode `use-set`

```
$ orca-headless use-set < request.json > response.json
```

**Request:** `{"input_3mf": "/cache/3mf/<sha>.3mf"}`

**Response:**

```json
{
  "plates": [
    {"id": 1, "used_filament_indices": [0, 2]},
    {"id": 2, "used_filament_indices": [0]}
  ]
}
```

This mode runs `Model::read_from_file` and walks `paint_color` attributes only — does not load presets or slice. Cheap (~200ms–2s depending on mesh size).

#### Internal structure

The slice mode mirrors the GUI's call path:

```cpp
// 1. LOAD — same as existing CLI
Model model = Model::read_from_file(input_3mf, &config, &subs,
    LoadStrategy::LoadModel | LoadStrategy::LoadConfig | LoadStrategy::LoadAuxiliary,
    &plate_data, &project_presets, ...);

// 2. PRESETS — the part the existing CLI gets wrong
PresetBundle bundle;
bundle.load_presets(...);
bundle.load_selections(app_config, requested_machine_id);
bundle.apply_project_overrides(project_presets, config);
    // ← NEW: overlay different_settings_to_system onto bundle's selected presets
    // ← Mirrors what Plater::priv::load_files + Tab::on_preset_loaded do

// 3. AMS MAPPING — flat config key, no GUI needed
config.set("filament_map", ams_slot_assignments);
config.set("filament_settings_id", chosen_filament_ids);

// 4. RECENTER
model.center_instances_around_point(plate_center);

// 5. SLICE
Print print;
print.apply(model, config);
print.process();

// 6. EXPORT
StoreParams sp{ output_3mf, plate_data, project_presets,
                thumbnail, top_thumb, no_light_thumb,
                SaveStrategy::WithGcode };
Slic3r::store_bbs_3mf(sp);
```

### Python service

#### New endpoints

```
POST   /3mf                              multipart .3mf upload
       → { token, sha256, size, evicts: [token, ...] }

GET    /3mf/{token}/inspect              cached
       → { is_sliced, plates: [...], filaments: [...],
           use_set_per_plate: { "1": [0, 2], "2": [0] },
           estimate: {...} | null,
           thumbnail_urls: ["/3mf/{token}/plates/1/thumbnail", ...] }

GET    /3mf/{token}/plates/{n}/thumbnail → image/png
GET    /3mf/{token}                      → application/vnd.ms-package.3dmanufacturing-3dmodel+xml
                                           (raw 3MF bytes — works for any cached 3MF)
DELETE /3mf/{token}                      → 204
DELETE /3mf/cache                        → { evicted: N, freed_bytes: B }
GET    /3mf/cache/stats                  → { count, total_bytes, max_bytes, max_files }

POST   /slice                            JSON body referencing input token
       → 200 application/json
         { output_token, output_sha256, input_token,
           estimate, thumbnail_urls, download_url, settings_transfer }

POST   /slice-stream                     JSON body, SSE
       events: { type: "progress", phase, percent }
               { type: "result",   <same payload as POST /slice 200 body> }
               { type: "error",    code, message }
```

#### Preserved endpoints

```
GET    /profiles/{machines,processes,filaments}     unchanged
POST   /profiles/reload                             unchanged
POST   /profiles/filaments/import                   unchanged
POST   /profiles/filaments/resolve-import           unchanged
GET    /health                                      unchanged
```

#### Legacy back-compat

`POST /slice` with multipart body (current shape) is preserved as a shim — it uploads the file internally, slices, returns the legacy response shape, and deletes the input token. This keeps the cutover window safe.

#### Inspect implementation split

- **Cheap parts (Python ZIP+XML reads):** `is_sliced`, plate metadata from `model_settings.config`, declared filaments from `project_settings.config`, `slice_info.config` estimate if sliced, thumbnail bytes from `Metadata/plate_*.png`.
- **Expensive part (binary subprocess):** `use_set_per_plate` via `orca-headless use-set`.

Inspect result is cached against the token in memory; cache key is `(sha256, schema_version)`. Subsequent inspect calls are O(dict lookup). Bumping `schema_version` invalidates all cached inspects without dropping cached files.

#### Token cache

- LRU only — no TTL. Editing yesterday's job should not require re-upload.
- Configurable: `CACHE_DIR`, `CACHE_MAX_BYTES` (default 10 GB), `CACHE_MAX_FILES` (default 200). Whichever cap hits first triggers LRU.
- Files are content-addressed by SHA-256. Tokens are opaque IDs that map to a SHA.
- Sliced outputs enter the same cache as inputs and count toward the caps.

### `bambu-gateway` changes

```python
class OrcaslicerClient:
    async def upload(self, file: bytes | Path) -> Token
    async def inspect(self, token: Token) -> InspectResult
    async def thumbnail(self, token: Token, plate: int) -> bytes
    async def download(self, token: Token) -> bytes
    async def slice(self, token, machine_id, process_id, filaments, plate_id) -> SliceResult
    async def slice_stream(self, ...) -> AsyncIterator[SliceEvent]
    async def delete(self, token: Token)
    # profile passthroughs unchanged
```

**Files deleted:** `app/parse_3mf.py`, `app/print_estimate.py`. Plate/thumbnail/use-set logic in `app/slice_jobs.py` and the padding in `app/filament_selection.py` is replaced by `OrcaslicerClient` calls.

**Files preserved:** `app/ftp_client.py`, `app/mqtt_client.py`, AMS tray validation in `app/filament_selection.py`, the web UI, the slice job state machine.

## Data flow

### Flow A — User opens an unsliced `.3mf`

1. Browser uploads `.3mf` → gateway.
2. Gateway `POST /3mf` → orcaslicer-cli stores by SHA, returns token.
3. Gateway `GET /3mf/{token}/inspect` → orcaslicer-cli does cheap ZIP reads (plate metadata, declared filaments, thumbnails inventory, sliced state) and shells out to `orca-headless use-set` for paint_color use-set. Returns combined JSON.
4. Browser fetches plate thumbnails via `GET /3mf/{token}/plates/{n}/thumbnail` (proxied by gateway or direct, depending on UI architecture).
5. UI renders file-open view with plates, declared filaments, per-plate use-set, thumbnails. Gateway never opened the ZIP.

### Flow B — User picks profiles and slices

1. User picks machine/process/AMS in browser → gateway.
2. Gateway `POST /slice-stream` with `{token, machine_id, process_id, filaments, plate_id}` → orcaslicer-cli.
3. orcaslicer-cli resolves profiles to JSON files, shells out to `orca-headless slice`.
4. Binary loads 3MF as project (GUI path), applies project overrides, sets filament_map, recenters, slices, exports.
5. Binary streams progress on stderr; orcaslicer-cli forwards as SSE `progress` events.
6. Binary writes sliced 3MF to cache directory; emits final stdout JSON.
7. orcaslicer-cli registers sliced output as a new token, parses `slice_info.config` for estimate, emits SSE `result` event with `output_token`, estimate, thumbnail URLs, settings_transfer.
8. Browser fetches post-slice thumbnails by URL.

### Flow C — User confirms print

1. Browser confirms → gateway.
2. Gateway `GET /3mf/{output_token}` → bytes.
3. Gateway builds `ams_mapping` from current AMS tray state (gateway-side concern).
4. Gateway FTPS-uploads to printer.
5. Gateway sends MQTT `project_file` command with `ams_mapping` and `use_ams`.
6. Gateway optionally `DELETE /3mf/{output_token}` to free cache space (or relies on LRU).

### Reload semantics

`POST /profiles/reload` re-reads vendor + user profiles into the Python index. The binary has no long-lived state — every subprocess invocation reads profiles fresh from JSON files. No binary involvement; existing semantics preserved.

## Error handling

### Binary-side failures

| Failure | Detection | Handling |
|---|---|---|
| Crash / SIGSEGV | non-zero exit + no stdout JSON | Python wraps as `code: "binary_crashed"`, captures last 50 stderr lines |
| Invalid 3MF | binary writes `{status:"error",code:"invalid_3mf",...}` | passthrough |
| Profile resolution failure | binary error `code:"profile_resolve_failed"` | passthrough |
| Build volume exceeded | binary error `code:"build_volume_exceeded", details:{bbox,plate_size}` | gateway shows actionable message |
| Slicing produced no gcode | binary checks output, errors if empty | passthrough |
| Killed by oom-killer | exit 137, no stdout | `code:"binary_oom"` |
| Subprocess timeout | Python timeout (default 5min slice, 30s use-set) | `code:"binary_timeout"`, kills process group |

Stderr progress lines that fail to parse as JSON are logged but do not abort.

### Python-service-side failures

| Failure | HTTP response |
|---|---|
| Token not found / evicted | `404 {"code":"token_unknown"}` — gateway re-uploads transparently |
| Cache full and incoming upload exceeds caps after eviction | `507 {"code":"cache_full"}` |
| Profile id unknown | `400 {"code":"profile_unknown","setting_id":"..."}` |
| Concurrent slice limit hit (semaphore) | request queues; SSE progress emits `phase:"queued"` until slot opens |
| Binary not found / not executable at startup | service refuses to start |

### Gateway-side handling

- `404 token_unknown` → transparent retry: re-upload, get new token, retry the original call once. Cache eviction is invisible to users in normal operation.
- `binary_crashed`, `binary_oom`, `binary_timeout` → surface to UI with slice job marked failed. No automatic retry.

### Logging contract

- Python logs every binary invocation: mode, input token, request size, exit code, duration, last-line stderr summary.
- On error: full stderr (last 50 lines) at `WARNING`. Full stdout JSON at `DEBUG`.
- No stderr suppression; libslic3r writes progress and warnings there.

### Cancellation

- `slice-stream` SSE: client disconnect triggers Python to send SIGTERM to the binary's process group, then SIGKILL after 5s.
- The binary handles SIGTERM gracefully via libslic3r's `Print::cancel_callback`. Worst case is `kill -9` and the partial output gets cleaned up by token-cache file lifecycle.

## Testing strategy

### C++ binary

- **Smoke harness:** shell script that runs `orca-headless slice` and `use-set` against fixtures in `cpp/tests/`, asserts exit codes and JSON envelope shape.
- **No C++ unit tests.** The binary is a thin wrapper; libslic3r's own tests cover the heavy lifting, and Python integration tests exercise the wrapper end-to-end.
- **Reference-output comparison:** for ~5 representative 3MFs, slice via the GUI and via `orca-headless`, diff the output gcodes. Fidelity check that protects against drift from the GUI. Run manually before every upstream rebase.

### Python service

- **Existing pytest suite preserved.** New tests:
  - `test_token_cache.py` — upload, inspect cache hit/miss, eviction policy, `DELETE` endpoints
  - `test_inspect_endpoint.py` — fixture 3MFs with known plate/filament metadata, assert inspect JSON shape
  - `test_slice_token_flow.py` — end-to-end with binary stubbed via subprocess mock; separate test runs the real binary against a small fixture
  - `test_slice_legacy_compat.py` — old multipart `/slice` shim still works
- **Settings-transfer tests:** existing `test_slicer_settings_transfer.py` becomes a fidelity test against the new binary. Python-side smart-transfer logic is gone; we assert the binary produces the same overlay behavior the GUI does.

### bambu-gateway

- Delete tests for `parse_3mf.py`, `print_estimate.py`, anything that mocked 3MF internals.
- Add `OrcaslicerClient` tests with HTTP responses mocked.
- End-to-end test in gateway's existing harness: upload → inspect → slice → estimate → (mocked) FTPS+MQTT.

### Integration / fidelity

- 3–5 reference 3MFs (single-plate, multi-plate, multi-filament with paint_color, large mesh).
- Each slices through both code paths (old AppImage CLI + new orca-headless), comparing gcode (modulo timestamps/comments), print time, weight, filament usage.
- Runs on every PR that touches the binary or its profile-loading layer.

### Manual GUI parity check

Before declaring the binary "done": pick 5 production 3MFs from real users, slice them in the GUI and via the new binary, eyeball the gcode diff, and verify on a real printer. Non-negotiable acceptance gate.

## Build and deployment

- OrcaSlicer source vendored as a git submodule pinned to the 2.3.2 release tag.
- New `cpp/` directory in `orcaslicer-cli` containing `orca-headless.cpp`, `CMakeLists.txt`, and `tests/`.
- Dockerfile changes: replace AppImage extraction with `cmake --build` of libslic3r + `orca-headless`. First build expected ~20–40 minutes; incremental ~2–5 minutes with ccache. Dependencies (CGAL, OpenVDB, Boost, Eigen) come from OrcaSlicer's `deps/` superbuild.
- Version bumps to OrcaSlicer (e.g., 2.3.2 → 2.4.0) are explicit submodule updates with a rebase of `orca-headless.cpp` against any libslic3r API changes. Reference-output tests guard fidelity across the bump.

## Migration plan (high level)

The implementation plan will detail the order. At a glance:

1. **Phase 1:** Bring up `orca-headless` slice mode in parallel with existing AppImage CLI. Both supported via a feature flag. Token cache + new endpoints land in Python service. Reference fidelity tests pass.
2. **Phase 2:** Switch default slice path to `orca-headless`. AppImage stays as fallback for one release. Delete `app/normalize.py`, `_CLAMP_RULES`, smart settings transfer, BBL shims.
3. **Phase 3:** Add `use-set` mode and `/3mf/inspect` endpoint. Migrate gateway to `OrcaslicerClient`. Delete `parse_3mf.py`, `print_estimate.py` from gateway.
4. **Phase 4:** Remove AppImage CLI fallback. Remove legacy multipart `/slice` shim once gateway is fully migrated.

Each phase is independently shippable.

## Open implementation questions

These don't block the design but will be resolved during implementation:

- Exact OrcaSlicer source vendoring approach: git submodule vs. CMake `FetchContent` vs. vendored copy. Submodule is current preference.
- Whether to expose `apply_project_overrides` as a small library function in our fork (so it could in principle be upstreamed) or inline it in `orca-headless.cpp`.
- Whether the post-slice thumbnail rendering needs an offscreen GL context (mesa software rasterizer in container) or whether we can rely on the input 3MF's existing thumbnails. To be confirmed during Phase 1 spike.
