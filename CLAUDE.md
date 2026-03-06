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

Requires example `.3mf` files in `../bambu-poc/` (not included in this repo). There are no unit tests.

## Architecture

**`app/main.py`** — FastAPI app. Endpoints: `GET /health`, `GET /profiles/{machines,processes,filaments}`, `POST /slice`. The `/slice` endpoint accepts multipart form data with a `.3mf` file and profile setting_ids.

**`app/profiles.py`** — Profile loading and inheritance resolution. At startup (`lifespan`), reads all vendor JSON profiles from disk into memory (`_raw_profiles`). Profiles have an `inherits` chain that gets recursively resolved and memoized. Profiles are identified by their `setting_id` (e.g., `GM014` for P1S 0.4 nozzle). Only profiles with `instantiation: "true"` are exposed as leaf/selectable profiles. The `compatible_printers` field is mapped from profile names to setting_ids in API responses.

**`app/slicer.py`** — Slicing orchestration. Resolves profiles, writes them as temp JSON files, sanitizes the input 3MF (clamps invalid parameter values), performs smart settings transfer from the 3MF onto the process profile, then shells out to `orca-slicer` CLI. Serialized to one concurrent slice via `asyncio.Semaphore(1)`.

**`app/config.py`** — `ORCA_BINARY` and `PROFILES_DIR` paths, configurable via env vars.

**`app/models.py`** — Pydantic response models.

## Key Details

- The `"from"` key in profiles must NOT be stripped during resolution — OrcaSlicer CLI requires it.
- The slicer injects `G92 E0` into `layer_change_gcode` if not already present (workaround for extrusion issues).
- 3MF project settings use smart transfer: when a 3MF contains a `print_settings_id`, the slicer resolves the original profile and diffs the 3MF settings against it, then only transfers user customizations onto the target process profile. Falls back to full overlay when the original profile can't be resolved. The `/slice` response includes `X-Settings-Transfer-Status` (`applied`, `no_original_profile`, `no_customizations`, `no_3mf_settings`) and `X-Settings-Transferred` (JSON array of `{key, value, original}` objects when status is `applied`) headers.
- Profile `setting_id` values (e.g. `GM014`) are the stable identifiers used across the API (not profile names).
