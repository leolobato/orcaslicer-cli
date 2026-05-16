from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.binary_client import BinaryError
from app.stl_drafts import StlDraftCache


class FakeBinary:
    def __init__(self) -> None:
        self.requests: list[dict] = []

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
                    "bed": {
                        "width": 180,
                        "depth": 180,
                        "printable_area": [[0, 0], [180, 0], [180, 180], [0, 180]],
                    },
                    "objects": [
                        {
                            "id": "0",
                            "name": "part.stl",
                            "transform": {
                                "offset": [90, 90, 0],
                                "rotation": [0, 0, 0],
                                "scale": [1, 1, 1],
                            },
                            "bbox": {"min": [80, 80, 0], "max": [100, 100, 20]},
                            "printable": True,
                        }
                    ],
                    "warnings": [],
                    "actions": [
                        "auto_orient",
                        "rotate_z_90",
                        "rotate_z_minus_90",
                        "center",
                        "arrange",
                        "reset",
                    ],
                },
            }
        if op == "export_3mf":
            Path(request["output_3mf"]).write_bytes(b"materialized-3mf")
            return {"status": "ok"}
        raise AssertionError(op)


class FailingImportBinary(FakeBinary):
    async def stl_draft(self, request, timeout_s=120.0):
        self.requests.append(request)
        if request["operation"] == "import":
            raise BinaryError(code="invalid_stl", message="bad STL", details={})
        return await super().stl_draft(request, timeout_s=timeout_s)


class FakeTokenCache:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def put(self, payload: bytes):
        self.payloads.append(payload)
        return "tok3mf", "sha", len(payload), []


@pytest.fixture
def stl_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_binary = FakeBinary()
    main.app.state.stl_drafts = StlDraftCache(tmp_path / "stl-drafts", ttl_seconds=3600)
    main.app.state.token_cache = FakeTokenCache()

    async def fake_materialize_machine_process_for_binary(machine_id: str, process_id: str):
        machine_dir = tmp_path / "machine"
        process_dir = tmp_path / "process"
        machine_dir.mkdir()
        process_dir.mkdir()
        return {
            "machine_chain_dir": str(machine_dir),
            "machine_leaf_name": "Mock Machine",
            "process_chain_dir": str(process_dir),
            "process_leaf_name": "Mock Process",
        }

    async def fail_materialize_profiles_for_binary(*args, **kwargs):
        raise AssertionError("STL import must not call filament-aware materializer")

    monkeypatch.setattr(
        main,
        "materialize_machine_process_for_binary",
        fake_materialize_machine_process_for_binary,
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "materialize_profiles_for_binary",
        fail_materialize_profiles_for_binary,
    )
    monkeypatch.setattr(main, "BinaryClient", lambda binary_path: fake_binary)

    client = TestClient(main.app)
    return client, fake_binary


def _import_stl(client: TestClient):
    return client.post(
        "/stl/import",
        files={"file": ("part.stl", b"solid part\nendsolid part\n", "model/stl")},
        data={
            "machine_id": "GM020",
            "process_id": "GP000",
            "auto_orient": "true",
            "arrange": "true",
            "center": "true",
        },
    )


