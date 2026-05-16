"""Async wrapper around the orca-headless binary."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
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

    async def stl_draft(self, request: dict[str, Any], timeout_s: float = 120.0) -> dict[str, Any]:
        """Invoke ``orca-headless stl-draft`` and return the parsed response."""
        proc = await asyncio.create_subprocess_exec(
            self.binary_path,
            "stl-draft",
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

    async def dump_options(self, *, timeout_s: float = 30.0) -> dict[str, Any]:
        """Invoke ``orca-headless dump-options`` and return the catalogue dict.

        The binary writes the JSON catalogue to a temp file we create here.
        Unlike ``slice`` and ``use-set``, the ``dump-options`` subcommand
        emits its success/error envelope on **stderr** (the C++ side wires
        the dump command through a different logger sink). We therefore
        try stdout first and fall back to stderr — robust against the
        binary moving the envelope back to stdout in a future build.
        Returns the parsed catalogue (``{"options": [...]}``); raises
        ``BinaryError`` on a non-OK envelope or subprocess failure.
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            out_path = tf.name
        try:
            request = {"out_path": out_path}
            proc = await asyncio.create_subprocess_exec(
                self.binary_path, "dump-options",
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
                    message=f"dump-options timed out after {timeout_s}s",
                    details={},
                )

            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

            if proc.returncode != 0 and not stdout.strip() and not stderr.strip():
                raise BinaryError(
                    code="binary_crashed",
                    message=f"dump-options exited {proc.returncode} with no output",
                    details={},
                    stderr_tail=stderr_text[-2000:],
                )

            envelope_source = stdout if stdout.strip() else stderr
            try:
                envelope = json.loads(envelope_source)
            except json.JSONDecodeError as e:
                raise BinaryError(
                    code="binary_bad_response",
                    message=f"could not parse envelope as JSON: {e}",
                    details={
                        "stdout_head": stdout[:500].decode("utf-8", errors="replace"),
                        "stderr_head": stderr[:500].decode("utf-8", errors="replace"),
                    },
                    stderr_tail=stderr_text[-2000:],
                )

            if envelope.get("status") != "ok":
                raise BinaryError(
                    code=envelope.get("code", "unknown"),
                    message=envelope.get("message", ""),
                    details=envelope.get("details", {}),
                    stderr_tail=stderr_text[-2000:],
                )

            with open(out_path, "r", encoding="utf-8") as f:
                return json.load(f)
        finally:
            Path(out_path).unlink(missing_ok=True)

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

        # Non-JSON stderr lines are the binary's debug output and any abort
        # message printed before SIGSEGV. Keep the tail so we can surface it
        # alongside `binary_crashed` errors instead of leaving callers with
        # only "exit -11" to debug from.
        stderr_lines: list[str] = []

        async def pump_stderr() -> AsyncIterator[dict[str, Any]]:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    e = json.loads(stripped)
                    yield {"type": "progress", "payload": e}
                except json.JSONDecodeError:
                    text = stripped.decode("utf-8", errors="replace")
                    stderr_lines.append(text)
                    if len(stderr_lines) > 200:
                        del stderr_lines[: len(stderr_lines) - 200]
                    logger.debug("non-JSON stderr line from binary: %r", stripped)

        async for ev in pump_stderr():
            yield ev

        stdout = await proc.stdout.read()
        rc = await proc.wait()
        stderr_tail = "\n".join(stderr_lines)[-2000:]

        # Capture the raw stdout for diagnostics regardless of outcome —
        # the streaming path used to discard it on error, leaving callers
        # with only the JSONDecodeError text and no way to see what the
        # binary actually wrote (or didn't write). Surfaced via
        # ``details.stdout_head`` / ``details.stdout_len`` so callers that
        # consume structured fields can read it; the message itself stays
        # clean.
        stdout_head = stdout[:1000].decode("utf-8", errors="replace")
        stdout_len = len(stdout)

        if rc != 0 and not stdout.strip():
            yield {"type": "error", "payload": {
                "code": "binary_crashed",
                "message": f"exit {rc}",
                "stderr_tail": stderr_tail,
                "details": {"stdout_head": stdout_head, "stdout_len": stdout_len},
            }}
            return
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as e:
            yield {"type": "error", "payload": {
                "code": "binary_bad_response",
                "message": str(e),
                "stderr_tail": stderr_tail,
                "details": {"stdout_head": stdout_head, "stdout_len": stdout_len},
            }}
            return
        if response.get("status") != "ok":
            payload = dict(response)
            if stderr_tail and "stderr_tail" not in payload:
                payload["stderr_tail"] = stderr_tail
            yield {"type": "error", "payload": payload}
            return
        yield {"type": "result", "payload": response}
