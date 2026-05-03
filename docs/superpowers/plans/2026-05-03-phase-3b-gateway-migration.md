# Phase 3b: `bambu-gateway` migrates 3MF parsing to `orcaslicer-cli` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete every line of 3MF parsing in `bambu-gateway` and replace it with HTTP calls to `orcaslicer-cli`'s Phase 3 endpoints. After this lands, `bambu-gateway/app/parse_3mf.py` and `bambu-gateway/app/print_estimate.py` are gone, the gateway never opens a `.3mf` ZIP, and the OrcaSlicer-derived parsing logic lives in exactly one place.

**Architecture:** Extend the existing `bambu-gateway/app/slicer_client.py::SlicerClient` with `upload_3mf`, `inspect`, and `delete_token` methods that hit `POST /3mf`, `GET /3mf/{token}/inspect`, and `DELETE /3mf/{token}` on `orcaslicer-cli`. Replace `parse_3mf(data, plate_id)` with a thin adapter that uploads bytes, inspects, optionally deletes, and maps the response to the existing `ThreeMFInfo` Pydantic shape so call sites don't change. Replace `extract_print_estimate` similarly — most call sites can read the estimate straight from the slice response (Phase 1 already returns it). Remove deletes are explicit so the upload cache doesn't fill up.

**Tech Stack:** Python 3.12 (httpx async client, FastAPI, Pydantic), pytest, Docker.

**Where this plan lives, where it executes:** This plan file is committed to `orcaslicer-cli/docs/superpowers/plans/`. **Execution happens in the `bambu-gateway` repo at `/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/bambu-gateway`** — every task touches files under that path. The branch on both repos is `feat/orca-headless`.

**Repo conventions to honor (gateway side):**
- Existing `SlicerClient` in `app/slicer_client.py` is the single client to `orcaslicer-cli`. Don't create a parallel client class — extend this one.
- Pydantic models in `app/models.py`. The wire shape that downstream UI consumes is `ThreeMFInfo` (see `app/models.py:309`); preserve it.
- httpx client lifecycle is owned by FastAPI's lifespan in `app/main.py:173` (`slicer_client = SlicerClient(settings.orcaslicer_api_url)`). Reuse that instance.
- Settings live in `app/config.py`; new env vars go there.
- Commits follow CLAUDE.md style: subject ≤ 60, body wrapped at 140, code names in backticks.

**Out of scope:**
- Any UI / iOS app changes. The gateway's response shape (`ThreeMFInfo`) stays identical — only the implementation moves behind the `parse_3mf` function name.
- Changes to `orcaslicer-cli` itself. The Phase 3 endpoints (`POST /3mf`, `GET /3mf/{token}/inspect`, `DELETE /3mf/{token}`, `GET /3mf/{token}/plates/{n}/thumbnail`) are sufficient as-is.
- Phase 2 cleanup of `orcaslicer-cli` (deleting `app/normalize.py` etc.) — that comes after this lands and the legacy `/slice` route can be retired.

---

## Milestones

1. **`SlicerClient.inspect`** (Tasks 1–3) — upload, inspect, delete methods and a fake-transport pytest.
2. **`parse_3mf` migrated to network** (Tasks 4–6) — adapter mapping inspect → `ThreeMFInfo`; replace `parse_3mf` body; smoke against live `orcaslicer-cli`.
3. **`extract_print_estimate` retired** (Tasks 7–8) — slice response already carries the estimate; delete the helper and the file's redundant XML walking.
4. **Cleanup** (Tasks 9–10) — delete `parse_3mf.py` and `print_estimate.py`; update tests + README.

Each milestone ends green: gateway test suite passes, manual smoke against the live container works.

---

## Files to be created or modified

**Modified files (gateway):**
- `app/slicer_client.py` — add `upload_3mf`, `inspect`, `delete_token` methods; tighten existing imports
- `app/main.py` — pass `slicer_client` into `parse_3mf`; remove direct `parse_3mf`/`extract_print_estimate` imports once adapter is in
- `app/slice_jobs.py` — replace `extract_print_estimate(result_bytes)` call with reading the estimate that's already in the slice response
- `app/models.py` — none expected; `ThreeMFInfo` shape preserved
- `app/config.py` — none expected; `orcaslicer_api_url` already exists

**Replaced files (gateway):**
- `app/parse_3mf.py` — body becomes a thin async adapter calling `slicer_client.inspect`; eventually inlined into call sites and the file deleted

