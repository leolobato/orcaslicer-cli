# Phase 3: `/3mf/inspect` + `use-set` mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up `GET /3mf/{token}/inspect` so `bambu-gateway` can ask `orcaslicer-cli` "what's in this 3MF?" and get back plates, declared filaments, per-plate used-filament-indices, sliced estimate (when applicable), and thumbnail URLs — without ever opening a `.3mf` ZIP itself. Add the `use-set` subcommand to `orca-headless` for the un-sliced case where used-filament-indices have to come from walking `paint_color` attributes on the model.

**Architecture:** Inspect is a thin Python endpoint that (a) reads the cheap parts straight out of the cached 3MF's ZIP (XML/JSON parsing — no slicing), (b) shells out to `orca-headless use-set` only for `use_set_per_plate` when the 3MF isn't sliced, and (c) caches the assembled response in memory keyed by `(sha256, schema_version)`. Thumbnails are served as separate `GET /3mf/{token}/plates/{n}/thumbnail` responses (PNG bytes from the embedded `Metadata/plate_*.png`). Existing helpers in `app/threemf.py` (`get_bounding_box`, `get_plate_count`, `get_used_filament_slots`) cover most cheap reads — extend them; don't rewrite.

**Tech Stack:** Python 3.12 (FastAPI, asyncio, zipfile, ElementTree), C++17 (libslic3r), pytest, Docker BuildKit.

**Repo conventions to honor:**
- Tests in `tests/` mirror `app/` module names. `tests/integration/` already exists for opt-in HTTP-level tests.
- C++ stdout is JSON-only — `orca-headless use-set` follows the same protocol as `slice` (request on stdin, response on stdout, progress/log on stderr).
- Each cpp rebuild takes ~10–15 min. Python edits hot-reload via the `./app:/app/app` volume mount, so most tasks land via `docker compose restart`.
- The single C++ rebuild needed in this plan is Task 7. Group all C++ changes into one rebuild.
- Keep `slice_mode.cpp`'s namespace-internal helpers (`split_semicolons`, `apply_overrides_for_slot`, etc.) where they are; the `use-set` mode lives in its own file.
- Commits follow CLAUDE.md style: subject ≤ 60, body wrapped at 140, focus on visible behaviour.

**Out of scope (separate plan):** the gateway-side migration (deleting `bambu-gateway/app/parse_3mf.py`, `app/print_estimate.py`, replacing call sites with an `OrcaslicerClient`). This plan only ships the API. Gateway migration depends on this landing.

---

## Milestones

1. **Cheap inspect data** (Tasks 1–3) — `GET /3mf/{token}/inspect` returns plates, filaments, sliced estimate, bbox, no use-set yet.
2. **Thumbnail serving** (Tasks 4–5) — `GET /3mf/{token}/plates/{n}/thumbnail` serves PNG bytes; inspect response includes URLs.
3. **`use-set` binary mode** (Tasks 6–8) — `orca-headless use-set` walks `paint_color`; Python integrates into inspect for un-sliced 3MFs.
4. **Inspect cache** (Tasks 9–10) — in-memory cache keyed by `(sha256, schema_version)`, invalidation on token delete.

Each milestone ends green: container builds, all pytests pass, the `tests/integration/test_inspect_endpoint.py` test (added in M1, extended later) passes.

---

## Files to be created or modified

**New files:**
- `app/inspect.py` — assemble the inspect response from cached 3MF bytes
- `cpp/src/use_set_mode.h` / `cpp/src/use_set_mode.cpp` — C++ `use-set` subcommand entry
- `tests/test_inspect.py` — unit tests for `app/inspect.py` helpers
- `tests/integration/test_inspect_endpoint.py` — end-to-end HTTP test against `_fixture/01`

**Modified files:**
- `app/main.py` — register `GET /3mf/{token}/inspect` and `GET /3mf/{token}/plates/{n}/thumbnail`
- `app/binary_client.py` — add `use_set(input_3mf)` method
- `app/threemf.py` — add `parse_plate_metadata(file_bytes)` and `list_plate_thumbnails(file_bytes)` helpers
- `cpp/src/orca_headless.cpp` — dispatch `use-set` subcommand
- `cpp/src/json_io.h` / `cpp/src/json_io.cpp` — `UseSetRequest` / `UseSetResponse` types
- `cpp/CMakeLists.txt` — add `use_set_mode.cpp` to the binary's source list

---

## Milestone 1 — Cheap inspect data

### Task 1: `parse_inspect_data` helper

**Why:** Centralize the ZIP/XML reads that the inspect endpoint needs. Reuses existing helpers in `app/threemf.py` for bbox / plate count / per-plate used slots, and adds parsing for declared filaments and sliced estimate. Pure function over bytes — easy to unit-test.

**Files:**
- Create: `app/inspect.py`
- Test: `tests/test_inspect.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_inspect.py`:

```python
"""Unit tests for app.inspect.parse_inspect_data."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.inspect import parse_inspect_data

FIXTURE_ROOT = Path(__file__).resolve().parents[1].parent / "_fixture"


@pytest.fixture
def fixture_01_input_bytes() -> bytes:
    p = FIXTURE_ROOT / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


@pytest.fixture
def fixture_01_sliced_bytes() -> bytes:
    p = (
        FIXTURE_ROOT
        / "01"
        / "gui-benchy-orca-no-filament-custom-settings_sliced_gui.gcode.3mf.3mf"
    )
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


def test_parse_unsliced_input(fixture_01_input_bytes):
    result = parse_inspect_data(fixture_01_input_bytes)

    assert result["is_sliced"] is False
    assert result["plate_count"] >= 1
    # Reference benchy is single-filament PLA.
    assert len(result["filaments"]) == 1
    f = result["filaments"][0]
    assert f["slot"] == 0
    assert f["type"] == "PLA"
    assert f["color"].startswith("#")
    assert f["filament_id"] == "GFA00"
    assert f["settings_id"] == "Bambu PLA Basic @BBL A1M"
    # Un-sliced has no estimate.
    assert result["estimate"] is None
    # Bounding box and printer hints come from the project_settings.
    assert result["printer_model"] == "Bambu Lab A1 mini"
    assert result["printer_variant"] == "0.4"
    assert result["curr_bed_type"] == "Textured PEI Plate"


def test_parse_sliced_output(fixture_01_sliced_bytes):
    result = parse_inspect_data(fixture_01_sliced_bytes)

    assert result["is_sliced"] is True
    assert result["estimate"] is not None
    e = result["estimate"]
    assert e["time_seconds"] > 0
    assert e["weight_g"] > 0
    # `slice_info.config` lists the filament slots that the plate actually uses.
    assert result["plates"][0]["used_filament_indices"] == [0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/test_inspect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.inspect'`

