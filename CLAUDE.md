# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A REST API that wraps OrcaSlicer's CLI to provide headless 3D print slicing. It loads all vendor printer/process/filament profiles from OrcaSlicer's bundled resources, resolves their inheritance chains, and exposes endpoints to list profiles and slice `.3mf` files.

## Build & Run

This is a Docker-only project. OrcaSlicer is compiled from source in the Docker build.

```bash
docker compose up --build        # build and start on port 8000
docker compose up                # start (already built)
```

The API runs via uvicorn at `http://localhost:8000`. There is no local (non-Docker) dev setup — the OrcaSlicer binary and BBL profiles only exist inside the container.

## Testing

```bash
./test_api.sh                    # smoke tests against running container
./test_api.sh http://host:port   # test against a different host
```

Requires example `.3mf` files in `../bambu-poc/` (not included in this repo).

Unit tests exist in `tests/` and can be run with pytest inside the container:

- `test_filament_vendor_resolution.py` — filament inheritance across vendors and machine-scoped ID preference
- `test_slicer_settings_transfer.py` — filament key exclusion and customization detection
- `test_slice_request_parsing.py` — legacy list and sparse object filament parsing

## Architecture

**`app/main.py`** — FastAPI app. Endpoints: `GET /health`, `GET /profiles/{machines,processes,filaments}`, `POST /slice`, `POST /slice-stream` (SSE streaming with phase/progress), `POST /profiles/reload` (hot-reload from disk), `POST /profiles/filaments/resolve-import` (preview), `POST /profiles/filaments/import` (save custom filament). The `/slice` endpoint accepts multipart form data with a `.3mf` file and profile setting_ids.

**`app/profiles.py`** — Profile loading and inheritance resolution. At startup (`lifespan`), reads all vendor JSON profiles from disk into five in-memory indexes (`_raw_profiles`, type map, vendor map, name index, setting_id reverse index). Profiles have an `inherits` chain that gets recursively resolved and memoized. Parent lookup prefers same-vendor profiles before searching across vendors. `OrcaFilamentLibrary` loads first so vendor-specific profiles can override it. Only profiles with `instantiation: "true"` are exposed as leaf/selectable profiles. The `compatible_printers` field is mapped from profile names to setting_ids in API responses. `_write_bbl_machine_full_shims` materializes `BBL/machine_full/{name}.json` files holding `{"model_id": ...}` for each BBL parent machine — OrcaSlicer's CLI reads them at slice time to stamp `printer_model_id` (e.g. `"N1"`) onto `slice_info.config`; the AppImage doesn't ship that directory. Shims are written under `ORCA_RESOURCES_DIR/profiles/BBL/machine_full/` (not `PROFILES_DIR/BBL/...`) because the Dockerfile keeps two separate copies of the resources tree — the binary's `resources_dir()` resolves to `/opt/resources/` while our Python loader reads `/opt/orcaslicer/profiles/`.

**`app/slicer.py`** — Slicing orchestration. Resolves profiles, writes them as temp JSON files, sanitizes the input 3MF (clamps invalid parameter values via `_CLAMP_RULES`), performs smart settings transfer from the 3MF onto the process profile and onto per-filament profiles where the selected filament matches the 3MF's original, then shells out to `orca-slicer` CLI. Serialized to one concurrent slice via `asyncio.Semaphore(1)`. Process-side customization detection uses the 3MF's own fingerprint at `different_settings_to_system[0]` as an allowlist — only keys the GUI recorded as user-customized are transferred onto the target process profile; filament-like keys (`filament_*` / `*_filament`) are excluded even if listed there. Per-filament customizations at `different_settings_to_system[i + 2]` are applied to filament slot `i` only when that slot's selected filament `name` equals the 3MF's `filament_settings_id[i]`; when the user swapped in a different filament, the declared keys are discarded and reported as `filament_changed` so the client can surface the discard.

**`app/normalize.py`** — Per-filament vector-length normalization. Replicates `Preset::normalize` (OrcaSlicer `src/libslic3r/Preset.cpp` 370-415) for the ~38 per-filament keys observed to diverge between the GUI and our CLI output on multi-filament 3MFs (`pressure_advance`, `filament_cooling_*`, `textured_cool_plate_temp`, etc.). Applied to the process profile just before the temp JSON is written. Defaults were extracted from `PrintConfig.cpp` v2.3.2; derivation is in `docs/normalization-research.md`.