**Deleted files (gateway, end of plan):**
- `app/parse_3mf.py` (322 lines)
- `app/print_estimate.py` (86 lines)
- Any tests targeting their internals (kept tests that target `parse_3mf`'s public behaviour migrate to integration form)

**New tests:**
- `tests/test_slicer_client_inspect.py` — fake `httpx.MockTransport` covers upload+inspect+delete + error paths
- `tests/integration/test_parse_3mf_via_orcaslicer.py` — opt-in HTTP test against a live `orcaslicer-cli` at `$ORCASLICER_API_URL`

---

## Milestone 1 — `SlicerClient` gets inspect/upload/delete

### Task 1: Add `upload_3mf`, `inspect`, `delete_token` to `SlicerClient`

**Why:** `SlicerClient` already speaks to `orcaslicer-cli` for slice + profiles; adding the inspect surface keeps gateway → slicer comms in one place. Without these methods every caller would build raw httpx requests, fragmenting the contract.

**Files:**
- Modify: `bambu-gateway/app/slicer_client.py`

- [ ] **Step 1: Read the existing class shape**

```bash
cd /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/bambu-gateway
grep -n '    async def \|    def \|class SlicerClient' app/slicer_client.py
```

The class owns an `httpx.AsyncClient` self._client (lazy). Reuse it; don't create a new one per request.

- [ ] **Step 2: Add the three methods**

In `app/slicer_client.py`, alongside `slice` and `get_profiles`:

```python
    async def upload_3mf(self, data: bytes, *, filename: str = "input.3mf") -> dict:
        """POST /3mf — upload bytes, get a token + sha256.

        Returns the JSON response: ``{token, sha256, size, evicts}``.
        """
        files = {"file": (filename, data, "application/octet-stream")}
        async with self._async_client() as client:
            r = await client.post(f"{self._base_url}/3mf", files=files, timeout=120.0)
            r.raise_for_status()
            return r.json()

    async def inspect(self, token: str) -> dict:
        """GET /3mf/{token}/inspect — return the structured summary.

        Returns the JSON response with ``plates``, ``filaments``,
        ``estimate``, ``bbox``, ``thumbnail_urls``, ``use_set_per_plate``,
        and ``schema_version``.
        """
        async with self._async_client() as client:
            r = await client.get(f"{self._base_url}/3mf/{token}/inspect", timeout=60.0)
            r.raise_for_status()
            return r.json()

    async def delete_token(self, token: str) -> bool:
        """DELETE /3mf/{token} — drop the cached file.

        Returns True when the slicer confirmed the delete, False on 404
        (token already evicted). Other HTTP errors raise.
        """
        async with self._async_client() as client:
            r = await client.delete(f"{self._base_url}/3mf/{token}", timeout=10.0)
            if r.status_code == 404:
                return False
            r.raise_for_status()
            return True
```

If `_async_client` is named differently in the existing class (e.g. `_client`), match the established pattern. Don't introduce a new client constructor here.

- [ ] **Step 3: Smoke against the live container**

`orcaslicer-cli` runs at `http://localhost:8000` in dev. Smoke from a Python REPL inside the gateway repo:

```bash
docker compose up -d  # if gateway has a compose; else just run app via uvicorn
python3 -c "
import asyncio, os
from app.slicer_client import SlicerClient
async def main():
    c = SlicerClient(os.environ.get('ORCASLICER_API_URL', 'http://localhost:8000'))
    fp = '/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf'
    with open(fp, 'rb') as f: data = f.read()
    up = await c.upload_3mf(data)
    print('uploaded:', up['token'])
    insp = await c.inspect(up['token'])
    print('plates:', len(insp['plates']), 'filaments:', len(insp['filaments']))
    deleted = await c.delete_token(up['token'])
    print('deleted:', deleted)
asyncio.run(main())
"
```

Expected: `plates: 1 filaments: 1`, `deleted: True`.

- [ ] **Step 4: Commit**

```bash
git add app/slicer_client.py
git commit -m "Add upload_3mf, inspect, delete_token to SlicerClient"
```

---

### Task 2: Fake-transport pytest for the new methods

**Why:** Integration smoke (Task 1 step 3) is opt-in. Lock in the wire-level contract with a unit-style test using `httpx.MockTransport` so CI catches breaking changes whether or not a live slicer is reachable.

**Files:**
- Create: `bambu-gateway/tests/test_slicer_client_inspect.py`

- [ ] **Step 1: Write the test**

```python
"""Unit tests for the inspect surface of SlicerClient.

Uses ``httpx.MockTransport`` so the tests run without a live
``orcaslicer-cli`` container.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.slicer_client import SlicerClient


def _mock_transport(handlers: dict[tuple[str, str], httpx.Response]):
    def _handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in handlers:
            return httpx.Response(404, json={"code": "unmocked", "key": list(key)})
        resp = handlers[key]
        return resp
    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_upload_inspect_delete_roundtrip(monkeypatch):
    transport = _mock_transport({
        ("POST", "/3mf"): httpx.Response(
            200, json={"token": "tok-abc", "sha256": "deadbeef", "size": 100, "evicts": []},
        ),
        ("GET", "/3mf/tok-abc/inspect"): httpx.Response(
            200, json={
                "schema_version": 2,
                "is_sliced": False,
                "plate_count": 1,
                "plates": [{"id": 1, "name": "", "used_filament_indices": [0]}],
                "filaments": [],
                "estimate": None,
                "bbox": None,
                "printer_model": "",
                "printer_variant": "",
                "curr_bed_type": "",
                "thumbnail_urls": [],
                "use_set_per_plate": {"1": [0]},
            },
        ),
        ("DELETE", "/3mf/tok-abc"): httpx.Response(
            204, json=None,
        ),
    })

    # SlicerClient should accept a transport for testing — see Step 2 if
    # the constructor doesn't yet take one.
    client = SlicerClient("http://test", transport=transport)
    upload = await client.upload_3mf(b"\x50\x4b\x03\x04dummy")
    assert upload["token"] == "tok-abc"

    insp = await client.inspect("tok-abc")
    assert insp["plate_count"] == 1
    assert insp["plates"][0]["used_filament_indices"] == [0]

    deleted = await client.delete_token("tok-abc")
    assert deleted is True


@pytest.mark.asyncio
async def test_delete_token_returns_false_on_404():
    transport = _mock_transport({
        ("DELETE", "/3mf/tok-gone"): httpx.Response(
            404, json={"code": "token_unknown"},
        ),
    })
    client = SlicerClient("http://test", transport=transport)
    assert await client.delete_token("tok-gone") is False


@pytest.mark.asyncio
async def test_inspect_propagates_http_errors():
    transport = _mock_transport({
        ("GET", "/3mf/tok/inspect"): httpx.Response(500, json={"code": "boom"}),
    })
    client = SlicerClient("http://test", transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        await client.inspect("tok")
```

- [ ] **Step 2: Make `SlicerClient` accept a `transport=` kwarg if it doesn't already**

The fake-transport pattern requires the client to plumb the transport through to `httpx.AsyncClient(transport=...)`. If the existing constructor doesn't take `transport`, add it — default `None`, store on `self`, pass to every `AsyncClient` instantiation. Keep it backwards compatible (existing call sites don't change).