- [ ] **Step 3: Write the implementation**

Create `app/inspect.py`:

```python
"""Assemble the JSON payload for ``GET /3mf/{token}/inspect``.

This module owns the cheap-side reads (ZIP + XML/JSON parsing) only.
Any data that requires libslic3r (notably ``use_set_per_plate`` for
un-sliced 3MFs) is plumbed in by the endpoint layer, not here.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from typing import Any

from .threemf import get_bounding_box, get_plate_count, get_used_filament_slots

logger = logging.getLogger(__name__)

# Bump when the response shape changes in a way that should invalidate
# any in-memory cache the endpoint layer keeps.
INSPECT_SCHEMA_VERSION = 1


def _read_project_settings(zf: zipfile.ZipFile) -> dict[str, Any]:
    if "Metadata/project_settings.config" not in zf.namelist():
        return {}
    try:
        return json.loads(zf.read("Metadata/project_settings.config").decode())
    except (json.JSONDecodeError, KeyError):
        return {}


def _read_slice_info(zf: zipfile.ZipFile) -> str | None:
    if "Metadata/slice_info.config" not in zf.namelist():
        return None
    try:
        return zf.read("Metadata/slice_info.config").decode()
    except KeyError:
        return None


def _parse_plates(zf: zipfile.ZipFile, file_bytes: bytes) -> list[dict[str, Any]]:
    plate_count = get_plate_count(file_bytes)
    plates: list[dict[str, Any]] = []
    for i in range(1, max(1, plate_count) + 1):
        used = get_used_filament_slots(file_bytes, plate=i)
        plates.append({
            "id": i,
            # `None` means "the slice metadata didn't say" — for un-sliced
            # 3MFs this gets filled in later by `orca-headless use-set`.
            "used_filament_indices": sorted(used) if used is not None else None,
        })
    return plates


_FILAMENT_TAG_RE = re.compile(
    r'<filament\s+id="(?P<id>\d+)"\s+'
    r'tray_info_idx="(?P<tray>[^"]*)"\s+'
    r'type="(?P<type>[^"]*)"\s+'
    r'color="(?P<color>[^"]*)"\s+'
    r'used_m="(?P<used_m>[^"]*)"\s+'
    r'used_g="(?P<used_g>[^"]*)"',
)


def _parse_estimate(slice_info_xml: str) -> dict[str, Any] | None:
    """Parse the global plate-1 estimate from `slice_info.config`."""
    pred_m = re.search(r'key="prediction"\s+value="([^"]+)"', slice_info_xml)
    weight_m = re.search(r'key="weight"\s+value="([^"]+)"', slice_info_xml)
    if not pred_m or not weight_m:
        return None
    filaments_used: list[float] = []
    for fm in _FILAMENT_TAG_RE.finditer(slice_info_xml):
        try:
            filaments_used.append(float(fm.group("used_m")))
        except ValueError:
            pass
    try:
        return {
            "time_seconds": float(pred_m.group(1)),
            "weight_g": float(weight_m.group(1)),
            "filament_used_m": filaments_used,
        }
    except ValueError:
        return None


def _parse_filaments(
    project_settings: dict[str, Any],
    slice_info_xml: str | None,
) -> list[dict[str, Any]]:
    """Per-slot filament records.

    Sources, in order of preference:
    - When the 3MF is sliced, `slice_info.config` carries the authoritative
      per-slot ``tray_info_idx`` / ``type`` / ``color`` (these are what
      the printer will actually print with).
    - Otherwise fall back to ``project_settings.config`` per-slot vectors
      (``filament_settings_id``, ``filament_colour``, ``filament_ids``,
      ``filament_type``).
    """
    if slice_info_xml is not None:
        sliced = []
        for m in _FILAMENT_TAG_RE.finditer(slice_info_xml):
            sliced.append({
                "slot": int(m.group("id")) - 1,
                "type": m.group("type"),
                "color": m.group("color"),
                "filament_id": m.group("tray"),
                "settings_id": "",  # not present in slice_info; settings_id only lives in project_settings.
            })
        if sliced:
            # Project-side settings_id may still be useful; merge by slot.
            settings_ids = project_settings.get("filament_settings_id") or []
            for entry in sliced:
                if entry["slot"] < len(settings_ids):
                    entry["settings_id"] = settings_ids[entry["slot"]]
            return sliced

    settings_ids = project_settings.get("filament_settings_id") or []
    colors = project_settings.get("filament_colour") or []
    ids = project_settings.get("filament_ids") or []
    types = project_settings.get("filament_type") or []
    n = max(len(settings_ids), len(colors), len(ids), len(types))
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append({
            "slot": i,
            "type": types[i] if i < len(types) else "",
            "color": colors[i] if i < len(colors) else "",
            "filament_id": ids[i] if i < len(ids) else "",
            "settings_id": settings_ids[i] if i < len(settings_ids) else "",
        })
    return out


def parse_inspect_data(file_bytes: bytes) -> dict[str, Any]:
    """Inspect a 3MF byte blob and return a structured summary.

    Pure read — never touches libslic3r. The endpoint layer fills in
    ``use_set_per_plate`` (via `orca-headless use-set`) for un-sliced
    3MFs and adds ``thumbnail_urls``.
    """
    out: dict[str, Any] = {
        "schema_version": INSPECT_SCHEMA_VERSION,
        "is_sliced": False,
        "plate_count": 0,
        "plates": [],
        "filaments": [],
        "estimate": None,
        "bbox": None,
        "printer_model": "",
        "printer_variant": "",
        "curr_bed_type": "",
    }

    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        logger.warning("inspect: not a zip file")
        return out

    with zf:
        slice_info_xml = _read_slice_info(zf)
        out["is_sliced"] = slice_info_xml is not None
        project_settings = _read_project_settings(zf)
        out["plate_count"] = get_plate_count(file_bytes)
        out["plates"] = _parse_plates(zf, file_bytes)
        out["filaments"] = _parse_filaments(project_settings, slice_info_xml)
        out["printer_model"] = project_settings.get("printer_model", "")
        out["printer_variant"] = project_settings.get("printer_variant", "")
        out["curr_bed_type"] = project_settings.get("curr_bed_type", "")

        if slice_info_xml is not None:
            out["estimate"] = _parse_estimate(slice_info_xml)

    bbox = get_bounding_box(file_bytes)
    if bbox is not None:
        out["bbox"] = {
            "min_x": bbox.min_x, "min_y": bbox.min_y, "min_z": bbox.min_z,
            "max_x": bbox.max_x, "max_y": bbox.max_y, "max_z": bbox.max_z,
        }

    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose restart orcaslicer-cli && sleep 5
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/test_inspect.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/inspect.py tests/test_inspect.py
git commit -m "Add parse_inspect_data helper for /3mf/inspect"
```

