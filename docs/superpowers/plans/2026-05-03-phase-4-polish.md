# Phase 4: Docs, CI, residuals — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the loose ends from Phases 1–3: bring the README in line with the post-Phase-2 reality, get CI catching regressions on every PR, and reset the `vendor/OrcaSlicer` submodule pointer to match the working tree.

**Out of scope (deferred):**
- The ~0.6% structural diff (157 vs 137 internal solid infill regions on fixture 01). Probably a `FullPrintConfig::defaults()` vs `PresetBundle::full_config()` discrepancy. Cosmetic at this point — file an issue if it ever bites a real user.

---

## Tasks

### Task 1: Reset `vendor/OrcaSlicer` submodule pointer

**Why:** `git ls-tree HEAD vendor/OrcaSlicer` reports `0c41570` (some random newer ref) but the working tree is at `c724a3f` ("bump version to 2.3.2") — the release tag matching `ORCA_VERSION = "2.3.2"` in `app/config.py`. The drift is pre-existing; it predates this branch's work. The working tree is correct; the tracked pointer is wrong. Fix by committing the working-tree pointer.

**Files:**
- Modify: `vendor/OrcaSlicer` submodule pointer (via `git add`)

- [ ] **Step 1: Confirm the working-tree pointer matches the pinned version**

```bash
git -C vendor/OrcaSlicer rev-parse HEAD
# expect c724a3f5f51c52336624b689e846c8fbc943a912
git -C vendor/OrcaSlicer log -1 --oneline
# expect "c724a3f5f5 bump version to 2.3.2"
grep ORCA_VERSION app/config.py
# expect ORCA_VERSION = "2.3.2"
```

- [ ] **Step 2: Stage and commit the pointer**

```bash
git add vendor/OrcaSlicer
git diff --staged --submodule
# expect: Submodule vendor/OrcaSlicer 0c41570...c724a3f
```

- [ ] **Step 3: Commit**

```bash
git commit -m "Pin vendor/OrcaSlicer to v2.3.2 (c724a3f) to match working tree"
```

---

### Task 2: README rewrite for post-Phase-2 reality

**Why:** Current README (142 lines, last meaningful edit pre-Phase 1) describes the AppImage extraction path that's gone, the legacy `/slice` multipart endpoint that's gone, and is missing the token cache + headless binary surface the gateway now depends on.

**Files:**
- Modify: `README.md`

Replace the relevant sections per the existing structure:

#### Replacement: "Building from source" note (around current line 39)

Old wording: "OrcaSlicer is downloaded and extracted from the official AppImage."

New wording:
> **Note:** Building from source compiles `libslic3r` and the `orca-headless` binary from the OrcaSlicer C++ source (vendored as a git submodule pinned to v2.3.2). Expect a 10–15 minute first build with BuildKit cache mounts; subsequent builds reuse the deps layer.

#### Replacement: "API Endpoints" table

Drop the `POST /slice` and `POST /slice-stream` rows. Add the token cache + headless binary endpoints. Match this table:

```markdown
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
```

#### Replacement: "Slicing example"

Old (legacy multipart):

```bash
curl -o sliced.3mf \
  -F "file=@model.3mf" \
  -F "machine_profile=GM014" \
  ...
  http://localhost:8000/slice
```

New (token-then-slice):

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