- [ ] **Step 3: Run the test**

```bash
cd /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/bambu-gateway
pytest tests/test_slicer_client_inspect.py -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_slicer_client_inspect.py app/slicer_client.py
git commit -m "Pin SlicerClient inspect/upload/delete with mock transport"
```

---

### Task 3: Integration test against live slicer (opt-in)

**Why:** Catches contract drift between `orcaslicer-cli` and `bambu-gateway` that mock tests can't see.

**Files:**
- Create: `bambu-gateway/tests/integration/__init__.py` (empty)
- Create: `bambu-gateway/tests/integration/test_slicer_inspect_live.py`

- [ ] **Step 1: Write the integration test**

```python
"""Live HTTP test: gateway's SlicerClient.inspect against running orcaslicer-cli.

Skipped when ``$ORCASLICER_API_URL`` isn't reachable. Uses the shared
benchy fixture in ``../_fixture/01``.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from app.slicer_client import SlicerClient

API = os.environ.get("ORCASLICER_API_URL", "http://localhost:8000")
FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "_fixture"
    / "01"
    / "reference-benchy-orca-no-filament-custom-settings.3mf"
)


def _reachable() -> bool:
    try:
        return httpx.get(f"{API}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason=f"orcaslicer-cli unreachable at {API}",
)


@pytest.mark.asyncio
async def test_inspect_fixture_01_via_client():
    if not FIXTURE.exists():
        pytest.skip(f"missing fixture: {FIXTURE}")
    client = SlicerClient(API)
    data = FIXTURE.read_bytes()
    upload = await client.upload_3mf(data)
    try:
        insp = await client.inspect(upload["token"])
        assert insp["is_sliced"] is False
        assert insp["plate_count"] == 1
        assert insp["printer_model"] == "Bambu Lab A1 mini"
        assert insp["plates"][0]["used_filament_indices"] == [0]
        assert insp["filaments"][0]["type"] == "PLA"
        assert insp["filaments"][0]["filament_id"] == "GFA00"
    finally:
        await client.delete_token(upload["token"])
```

- [ ] **Step 2: Run on host**