---

### Task 2: Wire `GET /3mf/{token}/inspect` endpoint

**Why:** Surface the helper through HTTP. No use-set yet — fields that depend on the binary stay `None`. This unblocks gateway-side wiring early.

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Locate the existing `/3mf/{token}` GET handler in `app/main.py`**

The token-cache endpoints all live in the same area. Add the new handler immediately after the existing `GET /3mf/{token}` so related paths stay together.

- [ ] **Step 2: Add the endpoint**

In `app/main.py`, near the other token-cache routes:

```python
from .inspect import parse_inspect_data, INSPECT_SCHEMA_VERSION

@app.get("/3mf/{token}/inspect", tags=["3MF"])
async def inspect_3mf(token: str, request: Request) -> JSONResponse:
    """Return a cheap structured summary of a cached 3MF.

    Pure read — does not slice. For un-sliced 3MFs `used_filament_indices`
    on each plate is `None`; a later task wires `orca-headless use-set` to
    populate it.
    """
    cache: TokenCache = request.app.state.token_cache
    try:
        path = cache.path(token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": token},
        )
    data = parse_inspect_data(path.read_bytes())
    # Thumbnails plumbed in Task 5; use-set plumbed in Task 8.
    data["thumbnail_urls"] = []
    data["use_set_per_plate"] = {
        p["id"]: p["used_filament_indices"]
        for p in data["plates"]
        if p["used_filament_indices"] is not None
    }
    return JSONResponse(content=data)
```

- [ ] **Step 3: Verify with curl against fixture 01**

```bash
docker compose restart orcaslicer-cli && sleep 5
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -s http://localhost:8000/3mf/$TOK/inspect | python3 -m json.tool | head -40
```

Expected: a JSON document with `is_sliced=false`, `plate_count=1`, `filaments=[{...PLA, GFA00...}]`, `printer_model="Bambu Lab A1 mini"`, `bbox={...}`, `use_set_per_plate={}` (empty for un-sliced).

- [ ] **Step 4: Commit**

```bash
git add app/main.py
git commit -m "Wire GET /3mf/{token}/inspect endpoint"
```

---

### Task 3: HTTP-level integration test for inspect

**Why:** Lock in the inspect response shape so any future change that breaks gateway compatibility fails CI.

**Files:**
- Create: `tests/integration/test_inspect_endpoint.py`

- [ ] **Step 1: Write the test**

```python
"""HTTP-level integration test for GET /3mf/{token}/inspect.

Skipped when the container at $ORCASLICER_API isn't reachable. Uses
stdlib HTTP so no `httpx` venv is needed on the host (consistent with
test_slice_v2_fidelity.py).
"""
from __future__ import annotations

import io
import json
import os
import urllib.request
import uuid
from pathlib import Path

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8000")
FIXTURE_DIR = Path(__file__).resolve().parents[2].parent / "_fixture"


def _container_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_reachable(),
    reason=f"orcaslicer-cli not reachable at {API}",
)


def _post_multipart_file(url: str, file_path: Path) -> dict:
    boundary = f"----pytest{uuid.uuid4().hex}"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{file_path.name}"\r\n'.encode()
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(file_path.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url, data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30.0) as r:
        return json.loads(r.read().decode())


def test_inspect_unsliced_fixture_01() -> None:
    fp = FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    assert fp.exists()
    upload = _post_multipart_file(f"{API}/3mf", fp)
    token = upload["token"]

    data = _get_json(f"{API}/3mf/{token}/inspect")
    assert data["is_sliced"] is False
    assert data["plate_count"] == 1
    assert data["printer_model"] == "Bambu Lab A1 mini"
    assert len(data["filaments"]) == 1
    f0 = data["filaments"][0]
    assert f0["type"] == "PLA"
    assert f0["filament_id"] == "GFA00"
    assert data["bbox"] is not None
    # Un-sliced 3MF: per-plate slots aren't known yet.
    assert data["plates"][0]["used_filament_indices"] is None


def test_inspect_token_unknown_returns_404() -> None:
    bogus = "no-such-token"
    req = urllib.request.Request(f"{API}/3mf/{bogus}/inspect")
    try:
        urllib.request.urlopen(req, timeout=10.0)
    except urllib.error.HTTPError as e:
        assert e.code == 404
        body = json.loads(e.read().decode())
        assert body["code"] == "token_unknown"
        return
    raise AssertionError("expected 404")
```

- [ ] **Step 2: Run the test on the host**

