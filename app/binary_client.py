"""Async wrapper around the orca-headless binary."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class BinaryError(Exception):
    code: str
    message: str
    details: dict[str, Any]
    stderr_tail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class BinaryClient:
    def __init__(self, binary_path: str, slice_timeout_sec: int = 300) -> None:
        self.binary_path = binary_path
        self.slice_timeout_sec = slice_timeout_sec

    async def slice(self, request: dict[str, Any]) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            self.binary_path, "slice",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=json.dumps(request).encode()),
                timeout=self.slice_timeout_sec,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BinaryError(code="binary_timeout", message="slice exceeded timeout", details={})

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        if proc.returncode != 0 and not stdout.strip():
            raise BinaryError(
                code="binary_crashed",
                message=f"orca-headless exited {proc.returncode} with no stdout",
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

    async def use_set(self, *, input_3mf: str, timeout_s: float = 30.0) -> dict[str, Any]:
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
                code=response.get("code", "unknown"),
                message=response.get("message", ""),
                details=response.get("details", {}),
                stderr_tail=stderr_text[-2000:],
            )

        return response

    async def slice_stream(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        proc = await asyncio.create_subprocess_exec(
            self.binary_path, "slice",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(json.dumps(request).encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def pump_stderr() -> AsyncIterator[dict[str, Any]]:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    yield {"type": "progress", "payload": e}
                except json.JSONDecodeError:
                    logger.debug("non-JSON stderr line from binary: %r", line)

        async for ev in pump_stderr():
            yield ev

        stdout = await proc.stdout.read()
        rc = await proc.wait()

        if rc != 0 and not stdout.strip():
            yield {"type": "error", "payload": {"code": "binary_crashed", "message": f"exit {rc}"}}
            return
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as e:
            yield {"type": "error", "payload": {"code": "binary_bad_response", "message": str(e)}}
            return
        if response.get("status") != "ok":
            yield {"type": "error", "payload": response}
            return
        yield {"type": "result", "payload": response}