def test_stl_import_returns_scene_and_writes_draft(stl_client) -> None:
    client, fake_binary = stl_client

    resp = _import_stl(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft_token"]
    assert body["objects"][0]["printable"] is True
    assert fake_binary.requests[0]["operation"] == "import"
    assert Path(fake_binary.requests[0]["output_3mf"]).read_bytes() == b"fake-3mf"


def test_stl_layout_rejects_unknown_action(stl_client) -> None:
    client, _fake_binary = stl_client
    draft_token = _import_stl(client).json()["draft_token"]

    resp = client.post(f"/stl/{draft_token}/layout", json={"action": "drag"})

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_stl_layout_action"


def test_stl_layout_updates_scene(stl_client) -> None:
    client, fake_binary = stl_client
    draft_token = _import_stl(client).json()["draft_token"]

    resp = client.post(f"/stl/{draft_token}/layout", json={"action": "center"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft_token"] == draft_token
    assert fake_binary.requests[-1]["operation"] == "layout"
    assert fake_binary.requests[-1]["action"] == "center"


def test_stl_export_returns_3mf_token(stl_client) -> None:
    client, fake_binary = stl_client
    draft_token = _import_stl(client).json()["draft_token"]

    resp = client.post(f"/stl/{draft_token}/3mf")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"input_token": "tok3mf", "draft_token": draft_token}
    assert fake_binary.requests[-1]["operation"] == "export_3mf"


def test_stl_import_uses_machine_process_only_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_binary = FakeBinary()
    main.app.state.stl_drafts = StlDraftCache(tmp_path / "stl-drafts", ttl_seconds=3600)
    main.app.state.token_cache = FakeTokenCache()

    async def fake_materialize_machine_process_for_binary(machine_id: str, process_id: str):
        return {
            "machine_chain_dir": str(tmp_path / "machine"),
            "machine_leaf_name": "Machine",
            "process_chain_dir": str(tmp_path / "process"),
            "process_leaf_name": "Process",
        }

    async def fail_materialize_profiles_for_binary(*args, **kwargs):
        raise AssertionError("STL import must not call filament-aware materializer")

    monkeypatch.setattr(
        main,
        "materialize_machine_process_for_binary",
        fake_materialize_machine_process_for_binary,
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "materialize_profiles_for_binary",
        fail_materialize_profiles_for_binary,
    )
    monkeypatch.setattr(main, "BinaryClient", lambda binary_path: fake_binary)

    resp = _import_stl(TestClient(main.app))

    assert resp.status_code == 200, resp.text


def test_stl_import_binary_failure_deletes_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_binary = FailingImportBinary()
    drafts = StlDraftCache(tmp_path / "stl-drafts", ttl_seconds=3600)
    profile_root = tmp_path / "orca-headless-stl-profiles-fail"
    (profile_root / "machine").mkdir(parents=True)
    (profile_root / "process").mkdir()
    main.app.state.stl_drafts = drafts
    main.app.state.token_cache = FakeTokenCache()

    async def fake_materialize_machine_process_for_binary(machine_id: str, process_id: str):
        return {
            "machine_chain_dir": str(profile_root / "machine"),
            "machine_leaf_name": "Machine",
            "process_chain_dir": str(profile_root / "process"),
            "process_leaf_name": "Process",
        }

    monkeypatch.setattr(
        main,
        "materialize_machine_process_for_binary",
        fake_materialize_machine_process_for_binary,
    )
    monkeypatch.setattr(main, "BinaryClient", lambda binary_path: fake_binary)

    resp = _import_stl(TestClient(main.app))

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_stl"
    assert drafts._drafts == {}
    assert list((tmp_path / "stl-drafts").iterdir()) == []


def test_stl_import_cleans_profile_temp_dirs_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_binary = FakeBinary()
    profile_root = tmp_path / "orca-headless-stl-profiles-success"
    (profile_root / "machine").mkdir(parents=True)
    (profile_root / "process").mkdir()
    main.app.state.stl_drafts = StlDraftCache(tmp_path / "stl-drafts", ttl_seconds=3600)
    main.app.state.token_cache = FakeTokenCache()

    async def fake_materialize_machine_process_for_binary(machine_id: str, process_id: str):
        return {
            "machine_chain_dir": str(profile_root / "machine"),
            "machine_leaf_name": "Machine",
            "process_chain_dir": str(profile_root / "process"),
            "process_leaf_name": "Process",
        }

    monkeypatch.setattr(
        main,
        "materialize_machine_process_for_binary",
        fake_materialize_machine_process_for_binary,
    )
    monkeypatch.setattr(main, "BinaryClient", lambda binary_path: fake_binary)

    resp = _import_stl(TestClient(main.app))

    assert resp.status_code == 200, resp.text
    assert not profile_root.exists()


def test_stl_import_cleans_profile_temp_dirs_on_binary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_binary = FailingImportBinary()
    profile_root = tmp_path / "orca-headless-stl-profiles-binary-fail"
    (profile_root / "machine").mkdir(parents=True)
    (profile_root / "process").mkdir()
    main.app.state.stl_drafts = StlDraftCache(tmp_path / "stl-drafts", ttl_seconds=3600)
    main.app.state.token_cache = FakeTokenCache()

    async def fake_materialize_machine_process_for_binary(machine_id: str, process_id: str):
        return {
            "machine_chain_dir": str(profile_root / "machine"),
            "machine_leaf_name": "Machine",
            "process_chain_dir": str(profile_root / "process"),
            "process_leaf_name": "Process",
        }

    monkeypatch.setattr(
        main,
        "materialize_machine_process_for_binary",
        fake_materialize_machine_process_for_binary,
    )
    monkeypatch.setattr(main, "BinaryClient", lambda binary_path: fake_binary)

    resp = _import_stl(TestClient(main.app))

    assert resp.status_code == 400, resp.text
    assert not profile_root.exists()
