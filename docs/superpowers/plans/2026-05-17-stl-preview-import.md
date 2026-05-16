# STL Preview Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `orcaslicer-headless` side of preview-first STL support: draft cache, STL import/layout/export endpoints, a stateless C++ `orca-headless stl-draft` mode, and tests.

**Architecture:** FastAPI owns draft lifecycle and stores source STL plus current draft 3MF artifacts under the existing cache root. The C++ binary remains stateless: every call receives explicit input/output paths, loads model state through libslic3r, applies one GUI-parity operation, and returns scene JSON. Gateway rendering is a separate follow-up plan after these endpoints establish the API contract.

**Tech Stack:** FastAPI, Pydantic, pytest, httpx-style binary client tests, C++17, libslic3r `Model::read_from_file`, `orientation::orient`, `arrangement::arrange`, Docker-only integration verification.

---

## File Structure

- Create `app/stl_drafts.py`: draft-token cache, on-disk paths, scene dataclasses/Pydantic helpers, action validation.
- Modify `app/config.py`: add STL draft cache root and TTL env vars.
- Modify `app/binary_client.py`: add the `stl_draft` wrapper around `orca-headless stl-draft`.
- Modify `app/main.py`: wire `app.state.stl_drafts` and add `/stl/import`, `/stl/{draft_token}/layout`, `/stl/{draft_token}/3mf`.
- Create `cpp/src/stl_draft_mode.h` and `cpp/src/stl_draft_mode.cpp`: stateless C++ import/layout/export implementation.
- Modify `cpp/src/json_io.h` and `cpp/src/json_io.cpp`: STL draft request/response structs and JSON parsing/writing.
- Modify `cpp/src/orca_headless.cpp`: dispatch the `stl-draft` command.
- Modify `cpp/CMakeLists.txt`: compile `src/stl_draft_mode.cpp`.
- Create `tests/test_stl_drafts.py`: Python draft cache tests.
- Create `tests/test_stl_endpoints.py`: FastAPI endpoint tests with fake binary client.
- Create `tests/test_binary_client_stl.py`: subprocess wrapper tests.
- Create `tests/integration/test_stl_import.py`: opt-in container integration tests.

Gateway files are intentionally not modified in this plan. After this plan lands, create a separate `bambu-gateway` plan for `/api/stl-drafts`, Three.js rendering, and print-route UI.

---

### Task 1: Draft Cache Unit

**Files:**
- Create: `app/stl_drafts.py`
- Test: `tests/test_stl_drafts.py`

- [ ] **Step 1: Write failing tests for draft lifecycle**

Create `tests/test_stl_drafts.py`:

```python
from __future__ import annotations

import pytest

from app.stl_drafts import (
    StlDraftAction,
    StlDraftCache,
    StlDraftExpired,
    StlDraftUnknown,
    validate_stl_action,
)


def test_put_creates_source_and_current_paths(tmp_path):
    cache = StlDraftCache(root=tmp_path, ttl_seconds=3600)

    draft = cache.put_source(b"solid test\nendsolid test\n", "part.stl")

    assert draft.token
    assert draft.filename == "part.stl"
    assert draft.source_path.exists()
    assert draft.source_path.read_bytes().startswith(b"solid")
    assert draft.current_3mf_path.name == "current.3mf"
    assert draft.next_3mf_path().name == "next.3mf"


def test_get_unknown_raises(tmp_path):
    cache = StlDraftCache(root=tmp_path, ttl_seconds=3600)

    with pytest.raises(StlDraftUnknown):
        cache.get("missing")


def test_expired_draft_is_deleted(tmp_path, monkeypatch):
    cache = StlDraftCache(root=tmp_path, ttl_seconds=10)
    draft = cache.put_source(b"solid test\nendsolid test\n", "part.stl")
    monkeypatch.setattr("app.stl_drafts.time.time", lambda: draft.created_at + 11)

    with pytest.raises(StlDraftExpired):
        cache.get(draft.token)
    assert not draft.root.exists()


def test_validate_stl_action_accepts_known_actions():
    assert validate_stl_action("auto_orient") == StlDraftAction.AUTO_ORIENT
    assert validate_stl_action("rotate_z_90") == StlDraftAction.ROTATE_Z_90
    assert validate_stl_action("rotate_z_minus_90") == StlDraftAction.ROTATE_Z_MINUS_90
    assert validate_stl_action("center") == StlDraftAction.CENTER
    assert validate_stl_action("arrange") == StlDraftAction.ARRANGE
    assert validate_stl_action("reset") == StlDraftAction.RESET


def test_validate_stl_action_rejects_unknown_action():
    with pytest.raises(ValueError, match="invalid STL layout action"):
        validate_stl_action("drag")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_stl_drafts.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.stl_drafts'`.

- [ ] **Step 3: Implement draft cache**

Create `app/stl_drafts.py`:

```python
"""On-disk cache for preview-first STL draft sessions."""

from __future__ import annotations

import re
import secrets
import shutil
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StlDraftUnknown(KeyError):
    """Raised when a draft token is not known to this process."""


class StlDraftExpired(KeyError):
    """Raised when a draft token existed but exceeded its TTL."""


class StlDraftAction(StrEnum):
    AUTO_ORIENT = "auto_orient"
    ROTATE_Z_90 = "rotate_z_90"
    ROTATE_Z_MINUS_90 = "rotate_z_minus_90"
    CENTER = "center"
    ARRANGE = "arrange"
    RESET = "reset"


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str | None) -> str:
    raw = (name or "model.stl").split("/")[-1].split("\\")[-1].strip()
    safe = _SAFE_NAME_RE.sub("_", raw) or "model.stl"
    return safe if safe.lower().endswith(".stl") else f"{safe}.stl"


def validate_stl_action(value: str) -> StlDraftAction:
    try:
        return StlDraftAction(value)
    except ValueError as exc:
        allowed = ", ".join(a.value for a in StlDraftAction)
        raise ValueError(f"invalid STL layout action {value!r}; allowed: {allowed}") from exc


@dataclass(frozen=True)
class StlDraft:
    token: str
    root: Path
    filename: str
    created_at: float
    last_access: float

    @property
    def source_path(self) -> Path:
        return self.root / "source.stl"

    @property
    def current_3mf_path(self) -> Path:
        return self.root / "current.3mf"

    @property
    def scene_path(self) -> Path:
        return self.root / "scene.json"

    def next_3mf_path(self) -> Path:
        return self.root / "next.3mf"


class StlDraftCache:
    def __init__(self, root: Path, ttl_seconds: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._drafts: dict[str, StlDraft] = {}

    def put_source(self, payload: bytes, filename: str | None) -> StlDraft:
        token = secrets.token_urlsafe(16)
        now = time.time()
        root = self.root / token
        root.mkdir(parents=True, exist_ok=False)
        draft = StlDraft(
            token=token,
            root=root,
            filename=_safe_filename(filename),
            created_at=now,
            last_access=now,
        )
        draft.source_path.write_bytes(payload)
        self._drafts[token] = draft
        return draft

    def get(self, token: str) -> StlDraft:
        draft = self._drafts.get(token)
        if draft is None:
            raise StlDraftUnknown(token)
        now = time.time()
        if now - draft.created_at > self.ttl_seconds:
            self.delete(token)
            raise StlDraftExpired(token)
        refreshed = StlDraft(
            token=draft.token,
            root=draft.root,
            filename=draft.filename,
            created_at=draft.created_at,
            last_access=now,
        )
        self._drafts[token] = refreshed
        return refreshed

    def delete(self, token: str) -> bool:
        draft = self._drafts.pop(token, None)
        if draft is None:
            return False
        shutil.rmtree(draft.root, ignore_errors=True)
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_stl_drafts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/stl_drafts.py tests/test_stl_drafts.py
git commit -m "feat(api): add STL draft cache"
```