```bash
pytest tests/integration/test_slicer_inspect_live.py -v
```

Expected: 1 passed (or skipped if container isn't running).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/__init__.py tests/integration/test_slicer_inspect_live.py
git commit -m "Live integration test for SlicerClient.inspect"
```

---

## Milestone 2 — Replace `parse_3mf` with network calls

### Task 4: Adapter that maps inspect response → `ThreeMFInfo`

**Why:** Five `app/main.py` call sites consume `ThreeMFInfo` (Pydantic). Keep that shape stable and the call sites zero-diff for now; only the body of `parse_3mf` changes from "open ZIP, read XML" to "upload, inspect, adapt".

**Files:**
- Modify: `bambu-gateway/app/parse_3mf.py` — replace body with adapter; keep `parse_3mf` signature exactly

- [ ] **Step 1: Inspect every existing field in `ThreeMFInfo`**

```bash
grep -B0 -A10 "^class ThreeMFInfo\b\|^class PlateInfo\b\|^class FilamentInfo\b\|^class PrinterInfo\b\|^class PrintProfileInfo\b\|^class PlateObject\b" app/models.py
```

Confirm fields:
- `ThreeMFInfo.plates: list[PlateInfo]`
- `ThreeMFInfo.filaments: list[FilamentInfo]`
- `ThreeMFInfo.print_profile: PrintProfileInfo`
- `ThreeMFInfo.printer: PrinterInfo`
- `ThreeMFInfo.has_gcode: bool`
- `ThreeMFInfo.bed_type: str`
- `PlateInfo.id, name, objects: list[PlateObject], thumbnail (base64), used_filament_indices: list[int] | None`
- `PlateObject.id, name`
- `FilamentInfo.index, type, color, setting_id, used: bool`

- [ ] **Step 2: Map inspect response → `ThreeMFInfo`**

Replace `parse_3mf.py` with:

```python
"""Async adapter from orcaslicer-cli /3mf/inspect → ThreeMFInfo.

Replaces the in-process ZIP/XML parser. Behaviour preserved:
- ``plate_id=None`` returns every plate's ``used_filament_indices``;
- ``plate_id=N`` returns only that plate's slot data;
- ``FilamentInfo.used`` reflects union (or single-plate) as before.

Thumbnails: the inspect endpoint returns URLs; we fetch them and
base64-encode for the existing ``PlateInfo.thumbnail`` field. This adds
one round trip per plate but keeps the wire shape unchanged. Migration
to URL-based thumbnails is a separate Phase 4 task.
"""
from __future__ import annotations

import base64
from typing import Optional

import httpx

from app.models import (
    FilamentInfo,
    PlateInfo,
    PlateObject,
    PrinterInfo,
    PrintProfileInfo,
    ThreeMFInfo,
)
from app.slicer_client import SlicerClient


async def parse_3mf_via_slicer(
    data: bytes,
    slicer: SlicerClient,
    *,
    plate_id: Optional[int] = None,
    include_thumbnails: bool = True,
) -> ThreeMFInfo:
    """Upload + inspect + adapt. Always deletes the upload token before returning."""
    upload = await slicer.upload_3mf(data)
    token = upload["token"]
    try:
        insp = await slicer.inspect(token)
        thumbnails: dict[int, str] = {}
        if include_thumbnails:
            thumbnails = await _fetch_main_thumbnails(slicer, token, insp)
        return _adapt(insp, plate_id=plate_id, thumbnails=thumbnails)
    finally:
        await slicer.delete_token(token)


async def _fetch_main_thumbnails(
    slicer: SlicerClient, token: str, insp: dict,
) -> dict[int, str]:
    """Fetch one ``main`` PNG per plate, return ``{plate_id: base64-str}``.

    Skips silently when a plate has no main thumbnail — older 3MFs that
    only carry ``small`` variants surface as empty strings, matching the
    pre-migration behaviour for those files.
    """
    out: dict[int, str] = {}
    for entry in insp.get("thumbnail_urls", []):
        if entry.get("kind") != "main":
            continue
        plate = int(entry["plate"])
        if plate in out:
            continue
        url = f"{slicer._base_url}{entry['url']}"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=30.0)
            if r.status_code == 200:
                out[plate] = base64.b64encode(r.content).decode("ascii")
    return out


