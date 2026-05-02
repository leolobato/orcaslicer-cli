import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.binary_client import BinaryClient, BinaryError


@pytest.fixture
def client() -> BinaryClient:
    return BinaryClient(binary_path="/opt/orca-headless/bin/orca-headless")


async def test_slice_returns_response(client: BinaryClient, tmp_path: Path) -> None:
    fake_response = {
        "status": "ok",
        "output_3mf": "/tmp/out.3mf",
        "estimate": {"time_seconds": 100, "weight_g": 5.0, "filament_used_m": [1.0]},
        "settings_transfer": {},
    }
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(json.dumps(fake_response).encode(), b""))
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        result = await client.slice(request={
            "input_3mf": "/tmp/in.3mf",
            "output_3mf": "/tmp/out.3mf",
            "machine_profile": "/tmp/m.json",
            "process_profile": "/tmp/p.json",
            "filament_profiles": ["/tmp/f.json"],
        })
    assert result["status"] == "ok"
    assert result["estimate"]["time_seconds"] == 100


async def test_slice_raises_on_error_status(client: BinaryClient) -> None:
    err = {"status": "error", "code": "invalid_3mf", "message": "bad zip"}
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(json.dumps(err).encode(), b""))
    mock_proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        with pytest.raises(BinaryError) as excinfo:
            await client.slice(request={"input_3mf": "/x"})
    assert excinfo.value.code == "invalid_3mf"


async def test_slice_raises_on_crash_no_json(client: BinaryClient) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"segfault\n"))
    mock_proc.returncode = -11
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        with pytest.raises(BinaryError) as excinfo:
            await client.slice(request={"input_3mf": "/x"})
    assert excinfo.value.code == "binary_crashed"


async def test_slice_stream_yields_progress_then_result(client: BinaryClient) -> None:
    progress_lines = [
        b'{"phase":"loading_3mf","percent":0}\n',
        b'{"phase":"slicing","percent":50}\n',
        b'{"phase":"done","percent":100}\n',
    ]
    final_response = {
        "status": "ok",
        "output_3mf": "/tmp/out.3mf",
        "estimate": {"time_seconds": 1, "weight_g": 0.1, "filament_used_m": []},
        "settings_transfer": {},
    }

    class FakeStream:
        def __init__(self, lines: list[bytes]):
            self._lines = list(lines)
        async def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""

    mock_proc = AsyncMock()
    mock_proc.stderr = FakeStream(progress_lines)
    mock_proc.stdout = AsyncMock()
    mock_proc.stdout.read = AsyncMock(return_value=json.dumps(final_response).encode())
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = 0
    mock_proc.stdin = AsyncMock()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        events = []
        async for ev in client.slice_stream(request={"input_3mf": "/x"}):
            events.append(ev)

    types = [e["type"] for e in events]
    assert types == ["progress", "progress", "progress", "result"]
    assert events[-1]["payload"]["status"] == "ok"