---

### Task 2: Binary Client STL Wrappers

**Files:**
- Modify: `app/binary_client.py`
- Test: `tests/test_binary_client_stl.py`

- [ ] **Step 1: Write failing binary-client tests**

Create `tests/test_binary_client_stl.py`:

```python
from __future__ import annotations

import asyncio
import json

import pytest

from app.binary_client import BinaryClient, BinaryError


class _FakeProcess:
    def __init__(self, stdout_payload: dict, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout_payload = stdout_payload
        self.stdin_data: bytes | None = None

    async def communicate(self, input: bytes = b""):
        self.stdin_data = input
        return json.dumps(self.stdout_payload).encode(), b""

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return None


@pytest.mark.asyncio
async def test_stl_draft_import_invokes_binary(monkeypatch):
    calls = []
    proc = _FakeProcess({"status": "ok", "scene": {"draft_token": "abc"}})

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BinaryClient("/bin/orca-headless")

    result = await client.stl_draft({
        "operation": "import",
        "input_stl": "/tmp/source.stl",
        "output_3mf": "/tmp/current.3mf",
    })

    assert calls[0][:2] == ("/bin/orca-headless", "stl-draft")
    assert result["scene"]["draft_token"] == "abc"
    assert json.loads(proc.stdin_data.decode())["operation"] == "import"


@pytest.mark.asyncio
async def test_stl_draft_raises_binary_error(monkeypatch):
    proc = _FakeProcess(
        {"status": "error", "code": "invalid_stl", "message": "empty STL", "details": {}},
        returncode=1,
    )

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BinaryClient("/bin/orca-headless")

    with pytest.raises(BinaryError) as exc:
        await client.stl_draft({"operation": "import"})

    assert exc.value.code == "invalid_stl"
    assert exc.value.message == "empty STL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_binary_client_stl.py -q
```

Expected: FAIL with `AttributeError: 'BinaryClient' object has no attribute 'stl_draft'`.

- [ ] **Step 3: Add shared binary request helper and STL wrapper**

Modify `app/binary_client.py` by adding this method inside `BinaryClient`:

```python
    async def stl_draft(self, request: dict[str, Any], timeout_s: float = 120.0) -> dict[str, Any]:
        """Invoke ``orca-headless stl-draft`` and return the parsed response."""
        proc = await asyncio.create_subprocess_exec(
            self.binary_path, "stl-draft",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=json.dumps(request).encode()),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BinaryError(
                code="binary_timeout",
                message=f"stl-draft timed out after {timeout_s}s",
                details={},
            )

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if proc.returncode != 0 and not stdout.strip():
            raise BinaryError(
                code="binary_crashed",
                message=f"orca-headless stl-draft exited {proc.returncode} with no stdout",
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
                code=response.get("code", "unknown"),
                message=response.get("message", ""),
                details=response.get("details", {}),
                stderr_tail=stderr_text[-2000:],
            )

        return response
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_binary_client_stl.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/binary_client.py tests/test_binary_client_stl.py
git commit -m "feat(api): add STL draft binary client"
```

---

### Task 3: FastAPI STL Endpoints With Fake Binary

**Files:**
- Modify: `app/slicer.py`
- Modify: `app/config.py`
- Modify: `app/main.py`
- Test: `tests/test_stl_endpoints.py`

- [ ] **Step 1: Write failing endpoint tests**

