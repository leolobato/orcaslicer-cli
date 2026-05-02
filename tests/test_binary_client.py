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