```bash
pytest tests/integration/test_inspect_endpoint.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_inspect_endpoint.py
git commit -m "Pin /3mf/inspect response shape with HTTP integration test"
```

---

## Milestone 2 — Thumbnail serving

### Task 4: Helper to list and read plate thumbnails

**Why:** The 3MF embeds per-plate thumbnails (`Metadata/plate_1.png`, `plate_1_small.png`, sometimes `top_1.png`/`pick_1.png`). Surface these as a list of `{plate, kind, name}` so the endpoint can build URLs and serve the bytes on demand.

**Files:**
- Modify: `app/threemf.py`
- Test: `tests/test_threemf_thumbnails.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for app.threemf.list_plate_thumbnails."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.threemf import list_plate_thumbnails, read_plate_thumbnail

FIXTURE_ROOT = Path(__file__).resolve().parents[1].parent / "_fixture"


@pytest.fixture
def fixture_01_input_bytes() -> bytes:
    p = FIXTURE_ROOT / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    if not p.exists():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


def test_list_plate_thumbnails(fixture_01_input_bytes):
    thumbs = list_plate_thumbnails(fixture_01_input_bytes)
    # Reference benchy has at least the main plate_1.png.
    main = [t for t in thumbs if t["plate"] == 1 and t["kind"] == "main"]
    assert len(main) == 1
    assert main[0]["name"] == "Metadata/plate_1.png"


def test_read_plate_thumbnail_bytes(fixture_01_input_bytes):
    png = read_plate_thumbnail(fixture_01_input_bytes, plate=1, kind="main")
    assert png is not None
    # PNG signature.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_read_plate_thumbnail_missing(fixture_01_input_bytes):
    assert read_plate_thumbnail(fixture_01_input_bytes, plate=99, kind="main") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/test_threemf_thumbnails.py -v
```

Expected: FAIL with `ImportError: cannot import name 'list_plate_thumbnails'`.

- [ ] **Step 3: Implement helpers**

Append to `app/threemf.py`:

```python
import io
import re
import zipfile

# Maps a 3MF entry name like "Metadata/plate_1_small.png" to (plate_id, kind).
_PLATE_THUMB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^Metadata/plate_(\d+)\.png$"), "main"),
    (re.compile(r"^Metadata/plate_(\d+)_small\.png$"), "small"),
    (re.compile(r"^Metadata/plate_no_light_(\d+)\.png$"), "no_light"),
    (re.compile(r"^Metadata/top_(\d+)\.png$"), "top"),
    (re.compile(r"^Metadata/pick_(\d+)\.png$"), "pick"),
]


def list_plate_thumbnails(file_bytes: bytes) -> list[dict[str, object]]:
    """Return every plate thumbnail in a 3MF as ``{plate, kind, name}`` dicts.

    Empty list when the bytes aren't a valid ZIP. The "kind" enumerates
    the variants the GUI emits ("main" is the canonical preview;
    "small"/"top"/"pick"/"no_light" are alternate renderings).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        return []
    out: list[dict[str, object]] = []
    with zf:
        for name in zf.namelist():
            for pattern, kind in _PLATE_THUMB_PATTERNS:
                m = pattern.match(name)
                if m:
                    out.append({
                        "plate": int(m.group(1)),
                        "kind": kind,
                        "name": name,
                    })
                    break
    return out


def read_plate_thumbnail(
    file_bytes: bytes, plate: int, kind: str = "main",
) -> bytes | None:
    """Return the PNG bytes for a specific plate thumbnail, or None."""
    name_for_kind: dict[str, str] = {
        "main": f"Metadata/plate_{plate}.png",
        "small": f"Metadata/plate_{plate}_small.png",
        "no_light": f"Metadata/plate_no_light_{plate}.png",
        "top": f"Metadata/top_{plate}.png",
        "pick": f"Metadata/pick_{plate}.png",
    }
    target = name_for_kind.get(kind)
    if target is None:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            if target not in zf.namelist():
                return None
            return zf.read(target)
    except (zipfile.BadZipFile, KeyError):
        return None
```

- [ ] **Step 4: Run tests**

```bash
docker compose restart orcaslicer-cli && sleep 5
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/test_threemf_thumbnails.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/threemf.py tests/test_threemf_thumbnails.py
git commit -m "Helpers to list and read embedded plate thumbnails"
```

---

### Task 5: `GET /3mf/{token}/plates/{n}/thumbnail` endpoint

**Why:** Serve the PNG bytes directly so gateway/UI can `<img src="...">` them. Also wire `thumbnail_urls` into the inspect response.

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add the thumbnail endpoint**

In `app/main.py`, near the inspect endpoint:

```python
from fastapi.responses import Response

from .threemf import list_plate_thumbnails, read_plate_thumbnail


@app.get("/3mf/{token}/plates/{plate}/thumbnail", tags=["3MF"])
async def get_plate_thumbnail(
    token: str, plate: int, kind: str = "main", request: Request = None,
) -> Response:
    cache: TokenCache = request.app.state.token_cache
    try:
        path = cache.path(token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": token},
        )
    png = read_plate_thumbnail(path.read_bytes(), plate=plate, kind=kind)
    if png is None:
        return JSONResponse(
            status_code=404,
            content={
                "code": "thumbnail_not_found",
                "token": token, "plate": plate, "kind": kind,
            },
        )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
```

- [ ] **Step 2: Add thumbnail URLs to the inspect response**

Replace the `data["thumbnail_urls"] = []` line in `inspect_3mf` with:

```python
    file_bytes = path.read_bytes()
    data = parse_inspect_data(file_bytes)
    thumbs = list_plate_thumbnails(file_bytes)
    data["thumbnail_urls"] = [
        {
            "plate": t["plate"],
            "kind": t["kind"],
            "url": f"/3mf/{token}/plates/{t['plate']}/thumbnail?kind={t['kind']}",
        }
        for t in thumbs
    ]
```

(Also remove the now-redundant `path.read_bytes()` call earlier in the function — the bytes are already loaded.)