Create `tests/test_stl_endpoints.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.stl_drafts import StlDraftCache


class FakeBinary:
    def __init__(self):
        self.requests = []

    async def stl_draft(self, request, timeout_s=120.0):
        self.requests.append(request)
        op = request["operation"]
        if op in {"import", "layout"}:
            Path(request["output_3mf"]).write_bytes(b"fake-3mf")
            return {
                "status": "ok",
                "scene": {
                    "draft_token": request.get("draft_token", "token-from-api"),
                    "source_filename": "part.stl",
                    "bed": {"width": 180, "depth": 180, "printable_area": [[0, 0], [180, 0], [180, 180], [0, 180]]},
                    "objects": [{
                        "id": "0",
                        "name": "part.stl",
                        "transform": {"offset": [90, 90, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                        "bbox": {"min": [80, 80, 0], "max": [100, 100, 20]},
                        "printable": True,
                    }],
                    "warnings": [],
                    "actions": ["auto_orient", "rotate_z_90", "rotate_z_minus_90", "center", "arrange", "reset"],
                },
            }
        if op == "export_3mf":
            Path(request["output_3mf"]).write_bytes(b"materialized-3mf")
            return {"status": "ok"}
        raise AssertionError(op)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    app.state.stl_drafts = StlDraftCache(tmp_path / "stl-drafts", ttl_seconds=3600)
    app.state.token_cache = type("Cache", (), {
        "put": lambda self, payload: ("tok3mf", "sha", len(payload), []),
    })()
    fake = FakeBinary()
    monkeypatch.setattr("app.main.BinaryClient", lambda binary_path: fake)
    with TestClient(app) as c:
        c.fake_binary = fake
        yield c


def test_stl_import_returns_scene_and_writes_draft(client):
    resp = client.post(
        "/stl/import",
        data={
            "machine_id": "GM020",
            "process_id": "GP000",
            "auto_orient": "false",
            "arrange": "true",
            "center": "true",
        },
        files={"file": ("part.stl", b"solid part\nendsolid part\n", "application/sla")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["draft_token"]
    assert body["objects"][0]["printable"] is True
    assert client.fake_binary.requests[0]["operation"] == "import"


def test_stl_layout_rejects_unknown_action(client):
    imported = client.post(
        "/stl/import",
        data={"machine_id": "GM020", "process_id": "GP000"},
        files={"file": ("part.stl", b"solid part\nendsolid part\n", "application/sla")},
    ).json()

    resp = client.post(f"/stl/{imported['draft_token']}/layout", json={"action": "drag"})

    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_stl_layout_action"


def test_stl_layout_updates_scene(client):
    imported = client.post(
        "/stl/import",
        data={"machine_id": "GM020", "process_id": "GP000"},
        files={"file": ("part.stl", b"solid part\nendsolid part\n", "application/sla")},
    ).json()

    resp = client.post(f"/stl/{imported['draft_token']}/layout", json={"action": "center"})

    assert resp.status_code == 200
    assert resp.json()["draft_token"] == imported["draft_token"]
    assert client.fake_binary.requests[-1]["operation"] == "layout"
    assert client.fake_binary.requests[-1]["action"] == "center"


def test_stl_export_returns_3mf_token(client):
    imported = client.post(
        "/stl/import",
        data={"machine_id": "GM020", "process_id": "GP000"},
        files={"file": ("part.stl", b"solid part\nendsolid part\n", "application/sla")},
    ).json()

    resp = client.post(f"/stl/{imported['draft_token']}/3mf")

    assert resp.status_code == 200
    assert resp.json() == {"input_token": "tok3mf", "draft_token": imported["draft_token"]}
    assert client.fake_binary.requests[-1]["operation"] == "export_3mf"


def test_stl_import_uses_machine_process_only_materialization(client, monkeypatch):
    async def fake_materialize(machine_id: str, process_id: str):
        return {
            "machine_chain_dir": "/tmp/machine",
            "machine_leaf_name": "A1 mini",
            "process_chain_dir": "/tmp/process",
            "process_leaf_name": "0.20mm Standard",
        }

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("STL import must not materialize filament chains")

    monkeypatch.setattr("app.main.materialize_machine_process_for_binary", fake_materialize)
    monkeypatch.setattr("app.main.materialize_profiles_for_binary", fail_if_called)

    resp = client.post(
        "/stl/import",
        data={"machine_id": "GM020", "process_id": "GP000"},
        files={"file": ("part.stl", b"solid part\nendsolid part\n", "application/sla")},
    )

    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_stl_endpoints.py -q
```

Expected: FAIL with 404 for `/stl/import`.

- [ ] **Step 3: Add machine/process-only profile materialization**

Modify `app/slicer.py` by adding this helper after `materialize_profiles_for_binary`:

```python
async def materialize_machine_process_for_binary(
    machine_id: str,
    process_id: str,
) -> dict[str, Any]:
    """Write only machine/process chain dirs for STL draft import.

    STL draft preview does not compose a final print config and does not
    need filament profiles. Keeping this separate avoids inventing a fake
    filament slot solely to satisfy the slice path's materializer.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="orca-headless-stl-profiles-"))
    machine_dir = tmp_dir / "machine"
    process_dir = tmp_dir / "process"
    for d in (machine_dir, process_dir):
        d.mkdir(parents=True, exist_ok=True)

    machine = get_profile("machine", machine_id)
    machine_leaf_name = machine.get("name", machine_id)
    machine_written: set[str] = set()
    for link_name, _raw in iter_inheritance_chain(machine_leaf_name):
        _write_chain_link(
            machine_dir,
            link_name,
            is_filament=False,
            written_names=machine_written,
        )

    process = get_profile("process", process_id)
    process_leaf_name = process.get("name", process_id)
    process_written: set[str] = set()
    for link_name, _raw in iter_inheritance_chain(process_leaf_name):
        _write_chain_link(
            process_dir,
            link_name,
            is_filament=False,
            written_names=process_written,
        )

    return {
        "machine_chain_dir": str(machine_dir),
        "machine_leaf_name": machine_leaf_name,
        "process_chain_dir": str(process_dir),
        "process_leaf_name": process_leaf_name,
    }
```

- [ ] **Step 4: Add config values**

Modify `app/config.py`:

```python
STL_DRAFT_CACHE_DIR = Path(os.environ.get("STL_DRAFT_CACHE_DIR", str(CACHE_DIR / "stl-drafts")))
STL_DRAFT_TTL_SECONDS = int(os.environ.get("STL_DRAFT_TTL_SECONDS", str(60 * 60)))
```

- [ ] **Step 5: Wire cache in lifespan**

Modify `app/main.py` imports:

```python
from .stl_drafts import (
    StlDraftCache,
    StlDraftExpired,
    StlDraftUnknown,
    validate_stl_action,
)
```

Add `materialize_machine_process_for_binary` to the existing `.slicer` import list:

```python
from .slicer import (
    PLATE_TYPE_API_TO_ORCA,
    SUPPORTED_PLATE_TYPES,
    VALID_BRIM_TYPES,
    VALID_INFILL_PATTERNS,
    VALID_SUPPORT_TYPES,
    validate_3mf_preset_references,
    IncompatibleFilamentError,
    ModelTooBigError,
    SlicingError,
    materialize_machine_process_for_binary,
    materialize_profiles_for_binary,
    pad_filament_settings_for_sparse_3mf,
)
```

In `lifespan`, after `app.state.token_cache = ...`, add:

```python
    app.state.stl_drafts = StlDraftCache(
        root=cfg.STL_DRAFT_CACHE_DIR,
        ttl_seconds=cfg.STL_DRAFT_TTL_SECONDS,
    )
```

- [ ] **Step 6: Add endpoint models and handlers**

Add near `SliceTokenRequest` in `app/main.py`:

```python
class StlLayoutRequest(BaseModel):
    action: str


def _scene_with_token(scene: dict[str, Any], token: str) -> dict[str, Any]:
    out = dict(scene)
    out["draft_token"] = token
    return out


def _draft_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "message": message})
```

