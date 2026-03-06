# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Cursor CLI agent provider — spawns cursor-agent as a subprocess."""

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from uuid import UUID

from substrat.logging import EventLog, log_method
from substrat.model import CommandWrapper, LinkSpec, ToolDef

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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# Host directories cursor-agent needs access to inside the sandbox.
_CURSOR_BINDS: tuple[LinkSpec, ...] = (
    LinkSpec(Path.home() / ".cursor", Path.home() / ".cursor", "rw"),
    LinkSpec(Path.home() / ".local", Path.home() / ".local", "ro"),
    LinkSpec(
        Path.home() / ".config" / "cursor", Path.home() / ".config" / "cursor", "rw"
    ),
)


def _write_mcp_config(workspace: Path, agent_id: UUID) -> Path:
    """Write .cursor/mcp.json so cursor-agent can reach the MCP tool server."""
    cursor_dir = workspace / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    config_path = cursor_dir / "mcp.json"
    server_cfg: dict[str, object] = {
        "command": sys.executable,
        "args": [
            "-W",
            "ignore::RuntimeWarning:runpy",
            "-m",
            "substrat.provider.mcp_server",
            "--agent-id",
            agent_id.hex,
        ],
    }
    # cursor-agent does not propagate env to MCP server subprocesses.
    # Read socket path written by the daemon outside the sandbox root.
    sock_file = workspace.parent / ".substrat_socket"
    if sock_file.exists():
        server_cfg["env"] = {"SUBSTRAT_SOCKET": sock_file.read_text().strip()}
    config = {"mcpServers": {"substrat": server_cfg}}
    config_path.write_text(json.dumps(config, indent=2))
    # cursor-agent requires workspace trust before allowing MCP tool
    # calls. The --trust CLI flag is unreliable in headless mode — the
    # on-disk marker is the mechanism cursor-agent actually checks.
    (cursor_dir / ".workspace-trusted").touch()
    # Pre-approve the MCP server on disk. --approve-mcps is flaky in
    # some cursor-agent versions; explicit enable writes the approval
    # hash to ~/.cursor/projects/<slug>/mcp-approvals.json.
    _pre_approve_mcp(workspace, "substrat")
    return config_path