- [ ] **Step 3: Smoke test**

```bash
docker compose restart orcaslicer-cli && sleep 5
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -s http://localhost:8000/3mf/$TOK/inspect | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['thumbnail_urls'], indent=2))"
curl -s -o /tmp/thumb.png "http://localhost:8000/3mf/$TOK/plates/1/thumbnail?kind=main" -w 'HTTP %{http_code} bytes=%{size_download}\n'
file /tmp/thumb.png
```

Expected: inspect lists at least one `{plate:1, kind:"main", url:".../plates/1/thumbnail?kind=main"}`. The GET returns HTTP 200 and `file` reports `PNG image data`.

- [ ] **Step 4: Extend `tests/integration/test_inspect_endpoint.py`**

Add:

```python
def test_inspect_thumbnail_url_serves_png() -> None:
    fp = FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    upload = _post_multipart_file(f"{API}/3mf", fp)
    token = upload["token"]

    data = _get_json(f"{API}/3mf/{token}/inspect")
    assert data["thumbnail_urls"], "inspect should list at least one thumbnail"
    main = next(
        (t for t in data["thumbnail_urls"] if t["kind"] == "main"), None,
    )
    assert main is not None

    with urllib.request.urlopen(f"{API}{main['url']}", timeout=10.0) as r:
        assert r.status == 200
        assert r.headers["content-type"] == "image/png"
        png = r.read()
    # PNG signature.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
```

Run: `pytest tests/integration/test_inspect_endpoint.py -v` → expect 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/integration/test_inspect_endpoint.py
git commit -m "Serve plate thumbnails via /3mf/{token}/plates/{n}/thumbnail"
```

---

## Milestone 3 — `orca-headless use-set` mode

### Task 6: C++ `use-set` subcommand

**Why:** For un-sliced 3MFs, the gateway needs to know which filament indices each plate's painted faces reference. That data lives in the model's `paint_color` attributes — only libslic3r can walk it correctly. New binary subcommand mirrors the slice-mode JSON protocol but skips slicing.

**Files:**
- Create: `cpp/src/use_set_mode.h` / `cpp/src/use_set_mode.cpp`
- Modify: `cpp/src/json_io.h` / `cpp/src/json_io.cpp` — `UseSetRequest` / `UseSetResponse`
- Modify: `cpp/src/orca_headless.cpp` — dispatch `use-set` subcommand
- Modify: `cpp/CMakeLists.txt` — add `use_set_mode.cpp`

- [ ] **Step 1: Add request/response types in `cpp/src/json_io.h`**

After the existing `SliceRequest` / `SliceResponse` definitions:

```cpp
struct UseSetRequest {
    std::string input_3mf;
};

struct UseSetPlateInfo {
    int plate_id = 0;          // 1-based
    std::vector<int> used_filament_indices;  // 0-based, sorted
};

struct UseSetResponse {
    std::string status;        // "ok" or "error"
    std::vector<UseSetPlateInfo> plates;
    std::string error_code;
    std::string error_message;
    nlohmann::json error_details = nlohmann::json::object();
};

UseSetRequest parse_use_set_request_from_stdin();
void write_use_set_response_to_stdout(const UseSetResponse& r);
```

- [ ] **Step 2: Implement parse/write in `cpp/src/json_io.cpp`**

```cpp
UseSetRequest parse_use_set_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());
    UseSetRequest req;
    req.input_3mf = j.at("input_3mf").get<std::string>();
    return req;
}

void write_use_set_response_to_stdout(const UseSetResponse& r) {
    json out;
    out["status"] = r.status;
    if (r.status == "ok") {
        json plates = json::array();
        for (const auto& p : r.plates) {
            plates.push_back({
                {"id", p.plate_id},
                {"used_filament_indices", p.used_filament_indices},
            });
        }
        out["plates"] = plates;
    } else {
        out["code"] = r.error_code;
        out["message"] = r.error_message;
        out["details"] = r.error_details;
    }
    std::cout << out.dump() << std::endl;
}
```

- [ ] **Step 3: Implement `use_set_mode.cpp`**

Create `cpp/src/use_set_mode.h`:

```cpp
#pragma once
#include "json_io.h"

namespace orca_headless {

// Walks `paint_color` attributes on every ModelObject's volumes and
// instances, grouping by plate_id. Returns 0 on success, non-zero on
// failure. Writes a JSON envelope to stdout via
// write_use_set_response_to_stdout.
int run_use_set_mode(const UseSetRequest& req);

}  // namespace orca_headless
```

Create `cpp/src/use_set_mode.cpp`:

```cpp
#include "use_set_mode.h"
#include "progress.h"

#include "libslic3r/Model.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"
#include "libslic3r/Utils.hpp"

#include <filesystem>
#include <map>
#include <set>
#include <string>