Add endpoints before the static web mount:

```python
@app.post("/stl/import", tags=["STL"])
async def import_stl(
    request: Request,
    file: UploadFile = File(...),
    machine_id: str = Form(...),
    process_id: str = Form(...),
    plate_type: str | None = Form(None),
    auto_orient: bool = Form(False),
    arrange: bool = Form(True),
    center: bool = Form(True),
):
    if not file.filename or not file.filename.lower().endswith(".stl"):
        return _draft_error(400, "invalid_stl", "File must be a .stl file")
    payload = await file.read()
    if not payload:
        return _draft_error(400, "invalid_stl", "STL file is empty")
    drafts: StlDraftCache = request.app.state.stl_drafts
    draft = drafts.put_source(payload, file.filename)

    paths = await materialize_machine_process_for_binary(
        machine_id=machine_id,
        process_id=process_id,
    )
    binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
    try:
        result = await binary.stl_draft({
            "operation": "import",
            "draft_token": draft.token,
            "input_stl": str(draft.source_path),
            "output_3mf": str(draft.current_3mf_path),
            "machine_chain_dir": paths["machine_chain_dir"],
            "process_chain_dir": paths["process_chain_dir"],
            "machine_leaf_name": paths["machine_leaf_name"],
            "process_leaf_name": paths["process_leaf_name"],
            "plate_type": _resolve_plate_type_label(machine_id, plate_type),
            "options": {
                "auto_orient": auto_orient,
                "arrange": arrange,
                "center": center,
            },
        })
    except BinaryError as e:
        return _draft_error(400 if e.code == "invalid_stl" else 500, e.code, e.message)
    draft.scene_path.write_text(json.dumps(result["scene"]))
    return _scene_with_token(result["scene"], draft.token)


@app.post("/stl/{draft_token}/layout", tags=["STL"])
async def layout_stl(draft_token: str, body: StlLayoutRequest, request: Request):
    try:
        action = validate_stl_action(body.action)
    except ValueError as e:
        return _draft_error(400, "invalid_stl_layout_action", str(e))
    try:
        draft = request.app.state.stl_drafts.get(draft_token)
    except StlDraftUnknown:
        return _draft_error(404, "draft_unknown", "STL draft token is unknown")
    except StlDraftExpired:
        return _draft_error(410, "draft_expired", "STL draft token has expired")

    binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
    next_path = draft.next_3mf_path()
    try:
        result = await binary.stl_draft({
            "operation": "layout",
            "draft_token": draft.token,
            "input_3mf": str(draft.current_3mf_path),
            "output_3mf": str(next_path),
            "action": action.value,
        })
    except BinaryError as e:
        return _draft_error(409 if e.code in {"arrange_failed", "auto_orient_failed"} else 500, e.code, e.message)
    next_path.replace(draft.current_3mf_path)
    draft.scene_path.write_text(json.dumps(result["scene"]))
    return _scene_with_token(result["scene"], draft.token)


@app.post("/stl/{draft_token}/3mf", tags=["STL"])
async def materialize_stl_3mf(draft_token: str, request: Request):
    try:
        draft = request.app.state.stl_drafts.get(draft_token)
    except StlDraftUnknown:
        return _draft_error(404, "draft_unknown", "STL draft token is unknown")
    except StlDraftExpired:
        return _draft_error(410, "draft_expired", "STL draft token has expired")

    output_path = draft.root / "materialized.3mf"
    binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
    try:
        await binary.stl_draft({
            "operation": "export_3mf",
            "draft_token": draft.token,
            "input_3mf": str(draft.current_3mf_path),
            "output_3mf": str(output_path),
        })
    except BinaryError as e:
        return _draft_error(500, e.code, e.message)

    cache: TokenCache = request.app.state.token_cache
    token, _sha, _size, _evicted = cache.put(output_path.read_bytes())
    return {"input_token": token, "draft_token": draft.token}
```

- [ ] **Step 7: Run endpoint tests**

Run:

```bash
pytest tests/test_stl_endpoints.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add app/slicer.py app/config.py app/main.py tests/test_stl_endpoints.py
git commit -m "feat(api): add STL draft endpoints"
```

---

### Task 4: C++ JSON Protocol And Dispatcher

**Files:**
- Modify: `cpp/src/json_io.h`
- Modify: `cpp/src/json_io.cpp`
- Modify: `cpp/src/orca_headless.cpp`
- Modify: `cpp/CMakeLists.txt`
- Create: `cpp/src/stl_draft_mode.h`
- Create: `cpp/src/stl_draft_mode.cpp`

- [ ] **Step 1: Add C++ request/response structs**

Modify `cpp/src/json_io.h`:

```cpp
struct StlDraftRequest {
    std::string operation;   // import | layout | export_3mf
    std::string draft_token;
    std::string input_stl;
    std::string input_3mf;
    std::string output_3mf;
    std::string action;

    std::string machine_chain_dir;
    std::string process_chain_dir;
    std::string machine_leaf_name;
    std::string process_leaf_name;
    std::string plate_type;

    bool auto_orient = false;
    bool arrange = true;
    bool center = true;
};

struct StlDraftResponse {
    std::string status;      // ok | error
    nlohmann::json scene = nlohmann::json::object();
    std::string error_code;
    std::string error_message;
    nlohmann::json error_details = nlohmann::json::object();
};

StlDraftRequest parse_stl_draft_request_from_stdin();
void write_stl_draft_response_to_stdout(const StlDraftResponse& r);
```

- [ ] **Step 2: Implement C++ JSON parse/write**

Modify `cpp/src/json_io.cpp`:

```cpp
StlDraftRequest parse_stl_draft_request_from_stdin() {
    std::stringstream ss;
    ss << std::cin.rdbuf();
    json j = json::parse(ss.str());

    StlDraftRequest req;
    req.operation = j.value("operation", std::string{});
    req.draft_token = j.value("draft_token", std::string{});
    req.input_stl = j.value("input_stl", std::string{});
    req.input_3mf = j.value("input_3mf", std::string{});
    req.output_3mf = j.value("output_3mf", std::string{});
    req.action = j.value("action", std::string{});
    req.machine_chain_dir = j.value("machine_chain_dir", std::string{});
    req.process_chain_dir = j.value("process_chain_dir", std::string{});
    req.machine_leaf_name = j.value("machine_leaf_name", std::string{});
    req.process_leaf_name = j.value("process_leaf_name", std::string{});
    req.plate_type = j.value("plate_type", std::string{});
    if (j.contains("options") && j["options"].is_object()) {
        req.auto_orient = j["options"].value("auto_orient", false);
        req.arrange = j["options"].value("arrange", true);
        req.center = j["options"].value("center", true);
    }
    return req;
}

void write_stl_draft_response_to_stdout(const StlDraftResponse& r) {
    json out;
    out["status"] = r.status;
    if (r.status == "ok") {
        out["scene"] = r.scene;
    } else {
        out["code"] = r.error_code;
        out["message"] = r.error_message;
        out["details"] = r.error_details;
    }
    write_envelope_line(out);
}
```

