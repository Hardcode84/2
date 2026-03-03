# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for CursorAgentProvider wrap_command support."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from substrat.model import LinkSpec
from substrat.provider.base import AgentProvider, ProviderSession
from substrat.provider.cursor_agent import (
    _CURSOR_BINDS,
    CursorAgentProvider,
    CursorSession,
    _write_mcp_config,
    _write_rules,
)

FAKE_BINARY = "/usr/bin/cursor-agent"


def _fake_assistant_output(text: str) -> bytes:
    """Build stream-json output that CursorSession.send() expects."""
    msg = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    return json.dumps(msg).encode() + b"\n"


class _AsyncLineIter:
    """Async iterator over byte lines."""

    def __init__(self, data: bytes) -> None:
        self._lines = [line + b"\n" for line in data.split(b"\n") if line]
        self._idx = 0

    def __aiter__(self) -> "_AsyncLineIter":
        return self

    async def __anext__(self) -> bytes:
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


def _mock_process(stdout_data: bytes = b"", returncode: int = 0) -> AsyncMock:
    """Async subprocess mock with canned stdout."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.stdout = _AsyncLineIter(stdout_data)
    # communicate() returns (stdout, stderr).
    proc.communicate = AsyncMock(return_value=(stdout_data, b""))
    proc.wait = AsyncMock(return_value=returncode)
    return proc


async def _aiter_to_list(agen: object) -> list[str]:
    """Drain an async generator to a list."""
    return [chunk async for chunk in agen]  # type: ignore[union-attr]


# --- send wrapper ---


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_send_applies_wrapper(mock_exec: AsyncMock, _mock_bin: MagicMock) -> None:
    """Wrapper receives raw argv and cursor binds; its output is what gets exec'd."""
    captured: list[tuple[Sequence[str], Sequence[LinkSpec], Mapping[str, str]]] = []

    def spy_wrapper(
        cmd: Sequence[str],
        binds: Sequence[LinkSpec],
        env: Mapping[str, str],
    ) -> Sequence[str]:
        captured.append((cmd, binds, env))
        return ["bwrap", "--", *cmd]

    mock_exec.return_value = _mock_process(_fake_assistant_output("ok"))

    session = CursorSession(
        session_id="sess-1",
        model="test-model",
        workspace=Path("/tmp"),
        wrap_command=spy_wrapper,
    )
    chunks = await _aiter_to_list(session.send("hello"))

    # Wrapper was called exactly once.
    assert len(captured) == 1
    cmd, binds, env = captured[0]
    # Raw command starts with the cursor binary.
    assert cmd[0] == FAKE_BINARY
    # Provider-specific binds are passed through.
    assert list(binds) == list(_CURSOR_BINDS)
    assert dict(env) == {}
    # Subprocess received the wrapped command.
    actual_cmd = mock_exec.call_args[0]
    assert actual_cmd[0] == "bwrap"
    assert actual_cmd[1] == "--"
    assert chunks == ["ok"]


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_send_no_wrapper(mock_exec: AsyncMock, _mock_bin: MagicMock) -> None:
    """Without wrapper, raw cursor-agent command is used directly."""
    mock_exec.return_value = _mock_process(_fake_assistant_output("ok"))

    session = CursorSession(
        session_id="sess-1",
        model="test-model",
        workspace=Path("/tmp"),
    )
    chunks = await _aiter_to_list(session.send("hello"))

    actual_cmd = mock_exec.call_args[0]
    assert actual_cmd[0] == FAKE_BINARY
    assert "bwrap" not in actual_cmd
    assert chunks == ["ok"]


