# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Scripted provider -- deterministic Python scripts in bwrap sandbox.

Long-lived subprocess, stdin/stdout JSON lines protocol.
Tool calls bridged to daemon RPC. Turn history is the session state.
See docs/design/providers/scripted.md for the full design.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import UUID

from substrat.logging import EventLog, log_method
from substrat.model import CommandWrapper

logger = logging.getLogger(__name__)

# How long to wait for the script to exit after closing stdin.
_STOP_TIMEOUT = 5.0

# How long to wait for a single stdout line from the script.
_READ_TIMEOUT = 60.0

# Helper library path -- bind-mounted into the sandbox.
_HELPER = Path(__file__).parent / "substrat_script.py"


class ScriptedSession:
    """A live conversation with a scripted agent subprocess.

    The process stays alive across turns, blocking on stdin between them.
    State is the process memory. On suspend/restore, the turn history
    is serialized and replayed through a fresh process.
    """

    def __init__(
        self,
        script_path: Path,
        workspace: Path | None = None,
        log: EventLog | None = None,
        wrap_command: CommandWrapper | None = None,
        agent_id: UUID | None = None,
        daemon_socket: str | None = None,
    ) -> None:
        self._script_path = script_path
        self._workspace = workspace
        self._log = log
        self._wrap_command = wrap_command
        self._agent_id = agent_id
        self._daemon_socket = daemon_socket
        self._proc: asyncio.subprocess.Process | None = None
        self._history: list[dict[str, Any]] = []
        self._fresh: bool = True  # Process not yet spawned or just respawned.
        self._stderr_task: asyncio.Task[bytes] | None = None

    async def _spawn(self) -> asyncio.subprocess.Process:
        """Spawn the script subprocess."""
        cmd: list[str] = ["python3", str(self._script_path)]
        if self._wrap_command is not None:
            cmd = list(self._wrap_command(cmd, (), {}))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        self._fresh = True
        # Drain stderr in background to prevent pipe buffer deadlock.
        assert proc.stderr is not None
        self._stderr_task = asyncio.create_task(proc.stderr.read())
        return proc

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        """Return a live process, spawning if needed."""
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        return await self._spawn()

    async def _read_line(self) -> str:
        """Read one line from stdout, with timeout."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=_READ_TIMEOUT
            )
        except TimeoutError:
            raise RuntimeError(
                f"scripted agent timed out after {_READ_TIMEOUT}s"
            ) from None
        if not line:
            stderr = await self._drain_stderr()
            raise RuntimeError(
                "scripted agent exited unexpectedly" + (f": {stderr}" if stderr else "")
            )
        return line.decode()

    async def _drain_stderr(self) -> str:
        """Collect whatever stderr the process has produced."""
        if self._stderr_task is None:
            return ""
        with contextlib.suppress(asyncio.CancelledError):
            try:
                data = await asyncio.wait_for(self._stderr_task, timeout=1.0)
                return data.decode().strip()
            except TimeoutError:
                return ""
        return ""

    @log_method(before=True, after=True)
    async def send(self, message: str) -> AsyncGenerator[str, None]:
        """Send a turn message, dispatch tool calls, yield final response."""
        proc = await self._ensure_proc()
        assert proc.stdin is not None

        # Build turn payload.
        turn_msg: dict[str, Any] = {"type": "turn", "message": message}
        if self._fresh and self._history:
            turn_msg["history"] = self._history
        self._fresh = False

        # Write turn to stdin.
        proc.stdin.write((json.dumps(turn_msg) + "\n").encode())
        await proc.stdin.drain()

        # Track in-progress turn.
        current: dict[str, Any] = {"message": message, "calls": []}

        # Read loop: tool calls until done.
        while True:
            raw = await self._read_line()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("scripted agent sent invalid JSON: %s", raw.strip())
                continue

            msg_type = msg.get("type")

            if msg_type == "call":
                result = await self._dispatch_tool(msg)
                current["calls"].append(result)
                # Send result back to script.
                if "error" in result:
                    resp = {"type": "result", "id": msg["id"], "error": result["error"]}
                else:
                    resp = {"type": "result", "id": msg["id"], "data": result["result"]}
                proc.stdin.write((json.dumps(resp) + "\n").encode())
                await proc.stdin.drain()

            elif msg_type == "done":
                response = msg.get("response", "")
                current["response"] = response
                self._history.append(current)
                yield response
                return

            elif msg_type == "error":
                raise RuntimeError(
                    f"scripted agent error: {msg.get('message', 'unknown')}"
                )
            else:
                logger.warning("scripted agent sent unknown type: %s", msg_type)

    async def _dispatch_tool(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call via daemon RPC. Returns {tool, args, result/error}."""
        from substrat.rpc import async_call

        tool_name = msg["tool"]
        args = msg.get("args", {})
        entry: dict[str, Any] = {"tool": tool_name, "args": args}

        if self._daemon_socket is None or self._agent_id is None:
            entry["error"] = "no daemon connection"
            return entry

        try:
            resp = await async_call(
                self._daemon_socket,
                "tool.call",
                {
                    "agent_id": self._agent_id.hex,
                    "tool": tool_name,
                    "arguments": args,
                },
            )
        except Exception as exc:
            entry["error"] = str(exc)
            return entry

        if "error" in resp:
            entry["error"] = resp["error"]
        else:
            entry["result"] = resp
        return entry

    @log_method(after=True)
    async def suspend(self) -> bytes:
        """Serialize session state -- script path, workspace, turn history."""
        blob: dict[str, Any] = {
            "script": str(self._script_path),
            "history": self._history,
        }
        if self._workspace is not None:
            blob["workspace"] = str(self._workspace)
        if self._agent_id is not None:
            blob["agent_id"] = self._agent_id.hex
        if self._daemon_socket is not None:
            blob["daemon_socket"] = self._daemon_socket
        return json.dumps(blob).encode()

    @log_method(after=True)
    async def stop(self) -> None:
        """Shut down the script subprocess."""
        if self._proc is None or self._proc.returncode is not None:
            return
        # Close stdin -- script sees EOF and should exit.
        assert self._proc.stdin is not None
        self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=_STOP_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()
        # Cancel stderr drain.
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task