**`app/threemf.py`** — 3MF ZIP parsing, model bounding box extraction (affine transform math), build volume validation, multi-plate to single-plate extraction, and thumbnail preservation. `extract_plate` keeps each input build item as a distinct `<object>` in the rebuilt 3MF (preserving its original name and `identify_id`), so OrcaSlicer can emit per-object `label_object` boundaries in gcode rather than collapsing the plate into a single "Model".

**`app/slice_request.py`** — Parses `filament_profiles` form field. Supports legacy list format (`["GFSA00", "GFL99"]`) and sparse object format (`{"0": "GFSA00", "1": {"profile_setting_id": "GFL99", "tray_slot": 2}}`).

**`app/config.py`** — Paths and version constants. API version format: `{ORCA_VERSION}-{API_REVISION}`.

**`app/models.py`** — Pydantic response models.

## Environment Variables

```
ORCA_BINARY       = /opt/orcaslicer/bin/orca-slicer
PROFILES_DIR      = /opt/orcaslicer/profiles
USER_PROFILES_DIR = /data
LOG_LEVEL         = INFO
```

## Key Details

- The OrcaSlicer source is available at ../OrcaSlicer. We should use the GUI behavior as reference for our CLI wrapper and the source code should be explored whenever needed.
- OrcaSlicer version pinned to 2.3.2. The Dockerfile uses an AppImage extraction workaround for arm64 (computes ELF offset instead of `--appimage-extract`).
- The `"from"` key in profiles must NOT be stripped during resolution — OrcaSlicer CLI requires it.
- We do NOT inject `G92 E0` into `layer_change_gcode`. OrcaSlicer's validator (`Print.cpp:1585-1605`) only requires it on non-BBL printers running Marlin firmware with relative extrusion, and forbids it on non-BBL printers using absolute extrusion — so injecting unconditionally would break the latter. For BBL printers (the actual target of this CLI) the validator is a no-op and the GUI demonstrably slices without it.
- 3MF project settings use smart transfer driven by the 3MF's own `different_settings_to_system` fingerprint. The list is laid out as `[process, filament_0, …, filament_{N-1}, printer]` (per OrcaSlicer's `PresetBundle::load_3mf_*`, which loads the printer slot from `num_filaments + 1`): index 0 is the process allowlist, indices `1..N` are per-filament allowlists, and the trailing slot is the printer/machine allowlist. Only declared keys are overlaid onto the target profiles. When the fingerprint is missing or empty, nothing is transferred. Per-filament customizations are applied only when the selected filament's `name` matches the 3MF's `filament_settings_id[slot]` — when the user swaps to a different filament, declared customizations are discarded and reported back so the client can surface the discard. Printer customizations have no name guard: the machine profile is fixed by the request, so any declared printer-slot key is overlaid straight onto the resolved machine profile. The `/slice` response includes `X-Settings-Transfer-Status` (`applied`, `no_customizations`, `no_3mf_settings`), `X-Settings-Transferred` (process-side JSON array of `{key, value, original}`), `X-Filament-Settings-Transferred` (JSON array of per-slot entries with `{slot, original_filament, selected_filament, status, transferred, discarded}`), and `X-Machine-Settings-Transferred` (JSON array of `{key, value, original}` for printer-slot keys) headers.
- Profile `setting_id` values (e.g. `GM014`) are the stable identifiers used across the API (not profile names).
- Internal profile key format: `"{vendor_name}::{profile_name}"` for stable cross-reload references.
- Custom filament import generates IDs via `"P" + md5(logical_name)[:7]` with timestamp-based collision fallback.
- A filament is AMS-assignable only if it has `instantiation: "true"`, a non-empty `setting_id`, and resolves to a non-empty `filament_id`.
- Plate types map from snake_case API values (`cool_plate`, `engineering_plate`, `high_temp_plate`, `textured_pei_plate`, `textured_cool_plate`, `supertack_plate`) to OrcaSlicer display names written as `curr_bed_type`.
- Multi-plate 3MFs are auto-extracted to single-plate; cross-printer scenarios trigger auto-arrange.