- [ ] **Step 3: Add mode header and stub**

Create `cpp/src/stl_draft_mode.h`:

```cpp
#pragma once

#include "json_io.h"

namespace orca_headless {

int run_stl_draft_mode(const StlDraftRequest& req);

}  // namespace orca_headless
```

Create `cpp/src/stl_draft_mode.cpp`:

```cpp
#include "stl_draft_mode.h"

namespace orca_headless {

namespace {
int fail(const std::string& code, const std::string& message, StlDraftResponse& r) {
    r.status = "error";
    r.error_code = code;
    r.error_message = message;
    write_stl_draft_response_to_stdout(r);
    return 1;
}
}

int run_stl_draft_mode(const StlDraftRequest& req) {
    StlDraftResponse response;
    if (req.operation.empty()) {
        return fail("invalid_request", "operation is required", response);
    }
    return fail("unsupported_operation", "stl-draft operation is unavailable in this build", response);
}

}  // namespace orca_headless
```

- [ ] **Step 4: Wire dispatcher and build file**

Modify `cpp/src/orca_headless.cpp` includes:

```cpp
#include "stl_draft_mode.h"
```

Modify usage text:

```cpp
        "  stl-draft           Read JSON request on stdin, import/layout/export STL draft\n"
```

Add command branch before `return print_usage(argv[0]);`:

```cpp
    if (std::strcmp(argv[1], "stl-draft") == 0) {
        try {
            auto req = orca_headless::parse_stl_draft_request_from_stdin();
            return orca_headless::run_stl_draft_mode(req);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "fatal: %s\n", e.what());
            emit_fatal_envelope("binary_fatal", e.what());
            return 1;
        }
    }
```

Modify `cpp/CMakeLists.txt` executable sources:

```cmake
    src/stl_draft_mode.cpp
```

- [ ] **Step 5: Build binary target**

Run in the Docker/dev C++ environment described by `docs/dev-shell-cpp.md`:

```bash
cmake --build build --target orca-headless -j2
```

Expected: build succeeds.

- [ ] **Step 6: Smoke the stub command**

Run:

```bash
printf '{"operation":"import"}' | ./build/orca-headless stl-draft
```

Expected JSON includes:

```json
{"status":"error","code":"unsupported_operation","message":"stl-draft operation is unavailable in this build","details":{}}
```

- [ ] **Step 7: Commit**

Run:

```bash
git add cpp/src/json_io.h cpp/src/json_io.cpp cpp/src/orca_headless.cpp cpp/CMakeLists.txt cpp/src/stl_draft_mode.h cpp/src/stl_draft_mode.cpp
git commit -m "feat(binary): add STL draft command protocol"
```

---

### Task 5: C++ STL Import Scene

**Files:**
- Modify: `cpp/src/stl_draft_mode.cpp`

- [ ] **Step 1: Add import operation helpers**

Replace the stub body in `cpp/src/stl_draft_mode.cpp` with imports and helpers:

```cpp
#include "stl_draft_mode.h"

#include "libslic3r/Model.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"

#include <algorithm>
#include <filesystem>

namespace orca_headless {

namespace {

static const std::vector<std::string> k_actions{
    "auto_orient", "rotate_z_90", "rotate_z_minus_90", "center", "arrange", "reset",
};

int fail(const std::string& code, const std::string& message, StlDraftResponse& r) {
    r.status = "error";
    r.error_code = code;
    r.error_message = message;
    write_stl_draft_response_to_stdout(r);
    return 1;
}

Slic3r::DynamicPrintConfig minimal_bed_config() {
    Slic3r::DynamicPrintConfig cfg;
    auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area", true);
    area->values = {
        Slic3r::Vec2d(0.0, 0.0),
        Slic3r::Vec2d(180.0, 0.0),
        Slic3r::Vec2d(180.0, 180.0),
        Slic3r::Vec2d(0.0, 180.0),
    };
    return cfg;
}

Slic3r::Vec2d bed_center(const Slic3r::DynamicPrintConfig& cfg) {
    const auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area");
    if (!area || area->values.empty()) return {90.0, 90.0};
    double min_x = area->values.front().x(), max_x = min_x;
    double min_y = area->values.front().y(), max_y = min_y;
    for (const auto& p : area->values) {
        min_x = std::min(min_x, p.x());
        max_x = std::max(max_x, p.x());
        min_y = std::min(min_y, p.y());
        max_y = std::max(max_y, p.y());
    }
    return {(min_x + max_x) / 2.0, (min_y + max_y) / 2.0};
}

void ground_all(Slic3r::Model& model) {
    for (auto* obj : model.objects) {
        if (obj) obj->ensure_on_bed(false);
    }
}

nlohmann::json scene_for_model(
    const StlDraftRequest& req,
    const Slic3r::Model& model,
    const Slic3r::DynamicPrintConfig& cfg) {
    nlohmann::json scene;
    scene["draft_token"] = req.draft_token;
    scene["source_filename"] = std::filesystem::path(req.input_stl).filename().string();

    const auto* area = cfg.opt<Slic3r::ConfigOptionPoints>("printable_area");
    nlohmann::json printable = nlohmann::json::array();
    double min_x = 0.0, max_x = 180.0, min_y = 0.0, max_y = 180.0;
    if (area && !area->values.empty()) {
        min_x = max_x = area->values.front().x();
        min_y = max_y = area->values.front().y();
        for (const auto& p : area->values) {
            printable.push_back({p.x(), p.y()});
            min_x = std::min(min_x, p.x());
            max_x = std::max(max_x, p.x());
            min_y = std::min(min_y, p.y());
            max_y = std::max(max_y, p.y());
        }
    }
    scene["bed"] = {
        {"width", max_x - min_x},
        {"depth", max_y - min_y},
        {"printable_area", printable},
    };

    nlohmann::json objects = nlohmann::json::array();
    for (size_t obj_idx = 0; obj_idx < model.objects.size(); ++obj_idx) {
        const auto* obj = model.objects[obj_idx];
        if (!obj || obj->instances.empty()) continue;
        const auto* inst = obj->instances.front();
        Slic3r::BoundingBoxf3 bb = obj->instance_bounding_box(0, false);
        objects.push_back({
            {"id", std::to_string(obj_idx)},
            {"name", obj->name},
            {"transform", {
                {"offset", {inst->get_offset().x(), inst->get_offset().y(), inst->get_offset().z()}},
                {"rotation", {inst->get_rotation().x(), inst->get_rotation().y(), inst->get_rotation().z()}},
                {"scale", {inst->get_scaling_factor().x(), inst->get_scaling_factor().y(), inst->get_scaling_factor().z()}},
            }},
            {"bbox", {
                {"min", {bb.min.x(), bb.min.y(), bb.min.z()}},
                {"max", {bb.max.x(), bb.max.y(), bb.max.z()}},
            }},
            {"printable", bb.max.x() >= min_x && bb.min.x() <= max_x && bb.max.y() >= min_y && bb.min.y() <= max_y},
        });
    }
    scene["objects"] = objects;
    scene["warnings"] = nlohmann::json::array();
    scene["actions"] = k_actions;
    return scene;
}
```