def _adapt(
    insp: dict,
    *,
    plate_id: Optional[int],
    thumbnails: dict[int, str],
) -> ThreeMFInfo:
    plates: list[PlateInfo] = []
    for p in insp.get("plates", []):
        pid = int(p["id"])
        plates.append(PlateInfo(
            id=pid,
            name=p.get("name", "") or "",
            objects=[],  # Populated below from inspect.plates[].objects when surfaced.
            thumbnail=thumbnails.get(pid, ""),
            used_filament_indices=p.get("used_filament_indices"),
        ))

    filaments: list[FilamentInfo] = []
    for f in insp.get("filaments", []):
        filaments.append(FilamentInfo(
            index=int(f["slot"]),
            type=f.get("type", "") or "",
            color=f.get("color", "") or "",
            setting_id=f.get("settings_id", "") or "",
            used=True,  # Set below.
        ))

    # Mirror per-plate selection onto FilamentInfo.used.
    if plate_id is not None:
        selected = next((p for p in plates if p.id == plate_id), None)
        target_indices = (
            selected.used_filament_indices
            if selected and selected.used_filament_indices is not None
            else [f.index for f in filaments]
        )
    else:
        union: set[int] = set()
        any_known = False
        for p in plates:
            if p.used_filament_indices is not None:
                union |= set(p.used_filament_indices)
                any_known = True
        target_indices = list(union) if any_known else [f.index for f in filaments]
    target_set = set(target_indices)
    for f in filaments:
        f.used = f.index in target_set

    return ThreeMFInfo(
        plates=plates,
        filaments=filaments,
        print_profile=PrintProfileInfo(
            print_settings_id="",  # Inspect doesn't currently return this.
            layer_height="",
        ),
        printer=PrinterInfo(
            printer_settings_id="",
            printer_model=insp.get("printer_model", "") or "",
            nozzle_diameter=insp.get("printer_variant", "") or "",
        ),
        has_gcode=bool(insp.get("is_sliced", False)),
        bed_type=insp.get("curr_bed_type", "") or "",
    )
```

- [ ] **Step 3: Add unit tests for the adapter**

Create `tests/test_parse_3mf_adapter.py`:

```python
"""Unit tests for _adapt() — the inspect→ThreeMFInfo mapping."""
from __future__ import annotations

from app.parse_3mf import _adapt


def _fake_inspect(*, sliced: bool, plates: list[dict], filaments: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "is_sliced": sliced,
        "plate_count": len(plates),
        "plates": plates,
        "filaments": filaments,
        "estimate": None,
        "bbox": None,
        "printer_model": "Bambu Lab A1 mini",
        "printer_variant": "0.4",
        "curr_bed_type": "Textured PEI Plate",
        "thumbnail_urls": [],
        "use_set_per_plate": {},
    }


def test_adapter_unsliced_single_plate():
    insp = _fake_inspect(
        sliced=False,
        plates=[{"id": 1, "name": "", "used_filament_indices": [0]}],
        filaments=[
            {"slot": 0, "type": "PLA", "color": "#FFFFFF",
             "filament_id": "GFA00", "settings_id": "Bambu PLA Basic"},
        ],
    )
    info = _adapt(insp, plate_id=None, thumbnails={1: "BASE64"})
    assert info.has_gcode is False
    assert info.bed_type == "Textured PEI Plate"
    assert info.printer.printer_model == "Bambu Lab A1 mini"
    assert info.printer.nozzle_diameter == "0.4"
    assert len(info.plates) == 1
    assert info.plates[0].thumbnail == "BASE64"
    assert info.plates[0].used_filament_indices == [0]
    assert info.filaments[0].used is True


def test_adapter_filters_used_per_plate_id():
    insp = _fake_inspect(
        sliced=True,
        plates=[
            {"id": 1, "name": "", "used_filament_indices": [0]},
            {"id": 2, "name": "", "used_filament_indices": [1]},
        ],
        filaments=[
            {"slot": 0, "type": "PLA", "color": "#FFF", "filament_id": "GFA00", "settings_id": "A"},
            {"slot": 1, "type": "PETG", "color": "#000", "filament_id": "GFB00", "settings_id": "B"},
        ],
    )
    info = _adapt(insp, plate_id=2, thumbnails={})
    assert [f.used for f in info.filaments] == [False, True]


def test_adapter_unknown_per_plate_falls_back_to_all():
    insp = _fake_inspect(
        sliced=False,
        plates=[{"id": 1, "name": "", "used_filament_indices": None}],
        filaments=[
            {"slot": 0, "type": "PLA", "color": "#FFF", "filament_id": "GFA00", "settings_id": "A"},
            {"slot": 1, "type": "PETG", "color": "#000", "filament_id": "GFB00", "settings_id": "B"},
        ],
    )
    info = _adapt(insp, plate_id=None, thumbnails={})
    assert all(f.used for f in info.filaments)
