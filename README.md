# OrcaSlicer CLI API

A REST API that wraps [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) to provide headless 3D print slicing. Upload a `.3mf` file with Bambu Lab profile IDs and get back a sliced `.3mf` with generated G-code.

## Quick Start

```bash
docker compose up --build
```

The API will be available at `http://localhost:8000`.

> **Note:** The first build takes a while — OrcaSlicer is compiled from source.

## API Endpoints

### `GET /health`

Returns API status and version.

```json
{"status": "ok", "version": "2.3.1-1"}
```

### `GET /profiles/machines`

Lists available machine profiles (printers).

```json
[{"setting_id": "GM014", "name": "Bambu Lab P1S 0.4 nozzle", "nozzle_diameter": "0.4", "printer_model": "Bambu Lab P1S"}]
```

### `GET /profiles/processes?machine={setting_id}`

Lists print process profiles (layer height, speed, etc.). Optionally filter by machine.

### `GET /profiles/filaments?machine={setting_id}`

Lists filament profiles. Optionally filter by machine.

### `GET /profiles/plate-types`

Lists supported bed surface types:

```json
[
  {"value": "cool_plate", "label": "Cool Plate"},
  {"value": "engineering_plate", "label": "Engineering Plate"},
  {"value": "high_temp_plate", "label": "High Temp Plate"},
  {"value": "textured_pei_plate", "label": "Textured PEI Plate"},
  {"value": "textured_cool_plate", "label": "Textured Cool Plate"},
  {"value": "supertack_plate", "label": "Supertack Plate"}
]
```

### `POST /slice`

Slices a `.3mf` file. Accepts multipart form data:

| Field | Description |
|---|---|
| `file` | The `.3mf` file to slice |
| `machine_profile` | Machine setting_id (e.g., `GM014`) |
| `process_profile` | Process setting_id (e.g., `GP004`) |
| `filament_profiles` | JSON array of filament setting_ids (e.g., `["GFSA00"]`) |
| `plate_type` | Optional snake_case bed surface value (from `/profiles/plate-types`, e.g. `textured_pei_plate`) |

Returns the sliced `.3mf` file as a binary download.

**Example:**

```bash
curl -o sliced.3mf \
  -F "file=@model.3mf" \
  -F "machine_profile=GM014" \
  -F "process_profile=GP004" \
  -F "plate_type=textured_pei_plate" \
  -F 'filament_profiles=["GFSA00"]' \
  http://localhost:8000/slice
```

## Testing

```bash
./test_api.sh                    # test against localhost:8000
./test_api.sh http://host:port   # test against a different host
```

Requires example `.3mf` files in `../bambu-poc/`.

## Configuration

Environment variables (set in `docker-compose.yml`):

| Variable | Default | Description |
|---|---|---|
| `ORCA_BINARY` | `/opt/orcaslicer/bin/orca-slicer` | Path to OrcaSlicer binary |
| `PROFILES_DIR` | `/opt/orcaslicer/profiles/BBL` | Path to BBL profile directory |