- [ ] **Step 2: Implement import operation**

Add below helpers:

```cpp
int run_import(const StlDraftRequest& req, StlDraftResponse& response) {
    if (req.input_stl.empty() || req.output_3mf.empty()) {
        return fail("invalid_request", "input_stl and output_3mf are required", response);
    }
    Slic3r::DynamicPrintConfig cfg = minimal_bed_config();
    Slic3r::ConfigSubstitutionContext subs_ctx(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    Slic3r::Model model;
    try {
        model = Slic3r::Model::read_from_file(
            req.input_stl,
            nullptr,
            &subs_ctx,
            Slic3r::LoadStrategy::LoadModel | Slic3r::LoadStrategy::AddDefaultInstances);
    } catch (const std::exception& e) {
        return fail("invalid_stl", std::string("read_from_file: ") + e.what(), response);
    }
    if (model.objects.empty()) {
        return fail("invalid_stl", "STL file contains no objects", response);
    }
    if (req.center) model.center_instances_around_point(bed_center(cfg));
    ground_all(model);

    try {
        Slic3r::store_bbs_3mf(req.output_3mf, &model, nullptr, Slic3r::SaveStrategy::SaveModel);
    } catch (const std::exception& e) {
        return fail("export_failed", std::string("store_bbs_3mf: ") + e.what(), response);
    }

    response.status = "ok";
    response.scene = scene_for_model(req, model, cfg);
    write_stl_draft_response_to_stdout(response);
    return 0;
}
```

- [ ] **Step 3: Dispatch import**

Replace `run_stl_draft_mode`:

```cpp
int run_stl_draft_mode(const StlDraftRequest& req) {
    StlDraftResponse response;
    if (req.operation == "import") return run_import(req, response);
    if (req.operation == "layout" || req.operation == "export_3mf") {
        return fail("unsupported_operation", req.operation + " is unavailable in this build", response);
    }
    return fail("invalid_request", "operation must be import, layout, or export_3mf", response);
}
```

- [ ] **Step 4: Build and smoke import**

Run in C++ dev container:

```bash
cmake --build build --target orca-headless -j2
printf 'solid tri\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 20 0 0\n  vertex 0 20 0\n endloop\nendfacet\nendsolid tri\n' > /tmp/tri.stl
printf '{"operation":"import","draft_token":"d1","input_stl":"/tmp/tri.stl","output_3mf":"/tmp/tri.3mf","options":{"center":true}}' | ./build/orca-headless stl-draft
test -s /tmp/tri.3mf
```

Expected: JSON `status` is `ok`, `scene.objects[0].printable` is `true`, and `/tmp/tri.3mf` exists.

- [ ] **Step 5: Commit**

Run:

```bash
git add cpp/src/stl_draft_mode.cpp
git commit -m "feat(binary): import STL draft scenes"
```

---

### Task 6: C++ Layout And Export Operations

**Files:**
- Modify: `cpp/src/stl_draft_mode.cpp`

- [ ] **Step 1: Add rotate, center, and export helpers**

Modify `cpp/src/stl_draft_mode.cpp`:

```cpp
Slic3r::Model read_3mf_model(const std::string& path) {
    Slic3r::DynamicPrintConfig cfg;
    Slic3r::ConfigSubstitutionContext subs_ctx(
        Slic3r::ForwardCompatibilitySubstitutionRule::EnableSilent);
    return Slic3r::Model::read_from_file(
        path,
        &cfg,
        &subs_ctx,
        Slic3r::LoadStrategy::LoadModel | Slic3r::LoadStrategy::LoadAuxiliary);
}

void rotate_z(Slic3r::Model& model, double radians) {
    for (auto* obj : model.objects) {
        if (!obj) continue;
        for (auto* inst : obj->instances) {
            if (inst) inst->rotate(radians, Slic3r::Axis::Z);
        }
        obj->invalidate_bounding_box();
    }
    ground_all(model);
}

void copy_file_or_throw(const std::string& src, const std::string& dst) {
    std::filesystem::copy_file(
        src, dst,
        std::filesystem::copy_options::overwrite_existing);
}
```

- [ ] **Step 2: Add layout/export functions**

Add:

