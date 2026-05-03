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

> **Note:** Building from source compiles `libslic3r` and the `orca-headless` binary from the OrcaSlicer C++ source (vendored as a git submodule pinned to v2.3.2). Expect a 10–15 minute first build with BuildKit cache mounts; subsequent builds reuse the deps layer.

---

The API will be available at `http://localhost:8000`.

## Architecture

The service is a thin Python (FastAPI) layer over a purpose-built C++ binary
(`orca-headless`) that links `libslic3r` directly. The Python side owns
profile loading, the token cache, and HTTP routing; the C++ side owns
slicing and 3MF reads/writes through the same code paths the OrcaSlicer
GUI uses.

- **`app/`** — FastAPI app, profile resolution, token cache (`/data/cache`),
  request adapters into `orca-headless`.
- **`cpp/orca-headless`** — compiled from `vendor/OrcaSlicer` (pinned at
  v2.3.2). Two subcommands: `slice` and `use-set`.
- **Token cache** — every uploaded `.3mf` is stored once by sha256;
  subsequent calls (inspect, slice, thumbnail) reference the token.

Set `USE_HEADLESS_BINARY=1` to enable the binary path (default in
production deployments).

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | API status and version |
| GET | `/profiles/machines` | List machine profiles (printers) |
| GET | `/profiles/processes` | List process profiles. Filter: `?machine={setting_id}` |
| GET | `/profiles/filaments` | List filament profiles. Filter: `?machine={setting_id}&ams_assignable=true` |
| GET | `/profiles/machines/{setting_id}` | Fully-resolved machine profile with inheritance chain |
| GET | `/profiles/processes/{setting_id}` | Fully-resolved process profile with inheritance chain |
| GET | `/profiles/filaments/{setting_id}` | Fully-resolved filament profile with inheritance chain |
| GET | `/profiles/plate-types` | List supported bed surface types |
| POST | `/profiles/filaments` | Import a custom filament profile JSON |
| POST | `/profiles/filaments/resolve-import` | Preview filament import resolution without saving |
| DELETE | `/profiles/filaments/{setting_id}` | Delete a custom filament profile |
| POST | `/profiles/reload` | Hot-reload all profiles from disk |
| POST | `/3mf` | Upload a `.3mf` to the token cache; returns `{token, sha256, size}` |
| GET | `/3mf/{token}` | Download cached `.3mf` bytes |
| DELETE | `/3mf/{token}` | Drop a cached upload |
| GET | `/3mf/{token}/inspect` | Structured summary (plates, filaments, used-filament dispatch, estimate, thumbnails) |
| GET | `/3mf/{token}/plates/{n}/thumbnail` | PNG bytes of the plate thumbnail (`?kind=main\|small\|top\|pick\|no_light`) |
| POST | `/slice/v2` | Slice a cached `.3mf`, returns `{output_token, estimate, settings_transfer}` |
| POST | `/slice-stream/v2` | Same as `/slice/v2` but streams progress via SSE |

All profile identifiers use `setting_id` values (e.g. `GM014`, `GP004`, `GFSA00`).

### Slicing example

```bash
# 1. Upload — get a cache token
TOK=$(curl -s -X POST http://localhost:8000/3mf \
  -F "file=@model.3mf" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")

# 2. Slice via JSON body
OUT=$(curl -s -X POST http://localhost:8000/slice/v2 \
  -H 'Content-Type: application/json' \
  -d "{
    \"input_token\": \"$TOK\",
    \"machine_id\": \"GM014\",
    \"process_id\": \"GP004\",
    \"filament_settings_ids\": [\"GFSA00\"],
    \"recenter\": false
  }" | python3 -c "import json,sys; print(json.load(sys.stdin)['output_token'])")

# 3. Download the sliced .3mf
curl -s -o sliced.3mf http://localhost:8000/3mf/$OUT
```

The token cache is content-addressed (sha256-keyed): repeated uploads of the same bytes resolve to the same token. `recenter=false` keeps the model in its 3MF-stored position, matching the GUI's behaviour on import.

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

## Web UI

A built-in web interface is available at `http://localhost:8000/web/` for browsing and managing profiles.

- **Browse** all machine, process, and filament profiles with search filtering
- **Inspect** any profile to see its fully resolved fields or an inheritance diff view showing what each level in the chain overrides
- **Filter by machine** using the sidebar dropdown — processes and filaments automatically filter to compatible profiles
- **Create** custom filament profiles by picking a parent and overriding specific fields (grouped by category: Temperature, Retraction, Speed, etc.)
- **Edit and delete** existing custom filament profiles

The UI is served as static files from the same container — no additional setup needed.

## Configuration

Environment variables (set in `docker-compose.yml`):

| Variable | Default | Description |
|---|---|---|
| `USE_HEADLESS_BINARY` | `0` | Set to `1` to route slicing through the in-process `orca-headless` C++ binary |
| `ORCA_HEADLESS_BINARY` | `/opt/orca-headless/bin/orca-headless` | Path to the compiled `orca-headless` binary |
| `PROFILES_DIR` | `/opt/orcaslicer/profiles` | Path to vendor profile directory |
| `USER_PROFILES_DIR` | `/data` | Path for imported/custom profiles |
| `CACHE_DIR` | `/data/cache` | Path for the token cache (uploaded + sliced 3MFs) |
| `CACHE_MAX_BYTES` | `10737418240` (10 GB) | Token cache size cap; oldest evicted first |
| `CACHE_MAX_FILES` | `200` | Token cache entry-count cap |
| `LOG_LEVEL` | `INFO` | Logging level |

## Related Projects

OrcaSlicer CLI is the **headless slicing engine and profile catalog** in a suite of self-hosted projects that together replace the Bambu Handy app for printers in **Developer Mode** — keeping everything on your LAN, with no Bambu cloud.

**Self-hosted services**

- **[bambu-gateway](https://github.com/leolobato/bambu-gateway)** — Printer control plane and slicing web app. Talks to printers over MQTT/FTPS to monitor status, send commands, and upload jobs. Slices and prints 3MF files from the browser using `orcaslicer-cli`.
- **OrcaSlicer CLI** — this project.
- **[bambu-spool-helper](https://github.com/leolobato/bambu-spool-helper)** — Bridge between [Spoolman](https://github.com/Donkie/Spoolman) and the printer's AMS. Links real spools to Bambu filament profiles (via `orcaslicer-cli`) and pushes the settings to a chosen tray over MQTT.

**iOS apps**

- **[bambu-gateway-ios](https://github.com/leolobato/bambu-gateway-ios)** — Phone client for `bambu-gateway`. Browse printers, import 3MF files (including from MakerWorld), preview G-code, and start prints. Live Activities and push notifications for print state changes.
- **[spool-browser](https://github.com/leolobato/spool-browser)** — Phone client for `bambu-spool-helper` and Spoolman. Browse the spool inventory, link Bambu profiles to spools, activate filaments on the AMS, and print physical spool labels over Bluetooth.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) — the same license as [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer).