def _pre_approve_mcp(workspace: Path, server_name: str) -> None:
    """Run `cursor-agent mcp enable <name>` to pre-approve the server."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [_cursor_binary(), "mcp", "enable", server_name],
            cwd=str(workspace),
            capture_output=True,
            timeout=10,
        )


_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

_MAX_TOOL_ROUNDS = 10


def _tool_prompt(tools: Sequence[ToolDef]) -> str:
    """Format tool definitions into a prompt block with usage instructions."""
    if not tools:
        return ""
    lines = [
        "# Available tools",
        "",
        "When you need to use a tool, emit EXACTLY this format (no markdown fences):",
        "",
        "<tool_call>",
        '{"name": "<tool_name>", "arguments": {<args>}}',
        "</tool_call>",
        "",
        "Wait for the result before continuing. Available tools:",
        "",
    ]
    for tool in tools:
        lines.append(f"## {tool.name}")
        lines.append(tool.description)
        if tool.parameters:
            lines.append("Parameters:")
            for p in tool.parameters:
                req = "required" if p.required else "optional"
                lines.append(f"  - {p.name} ({p.type}, {req}): {p.description}")
        lines.append("")
    return "\n".join(lines)


def _parse_tool_calls(text: str) -> list[dict[str, object]]:
    """Extract tool call dicts from <tool_call> tags in text."""
    results: list[dict[str, object]] = []
    for m in _TOOL_CALL_RE.finditer(text):
        with contextlib.suppress(json.JSONDecodeError):
            results.append(json.loads(m.group(1)))
    return results


def _format_tool_results(
    results: list[tuple[str, dict[str, object]]],
) -> str:
    """Format (name, result_dict) pairs into <tool_result> text."""
    parts: list[str] = []
    for name, result in results:
        body = json.dumps(result)
        parts.append(f'<tool_result name="{name}">\n{body}\n</tool_result>')
    return "\n\n".join(parts)


class CursorSession:
    """A live conversation with cursor-agent.

    Each send() spawns a new subprocess with --resume to continue
    the local session.
    """

    def __init__(
        self,
        session_id: str,
        model: str | None,
        workspace: Path,
        system_prompt: str = "",
        log: EventLog | None = None,
        wrap_command: CommandWrapper | None = None,
        tools: Sequence[ToolDef] = (),
        *,
        private_workspace: bool = False,
        use_mcp: bool = True,
        agent_id: UUID | None = None,
        daemon_socket: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._model = model
        self._workspace = workspace
        self._system_prompt = system_prompt
        self._log = log
        self._wrap_command = wrap_command
        self._tools = tuple(tools)
        self._private_workspace = private_workspace
        self._use_mcp = use_mcp
        self._agent_id = agent_id
        self._daemon_socket = daemon_socket

    @property
    def session_id(self) -> str:
        return self._session_id

    @log_method(before=True, after=True)
    async def send(self, message: str) -> AsyncGenerator[str, None]:
        """Send a message, yield the final response text.

        When use_mcp is False, runs a multi-turn loop: spawn cursor-agent,
        parse <tool_call> tags, dispatch via daemon RPC, feed results back.
        """
        if self._use_mcp:
            async for chunk in self._run_once(message):
                yield chunk
            return
        # Prompt-based tool dispatch loop.
        from substrat.rpc import async_call

        prompt = message
        for _ in range(_MAX_TOOL_ROUNDS):
            full_text: list[str] = []
            async for chunk in self._run_once(prompt):
                full_text.append(chunk)
                yield chunk
            calls = _parse_tool_calls("".join(full_text))
            if not calls:
                return
            # Dispatch each tool call through the daemon.
            assert self._daemon_socket is not None, (
                "daemon_socket required for prompt-based tool dispatch"
            )
            assert self._agent_id is not None, (
                "agent_id required for prompt-based tool dispatch"
            )
            results: list[tuple[str, dict[str, object]]] = []
            for call in calls:
                name = str(call.get("name", ""))
                arguments = call.get("arguments", {})
                resp = await async_call(
                    self._daemon_socket,
                    "tool.call",
                    {
                        "agent_id": self._agent_id.hex,
                        "tool": name,
                        "arguments": arguments,
                    },
                )
                results.append((name, resp))
            prompt = _format_tool_results(results)

    async def _run_once(self, message: str) -> AsyncGenerator[str, None]:
        """Spawn cursor-agent once, yield text chunks."""
        cmd = self._build_cmd(message)
        if self._wrap_command is not None:
            cmd = list(self._wrap_command(cmd, _CURSOR_BINDS, {}))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        try:
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
            if proc.returncode != 0:
                assert proc.stderr is not None
                stderr = (await proc.stderr.read()).decode().strip()
                raise RuntimeError(f"cursor-agent exited {proc.returncode}: {stderr}")
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    @log_method(after=True)
    async def suspend(self) -> bytes:
        """Serialize state — session ID, model, workspace, system prompt."""
        state: dict[str, object] = {
            "session_id": self._session_id,
            "model": self._model,
            "workspace": str(self._workspace),
            "system_prompt": self._system_prompt,
        }
        if self._private_workspace:
            state["private_workspace"] = True
        if not self._use_mcp:
            state["use_mcp"] = False
        if self._agent_id is not None:
            state["agent_id"] = self._agent_id.hex
        if self._daemon_socket is not None:
            state["daemon_socket"] = self._daemon_socket
        return json.dumps(state).encode()

    @log_method(after=True)
    async def stop(self) -> None:
        """Close event log and remove private workspace if we created one."""
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._private_workspace and self._workspace.exists():
            shutil.rmtree(self._workspace, ignore_errors=True)

    def _build_cmd(self, prompt: str) -> list[str]:
        cmd = [
            _cursor_binary(),
            "--print",
            "--output-format",
            "stream-json",
            "--trust",
            "--yolo",
            "--approve-mcps",
        ]
        if self._model is not None:
            cmd.extend(["--model", self._model])
        cmd.extend(
            [
                "--workspace",
                str(self._workspace),
                "--resume",
                self._session_id,
                prompt,
            ]
        )
        return cmd


class CursorAgentProvider:
    """Factory for cursor-agent sessions.

    The caller owns the EventLog — this provider just writes events to it.
    """

    def __init__(
        self,
        tools: Sequence[ToolDef] = (),
        *,
        use_mcp: bool = False,
    ) -> None:
        self._tools = tuple(tools)
        self._use_mcp = use_mcp

    @property
    def name(self) -> str:
        return "cursor-agent"

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
    ) -> CursorSession:
        """Create a new cursor-agent session."""
        private = workspace is None
        ws = (
            workspace
            if workspace is not None
            else Path(tempfile.mkdtemp(prefix="substrat-"))
        )
        # When MCP is disabled, inject tool descriptions into the system prompt
        # instead of writing mcp.json.
        effective_prompt = system_prompt
        if self._use_mcp:
            if agent_id is not None and workspace is not None:
                _write_mcp_config(ws, agent_id)
        else:
            tool_block = _tool_prompt(self._tools)
            if tool_block:
                effective_prompt = (
                    f"{system_prompt}\n\n{tool_block}" if system_prompt else tool_block
                )
        rules_path = _write_rules(ws, effective_prompt)
        session_id = await self._create_chat(wrap_command)
        log_payload: dict[str, object] = {
            "provider": self.name,
            "model": model,
            "session_id": session_id,
            "system_prompt": system_prompt,
            "workspace": str(ws),
        }
        if rules_path is not None:
            log_payload["rules_path"] = str(rules_path)
        if log is not None:
            log.log("session.created", log_payload)
        return CursorSession(
            session_id=session_id,
            model=model,
            workspace=ws,
            system_prompt=effective_prompt,
            log=log,
            wrap_command=wrap_command,
            tools=self._tools,
            private_workspace=private,
            use_mcp=self._use_mcp,
            agent_id=agent_id,
            daemon_socket=daemon_socket,
        )

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
        *,
        wrap_command: CommandWrapper | None = None,
    ) -> CursorSession:
        """Restore from a suspended state blob."""
        data = json.loads(state.decode())
        session_id = data["session_id"]
        system_prompt = data.get("system_prompt", "")
        workspace = Path(data["workspace"])
        private = data.get("private_workspace", False)
        use_mcp = data.get("use_mcp", True)
        agent_id_hex = data.get("agent_id")
        agent_id = UUID(agent_id_hex) if agent_id_hex else None
        daemon_socket = data.get("daemon_socket")
        # Recreate workspace if it was cleaned up (e.g. private ws after stop()).
        workspace.mkdir(parents=True, exist_ok=True)
        # Re-write rules file in case workspace was cleaned up.
        _write_rules(workspace, system_prompt)
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
            workspace=workspace,
            system_prompt=system_prompt,
            log=log,
            wrap_command=wrap_command,
            tools=self._tools,
            private_workspace=private,
            use_mcp=use_mcp,
            agent_id=agent_id,
            daemon_socket=daemon_socket,
        )

    def models(self) -> list[str]:
        """Return model identifiers supported by cursor-agent."""
        proc = subprocess.run(
            [_cursor_binary(), "--list-models"],
            capture_output=True,
        )
        result: list[str] = []
        for raw_line in proc.stdout.decode().splitlines():
            line = _ANSI_RE.sub("", raw_line).strip()
            if " - " in line:
                result.append(line.split(" - ", 1)[0].strip())
        return result

    async def _create_chat(self, wrap_command: CommandWrapper | None = None) -> str:
        """Pre-create a chat via cursor-agent create-chat."""
        cmd: Sequence[str] = [_cursor_binary(), "create-chat"]
        if wrap_command is not None:
            cmd = wrap_command(cmd, _CURSOR_BINDS, {})
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        session_id = stdout.decode().strip()
        if not session_id:
            err = stderr.decode().strip()
            raise RuntimeError(f"cursor-agent create-chat failed: {err}")
        return session_id
