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

    result = await client.stl_draft(
        {
            "operation": "import",
            "input_stl": "/tmp/source.stl",
            "output_3mf": "/tmp/current.3mf",
        }
    )

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
