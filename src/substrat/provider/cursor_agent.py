# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Cursor CLI agent provider — spawns cursor-agent as a subprocess."""

import asyncio
import json
import shutil
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from pathlib import Path

from substrat.logging import EventLog, log_method
from substrat.model import LinkSpec, ToolDef

type CommandWrapper = Callable[
    [Sequence[str], Sequence[LinkSpec], Mapping[str, str]],
    Sequence[str],
]


_MDC_TEMPLATE = """\
---
description: Substrat agent instructions
alwaysApply: true
---
{body}
"""


def _write_rules(workspace: Path, system_prompt: str) -> Path | None:
    """Write system prompt as a persistent .mdc rule file.

    Returns the file path on success, None if prompt is empty.
    """
    if not system_prompt:
        return None
    rules_dir = workspace / ".cursor" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    mdc_path = rules_dir / "substrat.mdc"
    mdc_path.write_text(_MDC_TEMPLATE.format(body=system_prompt))
    return mdc_path


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
        wrap_command: CommandWrapper | None = None,
        tools: Sequence[ToolDef] = (),
    ) -> None:
        self._session_id = session_id
        self._model = model
        self._workspace = workspace
        self._log = log
        self._wrap_command = wrap_command
        self._tools = tuple(tools)

    @property
    def session_id(self) -> str:
        return self._session_id

    @log_method(before=True, after=True)
    async def send(self, message: str) -> AsyncGenerator[str, None]:
        """Send a message, yield the final response text."""
        cmd = self._build_cmd(message)
        if self._wrap_command is not None:
            cmd = list(self._wrap_command(cmd, (), {}))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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

    def __init__(
        self,
        wrap_command: CommandWrapper | None = None,
        tools: Sequence[ToolDef] = (),
    ) -> None:
        self._wrap_command = wrap_command
        self._tools = tuple(tools)

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
        workspace = Path("/tmp")
        rules_path = _write_rules(workspace, system_prompt)
        session_id = await self._create_chat()
        log_payload: dict[str, object] = {
            "provider": self.name,
            "model": model,
            "session_id": session_id,
            "system_prompt": system_prompt,
            "workspace": str(workspace),
        }
        if rules_path is not None:
            log_payload["rules_path"] = str(rules_path)
        if log is not None:
            log.log("session.created", log_payload)
        return CursorSession(
            session_id=session_id,
            model=model,
            workspace=workspace,
            log=log,
            wrap_command=self._wrap_command,
            tools=self._tools,
        )

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
            wrap_command=self._wrap_command,
            tools=self._tools,
        )

    async def _create_chat(self) -> str:
        """Pre-create a chat via cursor-agent create-chat."""
        cmd: Sequence[str] = [_cursor_binary(), "create-chat"]
        if self._wrap_command is not None:
            cmd = self._wrap_command(cmd, (), {})
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        session_id = stdout.decode().strip()
        if not session_id:
            raise RuntimeError("cursor-agent create-chat returned empty ID")
        return session_id