# --- create-chat wrapper ---


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_create_chat_applies_wrapper(
    mock_exec: AsyncMock, _mock_bin: MagicMock, tmp_path: Path
) -> None:
    """Wrapper is applied to the create-chat subprocess."""
    captured: list[tuple[Sequence[str], Sequence[LinkSpec], Mapping[str, str]]] = []

    def spy_wrapper(
        cmd: Sequence[str],
        binds: Sequence[LinkSpec],
        env: Mapping[str, str],
    ) -> Sequence[str]:
        captured.append((cmd, binds, env))
        return ["bwrap", "--", *cmd]

    # Only create-chat — system prompt is written as .mdc, not sent.
    mock_exec.return_value = _mock_process(b"chat-id-123\n")

    provider = CursorAgentProvider()
    session = await provider.create(
        model="test-model",
        system_prompt="be cool",
        workspace=tmp_path,
        wrap_command=spy_wrapper,
    )

    # create-chat call was wrapped.
    assert list(captured[0][0]) == [FAKE_BINARY, "create-chat"]
    # Cursor binds are forwarded.
    assert list(captured[0][1]) == list(_CURSOR_BINDS)
    create_cmd = mock_exec.call_args_list[0][0]
    assert create_cmd[0] == "bwrap"
    assert session.session_id == "chat-id-123"
    # Only one subprocess call (no system prompt send).
    assert mock_exec.call_count == 1


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_create_chat_no_wrapper(
    mock_exec: AsyncMock, _mock_bin: MagicMock, tmp_path: Path
) -> None:
    """Without wrapper, create-chat uses raw command."""
    mock_exec.return_value = _mock_process(b"chat-id-456\n")

    provider = CursorAgentProvider()
    session = await provider.create(
        model="test-model",
        system_prompt="hey",
        workspace=tmp_path,
    )

    create_cmd = mock_exec.call_args_list[0][0]
    assert create_cmd == (FAKE_BINARY, "create-chat")
    assert session.session_id == "chat-id-456"
    # Only one subprocess call (no system prompt send).
    assert mock_exec.call_count == 1


# --- rules generation ---


def test_write_rules_creates_mdc(tmp_path: Path) -> None:
    """_write_rules writes .cursor/rules/substrat.mdc with correct content."""
    result = _write_rules(tmp_path, "You are agent alice.")
    mdc = tmp_path / ".cursor" / "rules" / "substrat.mdc"
    assert result == mdc
    assert mdc.exists()
    content = mdc.read_text()
    assert "alwaysApply: true" in content
    assert "You are agent alice." in content


def test_write_rules_skipped_when_empty(tmp_path: Path) -> None:
    """Empty prompt produces no file."""
    result = _write_rules(tmp_path, "")
    assert result is None
    assert not (tmp_path / ".cursor" / "rules" / "substrat.mdc").exists()


# --- MCP config ---


def test_write_mcp_config(tmp_path: Path) -> None:
    """_write_mcp_config writes .cursor/mcp.json with agent-id."""
    aid = uuid4()
    path = _write_mcp_config(tmp_path, aid)
    assert path == tmp_path / ".cursor" / "mcp.json"
    config = json.loads(path.read_text())
    server = config["mcpServers"]["substrat"]
    assert server["command"] == "python"
    assert "--agent-id" in server["args"]
    assert aid.hex in server["args"]


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_create_writes_mcp_config(
    mock_exec: AsyncMock, _mock_bin: MagicMock, tmp_path: Path
) -> None:
    """create() writes MCP config when both workspace and agent_id are given."""
    mock_exec.return_value = _mock_process(b"chat-id\n")
    aid = uuid4()
    provider = CursorAgentProvider()
    await provider.create(
        model="m",
        system_prompt="p",
        workspace=tmp_path,
        agent_id=aid,
    )
    mcp = tmp_path / ".cursor" / "mcp.json"
    assert mcp.exists()
    config = json.loads(mcp.read_text())
    assert aid.hex in config["mcpServers"]["substrat"]["args"]


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_create_no_mcp_without_agent_id(
    mock_exec: AsyncMock, _mock_bin: MagicMock, tmp_path: Path
) -> None:
    """create() without agent_id does not write MCP config."""
    mock_exec.return_value = _mock_process(b"chat-id\n")
    provider = CursorAgentProvider()
    await provider.create(
        model="m",
        system_prompt="p",
        workspace=tmp_path,
    )
    assert not (tmp_path / ".cursor" / "mcp.json").exists()


# --- protocol compliance ---


def test_provider_satisfies_protocol() -> None:
    """CursorAgentProvider satisfies AgentProvider protocol."""
    provider = CursorAgentProvider()
    assert isinstance(provider, AgentProvider)


def test_session_with_wrapper_satisfies_protocol() -> None:
    """CursorSession with wrap_command still satisfies ProviderSession."""
    session = CursorSession(
        session_id="x",
        model="m",
        workspace=Path("/tmp"),
        wrap_command=lambda cmd, binds, env: cmd,
    )
    assert isinstance(session, ProviderSession)