```cpp
int run_layout(const StlDraftRequest& req, StlDraftResponse& response) {
    if (req.input_3mf.empty() || req.output_3mf.empty() || req.action.empty()) {
        return fail("invalid_request", "input_3mf, output_3mf, and action are required", response);
    }
    Slic3r::DynamicPrintConfig cfg = minimal_bed_config();
    Slic3r::Model model;
    try {
        model = read_3mf_model(req.input_3mf);
    } catch (const std::exception& e) {
        return fail("invalid_draft", std::string("read_from_file: ") + e.what(), response);
    }

    if (req.action == "center") {
        model.center_instances_around_point(bed_center(cfg));
        ground_all(model);
    } else if (req.action == "rotate_z_90") {
        rotate_z(model, Slic3r::Geometry::deg2rad(90.0));
    } else if (req.action == "rotate_z_minus_90") {
        rotate_z(model, Slic3r::Geometry::deg2rad(-90.0));
    } else if (req.action == "reset") {
        try {
            copy_file_or_throw(req.input_3mf, req.output_3mf);
        } catch (const std::exception& e) {
            return fail("export_failed", std::string("copy reset draft: ") + e.what(), response);
        }
        response.status = "ok";
        response.scene = scene_for_model(req, model, cfg);
        write_stl_draft_response_to_stdout(response);
        return 0;
    } else if (req.action == "auto_orient" || req.action == "arrange") {
        return fail(
            req.action == "auto_orient" ? "auto_orient_failed" : "arrange_failed",
            req.action + " requires orientation/arrange support",
            response);
    } else {
        return fail("invalid_request", "unknown action: " + req.action, response);
    }

    try {
        Slic3r::store_bbs_3mf(req.output_3mf, &model, nullptr, Slic3r::SaveStrategy::SaveModel);
    } catch (const std::exception& e) {
        return fail("export_failed", std::string("store_bbs_3mf: ") + e.what(), response);
    }
    response.status = "ok";
    response.scene = scene_for_model(req, model, cfg);
    write_stl_draft_response_to_stdout(response);
    return 0;
}

int run_export_3mf(const StlDraftRequest& req, StlDraftResponse& response) {
    if (req.input_3mf.empty() || req.output_3mf.empty()) {
        return fail("invalid_request", "input_3mf and output_3mf are required", response);
    }
    try {
        copy_file_or_throw(req.input_3mf, req.output_3mf);
    } catch (const std::exception& e) {
        return fail("export_failed", std::string("copy export draft: ") + e.what(), response);
    }
    response.status = "ok";
    write_stl_draft_response_to_stdout(response);
    return 0;
}
```

Update dispatch:

```cpp
    if (req.operation == "import") return run_import(req, response);
    if (req.operation == "layout") return run_layout(req, response);
    if (req.operation == "export_3mf") return run_export_3mf(req, response);
```

- [ ] **Step 3: Build and smoke layout/export**

Run:

```bash
cmake --build build --target orca-headless -j2
printf '{"operation":"layout","draft_token":"d1","input_3mf":"/tmp/tri.3mf","output_3mf":"/tmp/tri-next.3mf","action":"rotate_z_90"}' | ./build/orca-headless stl-draft
test -s /tmp/tri-next.3mf
printf '{"operation":"export_3mf","draft_token":"d1","input_3mf":"/tmp/tri-next.3mf","output_3mf":"/tmp/tri-final.3mf"}' | ./build/orca-headless stl-draft
test -s /tmp/tri-final.3mf
```

Expected: both commands return `{"status":"ok", ...}` or `{"status":"ok"}` and output files exist.

- [ ] **Step 4: Commit**

Run:

```bash
git add cpp/src/stl_draft_mode.cpp
git commit -m "feat(binary): layout and export STL drafts"
```

---

### Task 7: Auto-Orient And Arrange Parity

**Files:**
- Modify: `cpp/src/stl_draft_mode.cpp`

- [ ] **Step 1: Wire auto-orient through libslic3r orientation**

Add include:

```cpp
#include "libslic3r/Orient.hpp"
```

Add helper:

```cpp
void auto_orient_all(Slic3r::Model& model) {
    Slic3r::orientation::OrientMeshs selected;
    Slic3r::orientation::OrientMeshs unselected;
    for (auto* obj : model.objects) {
        if (!obj) continue;
        for (auto* inst : obj->instances) {
            if (!inst) continue;
            Slic3r::orientation::OrientMesh om;
            om.name = obj->name;
            om.mesh = obj->mesh();
            om.setter = [inst, obj](const Slic3r::orientation::OrientMesh& p) {
                inst->rotate(p.rotation_matrix);
                obj->invalidate_bounding_box();
                obj->ensure_on_bed(false);
            };
            selected.emplace_back(std::move(om));
        }
    }
    Slic3r::orientation::OrientParams params;
    params.min_volume = true;  // GUI default when OrientSettings.min_area is false.
    params.progressind = [](unsigned, std::string) {};
    Slic3r::orientation::orient(selected, unselected, params);
    for (auto& mesh : selected) mesh.apply();
    ground_all(model);
}
```

In `run_layout`, replace the `auto_orient` failure branch:

```cpp
    } else if (req.action == "auto_orient") {
        try {
            auto_orient_all(model);
        } catch (const std::exception& e) {
            return fail("auto_orient_failed", e.what(), response);
        }
    } else if (req.action == "arrange") {
```

- [ ] **Step 2: Wire arrange through existing arrange pipeline**

Add includes:

```cpp
#include "libslic3r/Arrange.hpp"
#include "libslic3r/ModelArrange.hpp"
```

Add helper adapted from `cpp/src/slice_mode.cpp::arrange_instances_or_fail`:

```cpp
bool arrange_draft_instances(Slic3r::Model& model, const Slic3r::DynamicPrintConfig& cfg, std::string& error) {
    using namespace Slic3r::arrangement;
    ArrangePolygons movable;
    std::vector<Slic3r::ModelInstance*> instances;
    for (auto* obj : model.objects) {
        if (!obj) continue;
        for (auto* inst : obj->instances) {
            ArrangePolygon ap = Slic3r::get_instance_arrange_poly(inst, cfg);
            ap.itemid = static_cast<int>(movable.size());
            instances.push_back(inst);
            movable.emplace_back(std::move(ap));
        }
    }
    if (movable.empty()) return true;

    ArrangeParams params;
    params.allow_rotations = true;
    params.is_seq_print = false;
    params.min_obj_distance = 0;
    params.progressind = [](unsigned, std::string) {};
    update_arrange_params(params, &cfg, movable);
    update_selected_items_inflation(movable, &cfg, params);
    update_selected_items_axis_align(movable, &cfg, params);

    Slic3r::Points bedpts = get_shrink_bedpts(&cfg, params);
    if (bedpts.size() < 3) {
        error = "printable area has fewer than 3 points";
        return false;
    }
    arrange(movable, {}, bedpts, params);
    for (size_t i = 0; i < movable.size(); ++i) {
        const auto& ap = movable[i];
        if (!ap.is_arranged() || ap.bed_idx != 0) {
            error = "Cannot place STL draft on bed";
            return false;
        }
        instances[i]->apply_arrange_result(ap.translation.cast<double>(), ap.rotation);
    }
    ground_all(model);
    return true;
}
```