class ScriptedProvider:
    """Factory for scripted agent sessions."""

    @property
    def name(self) -> str:
        return "scripted"

    async def create(
        self,
        model: str | None,
        system_prompt: str,
        log: EventLog | None = None,
        *,
        workspace: Path | None = None,
        wrap_command: CommandWrapper | None = None,
        agent_id: UUID | None = None,
        daemon_socket: str | None = None,
    ) -> ScriptedSession:
        """Create a new scripted session. model is the script path."""
        if not model:
            raise ValueError("scripted provider requires model (script path)")
        script_path = Path(model)
        if not script_path.is_absolute() and workspace is not None:
            script_path = workspace / script_path
        if log is not None:
            log.log(
                "session.created",
                {
                    "provider": self.name,
                    "script": str(script_path),
                    "workspace": str(workspace) if workspace else None,
                },
            )
        return ScriptedSession(
            script_path=script_path,
            workspace=workspace,
            log=log,
            wrap_command=wrap_command,
            agent_id=agent_id,
            daemon_socket=daemon_socket,
        )

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
        *,
        wrap_command: CommandWrapper | None = None,
    ) -> ScriptedSession:
        """Restore from a suspended state blob."""
        data = json.loads(state.decode())
        ws = Path(data["workspace"]) if data.get("workspace") else None
        agent_id_hex = data.get("agent_id")
        session = ScriptedSession(
            script_path=Path(data["script"]),
            workspace=ws,
            log=log,
            wrap_command=wrap_command,
            agent_id=UUID(agent_id_hex) if agent_id_hex else None,
            daemon_socket=data.get("daemon_socket"),
        )
        session._history = data.get("history", [])
        # Process is dead after restore. Next send() spawns fresh with replay.
        if log is not None:
            log.log(
                "session.restored",
                {
                    "provider": "scripted",
                    "script": data["script"],
                    "history_turns": len(session._history),
                },
            )
        return session

    def models(self) -> list[str]:
        """Scripted provider has no fixed model list."""
        return []