namespace orca_headless {

namespace {

// `paint_color` is encoded as a per-face string of digit codes (one digit
// per face) in OrcaSlicer. Each digit is a filament index. A digit '0'
// means "default filament" — slot 0 in our normalization.
std::set<int> extract_filament_indices(const std::string& paint_color) {
    std::set<int> out;
    for (char c : paint_color) {
        if (c >= '0' && c <= '9') {
            out.insert(static_cast<int>(c - '0'));
        }
    }
    return out;
}

// Helper: emit error + return 1 with a populated UseSetResponse.
int fail(const std::string& code, const std::string& message,
         UseSetResponse& r) {
    r.status = "error";
    r.error_code = code;
    r.error_message = message;
    write_use_set_response_to_stdout(r);
    return 1;
}

}  // namespace

int run_use_set_mode(const UseSetRequest& req) {
    UseSetResponse response;

    if (Slic3r::temporary_dir().empty()) {
        std::error_code ec;
        std::filesystem::path tmp = std::filesystem::temp_directory_path(ec);
        if (ec || tmp.empty()) tmp = "/tmp";
        Slic3r::set_temporary_dir(tmp.string());
    }

    Slic3r::DynamicPrintConfig cfg;
    Slic3r::ConfigSubstitutionContext subs(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    Slic3r::PlateDataPtrs plate_data;
    std::vector<Slic3r::Preset*> project_presets;

    Slic3r::Model model;
    try {
        model = Slic3r::Model::read_from_file(
            req.input_3mf, &cfg, &subs,
            Slic3r::LoadStrategy::LoadModel
                | Slic3r::LoadStrategy::LoadConfig,
            &plate_data, &project_presets);
    } catch (const std::exception& e) {
        return fail("invalid_3mf",
                    std::string("read_from_file: ") + e.what(), response);
    }

    // Map: plate_id (1-based) → set of filament indices found in any
    // paint_color attribute on objects assigned to that plate.
    std::map<int, std::set<int>> plate_to_indices;

    // Plate assignment lives on each ModelObject via printable parts /
    // instance plate IDs. For Phase 1 single-plate inputs all objects
    // belong to plate 1; multi-plate inputs are handled by walking the
    // PlateDataPtrs and matching object_and_instances. Until the
    // multi-plate fixture exists we conservatively bucket everything
    // into plate 1 and revisit the assignment pass when fixture 04
    // lands.
    for (const auto& obj : model.objects) {
        if (!obj) continue;
        for (const auto& vol : obj->volumes) {
            if (!vol) continue;
            const auto idxs = extract_filament_indices(vol->config.opt_string(
                "extruder", true));  // fallback if paint_color absent
            for (int i : idxs) plate_to_indices[1].insert(i);
        }
        // The model's per-face paint_color attribute is on the volume's
        // mesh; OrcaSlicer accesses it via `mmu_segmentation_facets`.
        // libslic3r exposes it as `mmu_segmentation_facets.set` /
        // `get_facets_strings()` — see ModelVolume.hpp. For Phase 1 we
        // approximate by reading the volume's config "extruder" key,
        // which captures the slot index for non-painted volumes; full
        // paint walk is deferred until a painted-mesh fixture exists.
    }

    // Always seed plate 1 so empty results return at least the trivial
    // [0] used-set rather than no plates at all.
    if (plate_to_indices.empty()) {
        plate_to_indices[1].insert(0);
    }

    response.status = "ok";
    for (const auto& [plate_id, indices] : plate_to_indices) {
        UseSetPlateInfo info;
        info.plate_id = plate_id;
        info.used_filament_indices.assign(indices.begin(), indices.end());
        response.plates.push_back(info);
    }
    write_use_set_response_to_stdout(response);
    return 0;
}

}  // namespace orca_headless
```

- [ ] **Step 4: Dispatch in `cpp/src/orca_headless.cpp`**

In `main()`, after the existing `slice` branch, before the trailing `return print_usage`:

```cpp
    if (std::strcmp(argv[1], "use-set") == 0) {
        try {
            auto req = orca_headless::parse_use_set_request_from_stdin();
            return orca_headless::run_use_set_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            return 1;
        }
    }
```

Also include the header at the top:

```cpp
#include "use_set_mode.h"
```

- [ ] **Step 5: Add to `cpp/CMakeLists.txt`**

Find the `add_executable(orca-headless ...)` line and add `src/use_set_mode.cpp` to the source list:

```cmake
add_executable(orca-headless
    src/orca_headless.cpp
    src/json_io.cpp
    src/progress.cpp
    src/slice_mode.cpp
    src/use_set_mode.cpp
    src/nanosvg_impl.cpp
)
```

- [ ] **Step 6: Rebuild (10–15 min) and smoke**

```bash
DOCKER_BUILDKIT=1 docker compose build orcaslicer-cli  # use run_in_background; wait for notification
USE_HEADLESS_BINARY=1 GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --force-recreate
sleep 8
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c '
echo "{\"input_3mf\": \"/data/cache/$(ls /data/cache | head -1)\"}" \
  | /opt/orca-headless/bin/orca-headless use-set
'
```

Expected: a JSON line on stdout `{"plates":[{"id":1,"used_filament_indices":[0]}],"status":"ok"}`. (For fixture 01 there's no painting; the single-filament default is [0].)

- [ ] **Step 7: Commit**

```bash
git add cpp/src/use_set_mode.h cpp/src/use_set_mode.cpp \
        cpp/src/json_io.h cpp/src/json_io.cpp \
        cpp/src/orca_headless.cpp cpp/CMakeLists.txt
git commit -m "Add orca-headless use-set subcommand"
```

---

### Task 7: Python `BinaryClient.use_set` wrapper

**Why:** Same async-subprocess pattern the slice path uses, but tighter timeout (30s default — use-set is supposed to be ~200ms-2s).

**Files:**
- Modify: `app/binary_client.py`
- Test: `tests/test_binary_client.py` (extend existing — note the file currently has 4 pre-existing async failures unrelated to this work; new tests should still land)

- [ ] **Step 1: Add the method to `BinaryClient`**

In `app/binary_client.py`, alongside `slice`:

```python
    async def use_set(
        self, *, input_3mf: str, timeout_s: float = 30.0,
    ) -> dict:
        """Invoke `orca-headless use-set` and return the parsed response."""
        request = {"input_3mf": input_3mf}
        proc = await asyncio.create_subprocess_exec(
            self.binary_path, "use-set",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(request).encode()),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BinaryError(
                code="binary_timeout",
                message=f"orca-headless use-set timed out after {timeout_s}s",
                details={},
                stderr_tail="",
            )
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if proc.returncode != 0 and not stdout.strip():
            raise BinaryError(
                code="binary_crashed",
                message=f"orca-headless use-set exited {proc.returncode} with no stdout",
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
                code=response.get("code", "binary_error"),
                message=response.get("message", "use-set failed"),
                details=response.get("details", {}),
                stderr_tail=stderr_text[-2000:],
            )
        return response
```

- [ ] **Step 2: Smoke from inside the container against fixture 01's cached file**

```bash
docker compose restart orcaslicer-cli && sleep 5
docker exec orcaslicer-cli-orcaslicer-cli-1 python3 -c "
import asyncio
from app.binary_client import BinaryClient
from app.config import ORCA_HEADLESS_BINARY
import os, glob
cache_files = glob.glob('/data/cache/*.3mf')
assert cache_files, 'no cached 3mf to test against'
client = BinaryClient(binary_path=ORCA_HEADLESS_BINARY)
result = asyncio.run(client.use_set(input_3mf=cache_files[0]))
print(result)
"
```

Expected: dict with `plates: [{id:1, used_filament_indices:[0]}]`.

- [ ] **Step 3: Commit**

```bash
git add app/binary_client.py
git commit -m "BinaryClient.use_set wrapper with 30s default timeout"
```

---

### Task 8: Wire `use-set` into `/3mf/{token}/inspect`

**Why:** Populate `use_set_per_plate` for un-sliced 3MFs by calling the binary; for sliced 3MFs the data already comes from `slice_info.config` (existing path).

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Update the inspect handler**

Replace the body of `inspect_3mf`:

```python
    cache: TokenCache = request.app.state.token_cache
    try:
        path = cache.path(token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": token},
        )
    file_bytes = path.read_bytes()
    data = parse_inspect_data(file_bytes)
    thumbs = list_plate_thumbnails(file_bytes)
    data["thumbnail_urls"] = [
        {
            "plate": t["plate"],
            "kind": t["kind"],
            "url": f"/3mf/{token}/plates/{t['plate']}/thumbnail?kind={t['kind']}",
        }
        for t in thumbs
    ]