```

Run: `pytest tests/test_parse_3mf_adapter.py -v` → expect 3 passed.

- [ ] **Step 4: Commit**

```bash
git add app/parse_3mf.py tests/test_parse_3mf_adapter.py
git commit -m "Adapt /3mf/inspect to ThreeMFInfo; keep parse_3mf shape"
```

---

### Task 5: Replace `parse_3mf` call sites with the async adapter

**Why:** `parse_3mf(data, plate_id)` was sync; the new adapter is async. The five call sites in `app/main.py` are all inside `async def` handlers, so awaiting is straightforward — just need to thread `slicer_client` in.

**Files:**
- Modify: `bambu-gateway/app/main.py`

- [ ] **Step 1: Locate every call site**

```bash
grep -n "parse_3mf(file_data" app/main.py
```

Expected: 5 matches around lines 975, 1105, 1316, 1431, 1627.

- [ ] **Step 2: Update the import and switch to the adapter**

Replace:

```python
from app.parse_3mf import parse_3mf
```

with:

```python
from app.parse_3mf import parse_3mf_via_slicer
```

For each of the 5 sites, change:

```python
info = parse_3mf(file_data, plate_id=plate_id)
```

to:

```python
info = await parse_3mf_via_slicer(
    file_data, slicer_client, plate_id=plate_id,
)
```

Where `slicer_client` is the FastAPI app-state instance (already created at `app/main.py:173` as `slicer_client = SlicerClient(settings.orcaslicer_api_url)`). If the call site doesn't have `slicer_client` in scope, pull it from the request context (`request.app.state.slicer_client`) — match whatever pattern the rest of the file uses.

- [ ] **Step 3: Smoke each affected endpoint**

```bash
# Pick one representative endpoint that uses parse_3mf and POST a fixture.
# Look up the actual route from app/main.py:962 area first.
curl -s -X POST -F "file=@/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" \
  http://localhost:9000/3mf/parse | python3 -m json.tool | head -30
