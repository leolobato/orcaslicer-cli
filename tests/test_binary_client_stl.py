from __future__ import annotations

import asyncio
import json

import pytest

from app.binary_client import BinaryClient, BinaryError


class _FakeProcess:
    def __init__(
        self,
        stdout_payload: dict,
        returncode: int = 0,
        raw_stdout: bytes | None = None,
        raw_stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self.stdout_payload = stdout_payload
        self.raw_stdout = raw_stdout
        self.raw_stderr = raw_stderr
        self.stdin_data: bytes | None = None
        self.kill_called = False
        self.wait_called = False

    async def communicate(self, input: bytes = b""):
        self.stdin_data = input
        stdout = self.raw_stdout
        if stdout is None:
            stdout = json.dumps(self.stdout_payload).encode()
        return stdout, self.raw_stderr

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    async def wait(self):
        self.wait_called = True
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


@pytest.mark.asyncio
async def test_stl_draft_timeout_kills_and_waits(monkeypatch):
    proc = _FakeProcess({"status": "ok"})

    async def fake_exec(*args, **kwargs):
        return proc

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    client = BinaryClient("/bin/orca-headless")

    with pytest.raises(BinaryError) as exc:
        await client.stl_draft({"operation": "import"}, timeout_s=0.1)

    assert exc.value.code == "binary_timeout"
    assert proc.kill_called is True
    assert proc.wait_called is True


@pytest.mark.asyncio
async def test_stl_draft_crash_without_stdout_includes_stderr_tail(monkeypatch):
    proc = _FakeProcess({}, returncode=2, raw_stdout=b"", raw_stderr=b"boom")

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BinaryClient("/bin/orca-headless")

    with pytest.raises(BinaryError) as exc:
        await client.stl_draft({"operation": "import"})

    assert exc.value.code == "binary_crashed"
    assert "boom" in exc.value.stderr_tail


@pytest.mark.asyncio
async def test_stl_draft_bad_json_reports_stdout_head(monkeypatch):
    proc = _FakeProcess({}, raw_stdout=b"not-json")

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BinaryClient("/bin/orca-headless")

    with pytest.raises(BinaryError) as exc:
        await client.stl_draft({"operation": "import"})

    assert exc.value.code == "binary_bad_response"
    assert "not-json" in exc.value.details["stdout_head"]