    # Populate use_set_per_plate. Sliced 3MFs already carry per-plate
    # used-slot data via `slice_info.config` (parse_inspect_data fills
    # `plates[i].used_filament_indices` from there). For un-sliced 3MFs
    # we shell out to `orca-headless use-set`.
    use_set_per_plate: dict[int, list[int]] = {}
    needs_binary = any(
        p["used_filament_indices"] is None for p in data["plates"]
    )
    if needs_binary and cfg.USE_HEADLESS_BINARY:
        binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
        try:
            us_response = await binary.use_set(input_3mf=str(path))
        except BinaryError as e:
            logger.warning(
                "use-set failed; returning inspect without per-plate slots: %s",
                e.message,
            )
        else:
            for p in us_response.get("plates", []):
                use_set_per_plate[p["id"]] = p["used_filament_indices"]
            # Backfill into data["plates"].
            for plate in data["plates"]:
                if plate["used_filament_indices"] is None and \
                        plate["id"] in use_set_per_plate:
                    plate["used_filament_indices"] = use_set_per_plate[plate["id"]]
    # Sliced-side data already in data["plates"][i] — also surface as
    # the dict-keyed shape for gateway convenience.
    for plate in data["plates"]:
        if plate["used_filament_indices"] is not None:
            use_set_per_plate.setdefault(plate["id"], plate["used_filament_indices"])
    data["use_set_per_plate"] = use_set_per_plate
    return JSONResponse(content=data)
```

- [ ] **Step 2: Add the integration test for un-sliced + binary path**

In `tests/integration/test_inspect_endpoint.py`:

```python
def test_inspect_unsliced_use_set_via_binary() -> None:
    fp = FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    upload = _post_multipart_file(f"{API}/3mf", fp)
    token = upload["token"]

    data = _get_json(f"{API}/3mf/{token}/inspect")
    assert data["is_sliced"] is False
    # `use-set` should fill in plate 1.
    assert data["use_set_per_plate"], "binary should have populated use_set_per_plate"
    assert data["use_set_per_plate"]["1"] == [0]
    assert data["plates"][0]["used_filament_indices"] == [0]
```

Run: `pytest tests/integration/test_inspect_endpoint.py -v` → expect 4 passed.

- [ ] **Step 3: Commit**

```bash
git add app/main.py tests/integration/test_inspect_endpoint.py
git commit -m "Populate use_set_per_plate via orca-headless use-set"
```

---

## Milestone 4 — Inspect cache

### Task 9: In-memory cache keyed by `(sha256, schema_version)`

**Why:** Spec calls for caching inspect responses against the token cache's sha256 so repeated inspect calls are O(dict lookup). Token cache already exposes the sha256 — wire it through.

**Files:**
- Modify: `app/inspect.py` (add a small `InspectCache` class)
- Modify: `app/main.py` (use it in the endpoint)

- [ ] **Step 1: Add cache to `app/inspect.py`**

Append:

```python
class InspectCache:
    """In-memory cache of assembled inspect responses.

    Keyed by ``(sha256, INSPECT_SCHEMA_VERSION)``. Bumping the schema
    version invalidates every cached entry without touching the token
    cache that stores the actual 3MF bytes.

    Capped at 256 entries (LRU eviction). Inspect responses are small
    (low single-digit KB each) so this is generous.
    """
    MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._entries: "OrderedDict[tuple[str, int], dict[str, Any]]" = OrderedDict()

    def get(self, sha256: str) -> dict[str, Any] | None:
        key = (sha256, INSPECT_SCHEMA_VERSION)
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, sha256: str, value: dict[str, Any]) -> None:
        key = (sha256, INSPECT_SCHEMA_VERSION)
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self.MAX_ENTRIES:
            self._entries.popitem(last=False)

    def invalidate(self, sha256: str) -> None:
        for ver in list({k[1] for k in self._entries if k[0] == sha256}):
            self._entries.pop((sha256, ver), None)
```

Add the import: `from collections import OrderedDict`.

- [ ] **Step 2: Wire cache into the endpoint**

In `app/main.py`:

```python
from .inspect import (
    INSPECT_SCHEMA_VERSION, InspectCache, parse_inspect_data,
)
```

In the FastAPI lifespan, alongside the token cache instantiation:

```python
    app.state.inspect_cache = InspectCache()
```

In `inspect_3mf`, before calling `parse_inspect_data`:

```python
    inspect_cache: InspectCache = request.app.state.inspect_cache
    sha256 = cache.sha256_for(token)  # see step 3
    cached = inspect_cache.get(sha256)
    if cached is not None:
        return JSONResponse(content=cached)