Add a brief paragraph immediately after explaining: the token cache is content-addressed (sha256-keyed); repeated uploads of the same bytes resolve to the same token; `recenter=false` keeps the model in its 3MF-stored position (matching the GUI's behaviour on import).

#### Replacement: "Configuration" table

Add the new env vars introduced in Phase 1 + Phase 3:

```markdown
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
```

Drop `ORCA_BINARY` from the table — it's still wired in `app/config.py` for backward compatibility but no longer used by any code path on `feat/orca-headless`.

#### Add: brief architecture section

Insert after "Quick Start" / before "API Endpoints":

```markdown
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
```

- [ ] **Step 1: Apply the replacements**

Edit `README.md`. Preserve the "Web UI", "Related Projects", and "License" sections — those are accurate.

- [ ] **Step 2: Verify the README renders cleanly**

```bash
# Sanity check: any remaining stale references?
grep -n 'AppImage\|"/slice"\|/slice ' README.md
# Expected: zero hits
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Rewrite README for /slice/v2 + token cache + headless binary"
```

---

### Task 3: CI workflow for pytest + fidelity test on PR

**Why:** Today only `release.yml` exists (push of `v*` tags → docker push). No PR validation. With `/slice/v2`'s fidelity test pinned to `_fixture/01`, every PR can run that test against a built container and catch regressions before merge.

**Files:**
- Create: `.github/workflows/ci.yml`

The workflow needs to:
1. Build the Docker image (full BuildKit cache mounts)
2. Run the unit-test subset (`pytest tests/ -q --ignore=tests/integration`) inside the container
3. Run the integration tests (`pytest tests/integration/`) on the runner against the running container
4. Upload `_fixture/01` files as a workflow input — but `_fixture/` lives outside this repo (in the host workspace at `../_fixture`). For CI, we need to either:
   - **(a)** Check the fixtures into a CI-only path under this repo (e.g. `tests/fixtures/integration/`) — bypasses the host workspace assumption
   - **(b)** Skip the integration tests in CI for now and run unit tests only — pragmatic; preserves the `_fixture/` host convention
   - **(c)** Make the fixtures fetchable as a workflow artifact from a separate repo

Recommendation: **(b)** for this task. Just unit + smoke (`/health`, `/profiles/machines`) coverage. The fidelity test runs locally in dev and is the source of truth.

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  pull_request:
  push:
    branches:
      - main
      - 'feat/**'

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          submodules: recursive

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build image
        uses: docker/build-push-action@v6
        with:
          context: .
          load: true
          tags: orcaslicer-cli:ci
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Start container
        run: |
          docker run -d --name orcaslicer-cli \
            -p 8000:8000 \
            -e USE_HEADLESS_BINARY=1 \
            -v ${{ github.workspace }}/data:/data \
            -v ${{ github.workspace }}/app:/app/app \
            -v ${{ github.workspace }}/tests:/app/tests \
            -v ${{ github.workspace }}/conftest.py:/app/conftest.py \
            orcaslicer-cli:ci
          # Wait for /health
          for i in $(seq 1 30); do
            if curl -sf http://localhost:8000/health; then echo; break; fi
            sleep 2
          done

      - name: Run unit tests
        run: |
          docker exec orcaslicer-cli pytest tests/ -q --ignore=tests/integration

      - name: Smoke health + profiles
        run: |
          curl -sf http://localhost:8000/health
          curl -sf 'http://localhost:8000/profiles/machines' | python3 -c \
            "import json,sys; d=json.load(sys.stdin); print('machines:', len(d.get('machines', [])))"
          curl -sf 'http://localhost:8000/profiles/filaments?ams_assignable=true' | python3 -c \
            "import json,sys; d=json.load(sys.stdin); print('ams filaments:', len(d.get('filaments', [])))"

      - name: Container logs on failure
        if: failure()
        run: docker logs orcaslicer-cli
```

Note: this CI runs on every PR. The first build will be slow (no cache yet); subsequent runs reuse the GitHub Actions cache (`type=gha`). The `submodules: recursive` checkout pulls `vendor/OrcaSlicer` so the C++ build has its source.

- [ ] **Step 2: Verify the YAML parses**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

- [ ] **Step 3: Commit + push**

```bash
git add .github/workflows/ci.yml
git commit -m "Add CI workflow: build + unit tests + smoke on PR"
```

The workflow will trigger on the next push to `feat/orca-headless`. Watch the run via `gh run list -L 1 -w "CI"` and `gh run view --log` if it fails.

---

## Self-Review

**Coverage of the Phase 4 scope** (per the roadmap section in earlier plans):

- README rewrite ✓ Task 2
- CI hookup ✓ Task 3 (fidelity test deferred to local-only; unit + smoke run in CI)
- `vendor/OrcaSlicer` submodule pointer drift ✓ Task 1
- ~0.6% structural diff investigation — explicitly deferred (cosmetic; file an issue when it surfaces)

**Risk:** Task 3's CI build may exceed GitHub Actions' default timeout (6 hours) on the first run if cache is cold. Subsequent runs hit the cache hard. If the first run times out, a workflow_dispatch can manually re-trigger; the second run will succeed.

**Placeholder scan:** all steps have actual commands and code. No "TBD" / "similar to Task N" / "add error handling".