In `run_layout`, replace the `arrange` failure branch:

```cpp
    } else if (req.action == "arrange") {
        std::string error;
        if (!arrange_draft_instances(model, cfg, error)) {
            return fail("arrange_failed", error, response);
        }
```

- [ ] **Step 3: Apply import options**

In `run_import`, after optional center and before writing:

```cpp
    if (req.auto_orient) {
        try {
            auto_orient_all(model);
        } catch (const std::exception& e) {
            return fail("auto_orient_failed", e.what(), response);
        }
    }
    if (req.arrange) {
        std::string error;
        if (!arrange_draft_instances(model, cfg, error)) {
            return fail("arrange_failed", error, response);
        }
    }
    ground_all(model);
```

- [ ] **Step 4: Build and smoke auto-orient/arrange**

Run:

```bash
cmake --build build --target orca-headless -j2
printf '{"operation":"layout","draft_token":"d1","input_3mf":"/tmp/tri.3mf","output_3mf":"/tmp/tri-orient.3mf","action":"auto_orient"}' | ./build/orca-headless stl-draft
printf '{"operation":"layout","draft_token":"d1","input_3mf":"/tmp/tri-orient.3mf","output_3mf":"/tmp/tri-arranged.3mf","action":"arrange"}' | ./build/orca-headless stl-draft
```

Expected: both commands return `status: ok`.

- [ ] **Step 5: Commit**

Run:

```bash
git add cpp/src/stl_draft_mode.cpp
git commit -m "feat(binary): auto-orient and arrange STL drafts"
```

---

### Task 8: Integration Tests

**Files:**
- Create: `tests/integration/test_stl_import.py`

- [ ] **Step 1: Write opt-in integration tests**

Create `tests/integration/test_stl_import.py`:

```python
from __future__ import annotations

import io
import json
import os
import urllib.request
import uuid
import zipfile

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8070")


def _container_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_reachable(),
    reason=f"orcaslicer-headless not reachable at {API}",
)


ASCII_STL = b"""solid tri
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 20 0 0
  vertex 0 20 0
 endloop
endfacet
endsolid tri
"""


def _post_multipart(url: str, fields: dict[str, str], file_name: str, file_bytes: bytes) -> dict:
    boundary = f"----pytest{uuid.uuid4().hex}"
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(str(value).encode())
        body.write(b"\r\n")
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode())
    body.write(b"Content-Type: application/sla\r\n\r\n")
    body.write(file_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60.0) as r:
        return r.read()


def test_stl_import_layout_and_materialize_3mf() -> None:
    scene = _post_multipart(
        f"{API}/stl/import",
        {
            "machine_id": "GM020",
            "process_id": "GP000",
            "center": "true",
            "arrange": "true",
            "auto_orient": "false",
        },
        "tri.stl",
        ASCII_STL,
    )
    assert scene["draft_token"]
    assert scene["objects"][0]["printable"] is True

    rotated = _post_json(
        f"{API}/stl/{scene['draft_token']}/layout",
        {"action": "rotate_z_90"},
    )
    assert rotated["draft_token"] == scene["draft_token"]

    materialized = _post_json(f"{API}/stl/{scene['draft_token']}/3mf")
    assert materialized["input_token"]

    threemf = _get_bytes(f"{API}/3mf/{materialized['input_token']}")
    with zipfile.ZipFile(io.BytesIO(threemf)) as zf:
        assert "3D/3dmodel.model" in zf.namelist()
```

- [ ] **Step 2: Run integration test against running container**

Run:

```bash
ORCASLICER_API=http://localhost:8070 pytest tests/integration/test_stl_import.py -q
```

Expected: PASS when container is running; SKIP when not reachable.

- [ ] **Step 3: Commit**

Run:

```bash
git add tests/integration/test_stl_import.py
git commit -m "test(integration): cover STL draft import flow"
```

---

### Task 9: Final Verification And Docs

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-17-stl-preview-import-design.md` only if implementation intentionally diverged

- [ ] **Step 1: Add README API note**

Add a short STL section to `README.md`:

```markdown
### STL Draft Preview API

STL support is preview-first. Upload an STL with `POST /stl/import`,
apply optional preset layout actions through `POST /stl/{draft_token}/layout`,
then materialize the accepted draft with `POST /stl/{draft_token}/3mf`.
The materialized token is a normal 3MF token and can be used with
`GET /3mf/{token}/inspect` and `POST /slice/v2`.

The gateway/browser renders the original STL using the scene transform
returned by these endpoints. `orcaslicer-headless` remains the source of
truth for import, orientation, arrange, and 3MF generation.
```

- [ ] **Step 2: Run unit tests**

Run:

```bash
pytest tests/test_stl_drafts.py tests/test_binary_client_stl.py tests/test_stl_endpoints.py -q
```

Expected: PASS.

- [ ] **Step 3: Run existing affected tests**

Run:

```bash
pytest tests/test_cache.py tests/test_3mf_endpoints.py tests/test_slice_token_endpoint.py -q
```

Expected: PASS.

- [ ] **Step 4: Build C++ target**

Run in C++ dev environment:

```bash
cmake --build build --target orca-headless -j2
```

Expected: build succeeds.

- [ ] **Step 5: Run integration test with Docker service**

Run:

```bash
docker compose up --build
ORCASLICER_API=http://localhost:8070 pytest tests/integration/test_stl_import.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit docs and final fixes**

Run:

```bash
git add README.md docs/superpowers/specs/2026-05-17-stl-preview-import-design.md
git commit -m "docs(api): document STL draft preview API"
```

---

## Self-Review Notes

- Spec coverage: This plan covers `orcaslicer-headless` draft import, layout actions, materialization, API tests, C++ stateless binary mode, and integration tests. It intentionally does not implement `bambu-gateway`; the spec says gateway rendering is required, but that is a separate repo and should receive a follow-up plan once this API contract is implemented.
- Type consistency: The plan consistently uses `draft_token`, `operation`, `input_stl`, `input_3mf`, `output_3mf`, `scene`, and action values from `StlDraftAction`.
- Known implementation risk: `stl-draft` import needs enough machine/process config to match GUI bed shape and arrange behavior. If C++ discovers additional config keys are required, add them to `materialize_machine_process_for_binary` rather than routing STL drafts through filament materialization.