```

Expected: same response shape as before the migration. If `plates[0].used_filament_indices` differs from the in-process parser's output, that's the GUI-vs-our-old-parser delta — investigate and decide whether to accept (likely the new value is more correct) or open a follow-up.

- [ ] **Step 4: Run the existing gateway test suite**

```bash
pytest -q
```

Expected: same pass count as pre-migration. Tests that import `parse_3mf` directly may need updating to `parse_3mf_via_slicer` with a mock `SlicerClient` — that's expected.

- [ ] **Step 5: Commit**

```bash
git add app/main.py
git commit -m "Route 3MF parsing through orcaslicer-cli /3mf/inspect"
```

---

### Task 6: Side-by-side parity check against the old parser

**Why:** Catch any unexpected divergence between the new path (network → inspect) and the old path (in-process ZIP/XML) on real fixtures before the old parser is deleted.

**Files:**
- Create: `bambu-gateway/tests/integration/test_parse_parity.py`

- [ ] **Step 1: Write the parity test**

```python
"""Side-by-side parity: parse_3mf_via_slicer vs the old parse_3mf.

Runs against the shared fixture set. Skipped when orcaslicer-cli isn't
reachable. Once Task 9 deletes the old parser this test gets removed
(or repurposed against a captured golden-output JSON).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from app.parse_3mf import parse_3mf_via_slicer  # new
from app.parse_3mf_legacy import parse_3mf as parse_3mf_old  # if/when staged
from app.slicer_client import SlicerClient

API = os.environ.get("ORCASLICER_API_URL", "http://localhost:8000")
FIX_DIR = Path(__file__).resolve().parents[3] / "_fixture"


def _reachable() -> bool:
    try:
        return httpx.get(f"{API}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"slicer at {API} unreachable")


@pytest.mark.asyncio
async def test_fixture_01_parity():
    fp = FIX_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf"
    if not fp.exists():
        pytest.skip("fixture missing")
    data = fp.read_bytes()

    old = parse_3mf_old(data, plate_id=None)
    client = SlicerClient(API)
    new = await parse_3mf_via_slicer(
        data, client, plate_id=None, include_thumbnails=False,
    )

    # Compare wire shape, ignoring fields we know diverge intentionally:
    # - thumbnails (URL-fetch vs in-place base64 — see _fetch_main_thumbnails)
    # - PlateInfo.objects (inspect doesn't currently surface object IDs)
    def _strip(info):
        d = info.model_dump()
        for p in d.get("plates", []):
            p.pop("thumbnail", None)
            p.pop("objects", None)
        return d

    assert _strip(new) == _strip(old)
```

If the old parser has been staged-renamed (Task 9 prep), import `parse_3mf as parse_3mf_old` from `app.parse_3mf_legacy`. Otherwise wire whatever module name the staged copy uses.

- [ ] **Step 2: Stage the old parser as `parse_3mf_legacy.py`** (only for this test's lifetime)

```bash
git mv app/parse_3mf.py app/parse_3mf_legacy.py
# Then re-add the new adapter at app/parse_3mf.py with a fresh file
# carrying ONLY the parse_3mf_via_slicer + _adapt + _fetch_main_thumbnails.
```

If the new adapter is already at `app/parse_3mf.py` from Task 4, copy the legacy version to `app/parse_3mf_legacy.py` instead and don't touch the new file.

- [ ] **Step 3: Run the parity test**

```bash
pytest tests/integration/test_parse_parity.py -v
```

Expected: 1 passed. Any AssertionError tells you the exact field that diverges — investigate. Common divergences and how to handle:

- `plates[*].objects`: stripped above; deferred until inspect surfaces object IDs (Phase 4 follow-up).
- `plates[*].thumbnail`: stripped above; URL-fetch path tested separately.
- `plates[*].used_filament_indices`: if these differ, the new GUI-faithful port is computing something different. Likely correct — capture the diff in a comment, accept the new value, document.
- `print_profile.print_settings_id`/`layer_height`: inspect doesn't currently return these. If callers need them, add fields to `app/inspect.py` (small Phase 3-followup commit) before merging.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_parse_parity.py app/parse_3mf_legacy.py
git commit -m "Side-by-side parity test: new adapter vs legacy parse_3mf"
```

---

## Milestone 3 — Retire `extract_print_estimate`

### Task 7: Use the slice response's estimate directly in `slice_jobs.py`

**Why:** `app/slice_jobs.py:680` calls `extract_print_estimate(result_bytes)` after a slice to pull time/weight from the output 3MF. But `orcaslicer-cli`'s slice response already returns `estimate: {time_seconds, weight_g, filament_used_m}` (Phase 1 shipped this). One less round trip and one less XML walk.

**Files:**
- Modify: `bambu-gateway/app/slice_jobs.py`
- Modify: `bambu-gateway/app/slicer_client.py` (if needed — the SliceResult dataclass)

- [ ] **Step 1: Inspect the existing slice flow**

```bash
sed -n '670,700p' app/slice_jobs.py
grep -B2 -A20 "class SliceResult" app/slicer_client.py
```

Confirm whether `SliceResult` already includes the estimate. If yes, use it. If the SlicerClient discards the estimate today (legacy behaviour from before Phase 1 added the field), update it to surface the estimate.

- [ ] **Step 2: Add `estimate` to `SliceResult`**

In `app/slicer_client.py`:

```python
@dataclass
class SliceResult:
    output_token: str
    output_sha256: str
    download_url: str
    estimate: PrintEstimate | None  # NEW; PrintEstimate model already exists
```

In `SlicerClient.slice`, decode `r.json()["estimate"]` into a `PrintEstimate` and set it on the result.

- [ ] **Step 3: Replace the call site**

In `app/slice_jobs.py`:

```python
# old
extracted = extract_print_estimate(result_bytes)

# new
extracted = slice_result.estimate
```

Drop the import: `from app.print_estimate import extract_print_estimate`.

- [ ] **Step 4: Run the gateway tests**

```bash
pytest -q
```

Expected: same pass count.

- [ ] **Step 5: Commit**

```bash
git add app/slice_jobs.py app/slicer_client.py
git commit -m "Read slice estimate from /slice/v2 response, not output 3MF"
```

---

### Task 8: Drop `extract_print_estimate` fallback in `slicer_client.py`

**Why:** `app/slicer_client.py:111` uses `extract_print_estimate(resp.content)` as a fallback when the slice response doesn't carry an estimate. Phase 1's `/slice/v2` always carries it. The fallback is dead code — delete it.

**Files:**
- Modify: `bambu-gateway/app/slicer_client.py`

- [ ] **Step 1: Identify and remove the fallback**

```bash
grep -B5 -A5 "extract_print_estimate" app/slicer_client.py
```

Find the `or extract_print_estimate(resp.content)` chain and replace with the direct estimate decode.

- [ ] **Step 2: Drop the import**

Remove `from app.print_estimate import extract_print_estimate` if it's now unused.

- [ ] **Step 3: Run all tests**

```bash
pytest -q
```

- [ ] **Step 4: Commit**

```bash
git add app/slicer_client.py
git commit -m "Drop extract_print_estimate fallback in SlicerClient"
```

---

## Milestone 4 — Cleanup

### Task 9: Delete `parse_3mf_legacy.py` and `print_estimate.py`

**Why:** Both files are now imported from nowhere except the parity test (which itself has served its purpose).

**Files:**
- Delete: `bambu-gateway/app/parse_3mf_legacy.py`
- Delete: `bambu-gateway/app/print_estimate.py`
- Delete: `bambu-gateway/tests/integration/test_parse_parity.py`
- Delete: any test file that imported `app.parse_3mf` and exercised internals (`_parse_model_settings`, `_parse_project_settings`, etc.) — the public-API tests stay, internals tests go.

- [ ] **Step 1: Confirm nothing imports them**

```bash
grep -rn "from app.parse_3mf_legacy\|import parse_3mf_legacy" .
grep -rn "from app.print_estimate\|import extract_print_estimate" .
```

Both should return zero hits (excluding the files themselves). If they return anything, fix the call site first.

- [ ] **Step 2: Delete**

```bash
git rm app/parse_3mf_legacy.py app/print_estimate.py tests/integration/test_parse_parity.py
# Plus any internals-targeting tests grep -l 'from app.parse_3mf import _' tests/
```

- [ ] **Step 3: Run the suite**

```bash
pytest -q
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git commit -m "Remove legacy 3MF parser and print-estimate XML walker"
```

---

### Task 10: README + docs update

**Why:** Document that the gateway is now thin and depends on `orcaslicer-cli`'s Phase 3 endpoints.

**Files:**
- Modify: `bambu-gateway/README.md`
- Modify: `bambu-gateway/CLAUDE.md` if it describes the architecture

- [ ] **Step 1: Update the architecture section**

Add or update:

```markdown
## 3MF parsing

`bambu-gateway` does not open `.3mf` ZIPs. All metadata extraction
(plates, filaments, thumbnails, used-filament dispatch, print
estimates) goes through `orcaslicer-cli`'s HTTP API, which links
libslic3r directly. Set `ORCASLICER_API_URL` to point at it.

Required endpoints (consumed by `app/slicer_client.py`):
- `POST /3mf` — upload, returns token
- `GET /3mf/{token}/inspect` — structured summary
- `GET /3mf/{token}/plates/{n}/thumbnail` — PNG bytes
- `DELETE /3mf/{token}` — drop cached upload
- `POST /slice/v2` — slice, returns estimate + output token
```

- [ ] **Step 2: Update CLAUDE.md if it had a section about parse_3mf**

```bash
grep -n "parse_3mf\|print_estimate" CLAUDE.md
```

If those are referenced, replace with a pointer to the slicer client.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Doc gateway dependency on orcaslicer-cli /3mf/inspect"
```

---

## Self-Review

**Spec coverage** (vs `docs/superpowers/specs/2026-05-02-orca-headless-fork-design.md` Phase 3 deliverables):

- "Gateway never opens a .3mf ZIP again" — Task 9 deletes both ZIP-opening files.
- "Replace plate/thumbnail/filament logic with `OrcaslicerClient` calls" — Tasks 1, 4, 5 cover plates + filaments; thumbnails are fetched per-plate from inspect URLs.
- "Delete `app/parse_3mf.py`, `app/print_estimate.py`" — Task 9.
- "Replace `extract_print_estimate` with the slice response" — Tasks 7–8.

**Open follow-ups called out inline:**

- `PlateInfo.objects` (the per-plate object ID/name list) is left empty in the adapter for now. If the iOS app or another caller depends on it, add it to `orcaslicer-cli`'s inspect response (small Phase 3-followup commit on the slicer side, then update `_adapt`). Until then, callers see `objects=[]`.
- `PrintProfileInfo.print_settings_id` / `layer_height` aren't surfaced by inspect today. Same shape of follow-up.
- Thumbnail base64 inlining adds one HTTP round trip per plate; URL-based delivery is a UI-side migration deferred to Phase 4.

**Placeholder scan:** every step has actual code or commands. No "TBD" / "similar to Task N" / "add error handling" patterns.

**Type consistency:** `parse_3mf_via_slicer` returns `ThreeMFInfo` everywhere. `SlicerClient.upload_3mf` / `inspect` / `delete_token` return `dict` / `dict` / `bool` consistently across Tasks 1–6.

**Risks:**

- Task 5's call-site replacements assume `slicer_client` is reachable in every handler. Verify by reading `app/main.py:173` context — if any of the five call sites is in a non-FastAPI helper, route the client through whatever IoC path that helper already uses.
- Task 6's parity test will surface real differences in `used_filament_indices` (the GUI-faithful port computes different values than the legacy in-process parser when supports/wall_filament/layer_ranges are at play). Don't relax the test — investigate, decide, and document.
