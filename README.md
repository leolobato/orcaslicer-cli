# OrcaSlicer CLI API

A REST API that wraps [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) to provide headless 3D print slicing. Upload a `.3mf` file with Bambu Lab profile IDs and get back a sliced `.3mf` with generated G-code.

## Quick Start

### Using the pre-built image (recommended)

```bash
docker run -d -p 8000:8000 -v ./data:/data ghcr.io/leolobato/orcaslicer-cli:latest
```

Or with Docker Compose, create a `docker-compose.yml`:

```yaml
services:
  orcaslicer-cli:
    image: ghcr.io/leolobato/orcaslicer-cli:latest
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
```

Then run:

```bash
docker compose up
```

### Building from source

```bash
git clone https://github.com/leolobato/orcaslicer-cli.git
cd orcaslicer-cli
docker compose up --build
```

> **Note:** Building from source takes a while — OrcaSlicer is downloaded and extracted from the official AppImage.

---

The API will be available at `http://localhost:8000`.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | API status and version |
| GET | `/profiles/machines` | List machine profiles (printers) |
| GET | `/profiles/processes` | List process profiles. Filter: `?machine={setting_id}` |
| GET | `/profiles/filaments` | List filament profiles. Filter: `?machine={setting_id}&ams_assignable=true` |
| GET | `/profiles/filaments/{setting_id}` | Fully-resolved filament profile with all inherited fields |
| GET | `/profiles/plate-types` | List supported bed surface types |
| POST | `/profiles/filaments` | Import a custom filament profile JSON |
| POST | `/profiles/filaments/resolve-import` | Preview filament import resolution without saving |
| POST | `/profiles/reload` | Hot-reload all profiles from disk |
| POST | `/slice` | Slice a `.3mf` file, returns sliced `.3mf` binary |
| POST | `/slice-stream` | Same as `/slice` but streams progress via SSE |

All profile identifiers use `setting_id` values (e.g. `GM014`, `GP004`, `GFSA00`).

### Slicing example

```bash
curl -o sliced.3mf \
  -F "file=@model.3mf" \
  -F "machine_profile=GM014" \
  -F "process_profile=GP004" \
  -F "plate_type=textured_pei_plate" \
  -F 'filament_profiles=["GFSA00"]' \
  http://localhost:8000/slice
```

### Custom filament import

You can import custom filament profiles that inherit from any built-in profile. The API resolves the full inheritance chain, merges parent fields, and produces a standalone profile ready for slicing.

**Payload:**

```json
{
  "name": "My Custom PLA",
  "inherits": "Bambu PLA Basic @BBL P1S",
  "nozzle_temperature": [230]
}
```

Only `name` is required. Any field you provide overrides the inherited value.

**Inheritance resolution** works recursively — if a parent itself inherits from another profile, the full chain is walked and merged. When multiple vendors define a profile with the same name, the resolver prefers the same vendor as the child profile before falling back to others.

**ID generation:** If no `setting_id` is provided, it defaults to `name`. If no `filament_id` is provided, one is auto-generated as `"P" + md5(name)[:7]` with collision fallback.

**Preview before saving:** Use `POST /profiles/filaments/resolve-import` to see the fully materialized profile (including resolved `filament_id` and `filament_type`) without persisting it. Then call `POST /profiles/filaments` to save.

**AMS assignability:** A filament is assignable to the AMS when it has `instantiation: "true"`, a non-empty `setting_id`, and a resolved `filament_id`. Imported profiles meet these criteria automatically.

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
| `PROFILES_DIR` | `/opt/orcaslicer/profiles` | Path to vendor profile directory |
| `USER_PROFILES_DIR` | `/data` | Path for imported/custom profiles |
| `LOG_LEVEL` | `INFO` | Logging level |

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) — the same license as [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer).
