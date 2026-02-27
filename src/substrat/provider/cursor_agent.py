# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Cursor CLI agent provider — spawns cursor-agent as a subprocess."""

import asyncio
import json
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

from substrat.logging import EventLog, log_method


def _cursor_binary() -> str:
    """Find the cursor-agent binary."""
    path = shutil.which("cursor-agent")
    if path is None:
        raise RuntimeError("cursor-agent not found in PATH")
    return path


class CursorSession:
    """A live conversation with cursor-agent.

    Each send() spawns a new subprocess with --resume to continue
    the local session.
    """

    def __init__(
        self,
        session_id: str,
        model: str,
        workspace: Path,
        log: EventLog | None = None,
    ) -> None:
        self._session_id = session_id
        self._model = model
        self._workspace = workspace
        self._log = log

    @property
    def session_id(self) -> str:
        return self._session_id

    @log_method(before=True, after=True)
    async def send(self, message: str) -> AsyncGenerator[str, None]:
        """Send a message, yield the final response text."""
        proc = await asyncio.create_subprocess_exec(
            *self._build_cmd(message),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        async for line in proc.stdout:
            decoded = line.decode().strip()
            if not decoded:
                continue
            try:
                event = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            # Final assistant message (no timestamp_ms = not a partial delta).
            if etype == "assistant" and "timestamp_ms" not in event:
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        yield block["text"]
            # Error check.
            if etype == "result" and event.get("is_error"):
                raise RuntimeError(event.get("result", "cursor-agent error"))
        await proc.wait()

    @log_method(after=True)
    async def suspend(self) -> bytes:
        """Serialize state — session ID, model, workspace path."""
        state = {
            "session_id": self._session_id,
            "model": self._model,
            "workspace": str(self._workspace),
        }
        return json.dumps(state).encode()

    @log_method(after=True)
    async def stop(self) -> None:
        """Nothing to clean up — subprocesses are per-send."""
        if self._log is not None:
            self._log.close()

    def _build_cmd(self, prompt: str) -> list[str]:
        return [
            _cursor_binary(),
            "--print",
            "--output-format",
            "stream-json",
            "--trust",
            "--model",
            self._model,
            "--workspace",
            str(self._workspace),
            "--resume",
            self._session_id,
            prompt,
        ]


class CursorAgentProvider:
    """Factory for cursor-agent sessions.

    The caller owns the EventLog — this provider just writes events to it.
    """

    @property
    def name(self) -> str:
        return "cursor-agent"

    async def create(
        self,
        model: str,
        system_prompt: str,
        log: EventLog | None = None,
    ) -> CursorSession:
        """Create a new cursor-agent session."""
        session_id = await self._create_chat()
        if log is not None:
            log.log(
                "session.created",
                {
                    "provider": self.name,
                    "model": model,
                    "session_id": session_id,
                    "system_prompt": system_prompt,
                    "workspace": "/tmp",
                },
            )
        session = CursorSession(
            session_id=session_id,
            model=model,
            workspace=Path("/tmp"),
            log=log,
        )
        if system_prompt:
            async for _ in session.send(system_prompt):
                pass
        return session

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
    ) -> CursorSession:
        """Restore from a suspended state blob."""
        data = json.loads(state.decode())
        session_id = data["session_id"]
        if log is not None:
            log.log(
                "session.restored",
                {
                    "provider": self.name,
                    "model": data["model"],
                    "session_id": session_id,
                    "workspace": data["workspace"],
                },
            )
        return CursorSession(
            session_id=session_id,
            model=data["model"],
            workspace=Path(data["workspace"]),
            log=log,
        )

    @staticmethod
    async def _create_chat() -> str:
        """Pre-create a chat via cursor-agent create-chat."""
        proc = await asyncio.create_subprocess_exec(
            _cursor_binary(),
            "create-chat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        session_id = stdout.decode().strip()
        if not session_id:
            raise RuntimeError("cursor-agent create-chat returned empty ID")
        return session_id