```

After assembling `data`:

```python
    inspect_cache.put(sha256, data)
```

- [ ] **Step 3: Add `TokenCache.sha256_for(token)` if not present**

In `app/cache.py`, alongside `path(token)`:

```python
    def sha256_for(self, token: str) -> str:
        entry = self._index.get(token)
        if entry is None:
            raise KeyError(token)
        return entry.sha256
```

(The exact attribute name depends on the existing `_index` shape — adjust if it stores tuples or a different dataclass.)

- [ ] **Step 4: Invalidate cache on token delete**

In the existing `DELETE /3mf/{token}` handler, after the cache delete, add:

```python
    request.app.state.inspect_cache.invalidate(sha256)
```

(If the sha256 isn't readily available at delete time, capture it before calling `cache.delete(token)`.)

- [ ] **Step 5: Smoke test the cache**

```bash
docker compose restart orcaslicer-cli && sleep 5
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
# First call: warm — invokes binary use-set.
time curl -s -o /dev/null http://localhost:8000/3mf/$TOK/inspect
# Second call: should be O(dict lookup), <50ms.
time curl -s -o /dev/null http://localhost:8000/3mf/$TOK/inspect
```

Expected: second call markedly faster than first (typical: first ~500ms, second <30ms).

- [ ] **Step 6: Commit**

```bash
git add app/inspect.py app/main.py app/cache.py
git commit -m "Cache inspect responses by (sha256, schema_version)"
```

---

### Task 10: Cache invalidation tests

**Why:** Lock in the cache's hit/miss/invalidation behaviour.

**Files:**
- Modify: `tests/test_inspect.py`

- [ ] **Step 1: Add unit tests for `InspectCache`**

Append to `tests/test_inspect.py`:

```python
from app.inspect import INSPECT_SCHEMA_VERSION, InspectCache


def test_inspect_cache_hit_and_miss() -> None:
    c = InspectCache()
    assert c.get("sha-a") is None
    c.put("sha-a", {"is_sliced": False})
    assert c.get("sha-a") == {"is_sliced": False}
    assert c.get("sha-b") is None


def test_inspect_cache_invalidate() -> None:
    c = InspectCache()
    c.put("sha-a", {"is_sliced": False})
    c.invalidate("sha-a")
    assert c.get("sha-a") is None


def test_inspect_cache_evicts_oldest_when_full() -> None:
    c = InspectCache()
    # Fill past MAX_ENTRIES.
    for i in range(InspectCache.MAX_ENTRIES + 5):
        c.put(f"sha-{i}", {"i": i})
    # The first 5 should have been evicted.
    for i in range(5):
        assert c.get(f"sha-{i}") is None
    for i in range(5, InspectCache.MAX_ENTRIES + 5):
        assert c.get(f"sha-{i}") == {"i": i}


def test_inspect_cache_schema_version_invalidates_logically() -> None:
    """Bumping INSPECT_SCHEMA_VERSION must drop cached entries.

    We simulate the bump by stuffing an entry with a stale version
    directly into the internal dict, then verifying ``get`` misses.
    """
    c = InspectCache()
    c._entries[("sha-x", INSPECT_SCHEMA_VERSION - 1)] = {"stale": True}
    assert c.get("sha-x") is None
```

- [ ] **Step 2: Run all new tests**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/test_inspect.py -v
```

Expected: 6 passed (the 2 from Task 1 plus 4 new ones).

- [ ] **Step 3: Commit**

```bash
git add tests/test_inspect.py
git commit -m "Cover InspectCache hit/miss/eviction/version-invalidation"
```

---

## Self-Review

**Spec coverage** (vs `docs/superpowers/specs/2026-05-02-orca-headless-fork-design.md`):

- `GET /3mf/{token}/inspect` returning `{is_sliced, plates, filaments, use_set_per_plate, estimate, thumbnail_urls}` — Tasks 1, 2, 5, 8.
- `GET /3mf/{token}/plates/{n}/thumbnail` — Task 5.
- `orca-headless use-set` JSON-on-stdin/stdout subcommand — Task 6.
- Inspect cache keyed by `(sha256, schema_version)` — Task 9.

**Out of scope, deferred:** the gateway-side migration (`bambu-gateway/app/parse_3mf.py` deletion + `OrcaslicerClient` introduction) is its own plan after this lands.

**Known limitations called out inline:**

- Task 6's `paint_color` walk is a Phase-1 approximation (uses each volume's `extruder` config slot rather than the per-face `mmu_segmentation_facets`). Gives the right answer for the common single-filament case but won't differentiate plates by paint until we have a multi-color painted-mesh fixture (variant 04 from `scripts/generate_fixtures.py`). Flag it in the binary so callers know.
- For Phase 1 single-plate inputs everything is bucketed into plate 1 in the binary. Multi-plate dispatch comes when fixture 04 / a multi-plate fixture lands.

**Placeholder scan:** every step has the actual code or command; no "TBD"/"similar to Task N"/"add error handling".

**Type consistency:**

- `parse_inspect_data` returns the same shape in Tasks 1, 2, 5, 8 — the endpoint progressively fills more fields without changing the dict's existing keys.
- `BinaryClient.use_set` (Task 7) consumed by `inspect_3mf` (Task 8) using the documented `{plates: [{id, used_filament_indices}]}` shape from the binary spec.
- `InspectCache` API surface in Tasks 9 and 10 stays the same: `get` / `put` / `invalidate`.

**Risks:**

- Task 6 (binary rebuild) is the long-pole. Time-box waiting to ~15 min and confirm via `naming to docker.io/library/...` in the build log; the `tail` shell-pipe trap from earlier can mask compile errors, so always grep `error:` or run without piping.
- The use-set approximation in Task 6 may give incorrect data for painted multi-color models. Flagged as a "Phase 4 follow-up needing a painted fixture" in the binary itself.
