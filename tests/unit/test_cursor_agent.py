# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for CursorAgentProvider wrap_command support."""

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from substrat.model import LinkSpec, ToolDef, ToolParam
from substrat.provider.base import AgentProvider, ProviderSession
from substrat.provider.cursor_agent import (
    _CURSOR_BINDS,
    CursorAgentProvider,
    CursorSession,
    _format_tool_results,
    _parse_tool_calls,
    _tool_prompt,
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
    assert server["command"] == sys.executable
    assert "--agent-id" in server["args"]
    assert aid.hex in server["args"]
    assert (tmp_path / ".cursor" / ".workspace-trusted").exists()


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


# --- --approve-mcps flag ---


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_approve_mcps_always_included(
    mock_exec: AsyncMock, _mock_bin: MagicMock
) -> None:
    """--approve-mcps is always present, even without explicit tools."""
    mock_exec.return_value = _mock_process(_fake_assistant_output("ok"))
    session = CursorSession(
        session_id="sess-1",
        model="m",
        workspace=Path("/tmp"),
    )
    await _aiter_to_list(session.send("hello"))
    actual_cmd = mock_exec.call_args[0]
    assert "--approve-mcps" in actual_cmd


# --- private workspace ---


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_create_without_workspace_uses_private_dir(
    mock_exec: AsyncMock, _mock_bin: MagicMock
) -> None:
    """Provider allocates a unique temp dir when workspace is None."""
    mock_exec.return_value = _mock_process(b"chat-id\n")
    provider = CursorAgentProvider()
    session = await provider.create(model="m", system_prompt="p")
    assert session._private_workspace is True
    assert session._workspace != Path("/tmp")
    assert session._workspace.exists()
    # Rules were written into the private workspace.
    assert (session._workspace / ".cursor" / "rules" / "substrat.mdc").exists()
    await session.stop()
    assert not session._workspace.exists()


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_create_with_workspace_not_private(
    mock_exec: AsyncMock, _mock_bin: MagicMock, tmp_path: Path
) -> None:
    """Explicit workspace is not treated as private (not deleted on stop)."""
    mock_exec.return_value = _mock_process(b"chat-id\n")
    provider = CursorAgentProvider()
    session = await provider.create(model="m", system_prompt="p", workspace=tmp_path)
    assert session._private_workspace is False
    await session.stop()
    assert tmp_path.exists()


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_suspend_restore_preserves_private_flag(
    mock_exec: AsyncMock, _mock_bin: MagicMock
) -> None:
    """private_workspace flag survives suspend/restore."""
    mock_exec.return_value = _mock_process(b"chat-id\n")
    provider = CursorAgentProvider()
    session = await provider.create(model="m", system_prompt="p")
    ws_path = session._workspace
    state = await session.suspend()
    restored = await provider.restore(state)
    assert restored._private_workspace is True
    assert restored._workspace == ws_path
    await restored.stop()
    assert not ws_path.exists()


# --- tool prompt helpers ---


_SAMPLE_TOOLS = (
    ToolDef(
        name="check_inbox",
        description="Check the agent inbox for messages.",
    ),
    ToolDef(
        name="send_message",
        description="Send a message to another agent.",
        parameters=(
            ToolParam(name="to", type="string", description="Target agent ID."),
            ToolParam(
                name="body", type="string", description="Message body.", required=False
            ),
        ),
    ),
)


def test_tool_prompt_formats_tools() -> None:
    """_tool_prompt produces markdown with tool names and <tool_call> instructions."""
    result = _tool_prompt(_SAMPLE_TOOLS)
    assert "## check_inbox" in result
    assert "## send_message" in result
    assert "<tool_call>" in result
    assert "required" in result
    assert "optional" in result


def test_tool_prompt_empty() -> None:
    """Empty tool list returns empty string."""
    assert _tool_prompt(()) == ""


# --- parse tool calls ---


def test_parse_single_tool_call() -> None:
    """Single <tool_call> tag parsed correctly."""
    text = (
        "Some text\n<tool_call>\n"
        '{"name": "check_inbox", "arguments": {}}\n'
        "</tool_call>\nMore text"
    )
    calls = _parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "check_inbox"
    assert calls[0]["arguments"] == {}


def test_parse_multiple_tool_calls() -> None:
    """Multiple <tool_call> tags all parsed."""
    text = (
        '<tool_call>\n{"name": "a", "arguments": {"x": 1}}\n</tool_call>\n'
        "middle text\n"
        '<tool_call>\n{"name": "b", "arguments": {}}\n</tool_call>'
    )
    calls = _parse_tool_calls(text)
    assert len(calls) == 2
    assert calls[0]["name"] == "a"
    assert calls[1]["name"] == "b"


def test_parse_no_tool_calls() -> None:
    """Text without tool call tags returns empty list."""
    assert _parse_tool_calls("just regular text") == []


def test_parse_malformed_json_skipped() -> None:
    """Malformed JSON inside tags is silently skipped."""
    text = "<tool_call>\n{not json}\n</tool_call>"
    assert _parse_tool_calls(text) == []


# --- format tool results ---


def test_format_tool_results_single() -> None:
    """Single result formatted with name attribute."""
    out = _format_tool_results([("check_inbox", {"messages": []})])
    assert '<tool_result name="check_inbox">' in out
    assert '"messages": []' in out
    assert "</tool_result>" in out


def test_format_tool_results_multiple() -> None:
    """Multiple results separated by blank lines."""
    out = _format_tool_results(
        [
            ("a", {"ok": True}),
            ("b", {"ok": False}),
        ]
    )
    assert out.count("</tool_result>") == 2
    assert '\n\n<tool_result name="b">' in out


# --- send() with use_mcp=False ---


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
@patch("substrat.rpc.async_call")
async def test_send_no_mcp_dispatches_tool_calls(
    mock_rpc: AsyncMock, mock_exec: AsyncMock, _mock_bin: MagicMock
) -> None:
    """With use_mcp=False, tool calls are parsed and dispatched via RPC."""
    aid = uuid4()
    # Round 1: response with a tool call.
    round1_text = (
        "Let me check.\n<tool_call>\n"
        '{"name": "check_inbox", "arguments": {}}\n'
        "</tool_call>"
    )
    # Round 2: final response without tool calls.
    round2_text = "No new messages."
    mock_exec.side_effect = [
        _mock_process(_fake_assistant_output(round1_text)),
        _mock_process(_fake_assistant_output(round2_text)),
    ]
    mock_rpc.return_value = {"messages": []}

    session = CursorSession(
        session_id="sess-1",
        model="m",
        workspace=Path("/tmp"),
        use_mcp=False,
        agent_id=aid,
        daemon_socket="/tmp/test.sock",
    )
    chunks = await _aiter_to_list(session.send("hello"))

    # Both rounds yielded text.
    assert round1_text in chunks
    assert round2_text in chunks
    # RPC was called with correct params.
    mock_rpc.assert_called_once_with(
        "/tmp/test.sock",
        "tool.call",
        {"agent_id": aid.hex, "tool": "check_inbox", "arguments": {}},
    )
    # Two subprocess spawns (one per round).
    assert mock_exec.call_count == 2


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_send_no_mcp_no_tool_calls(
    mock_exec: AsyncMock, _mock_bin: MagicMock
) -> None:
    """With use_mcp=False but no tool calls in output, behaves like single round."""
    mock_exec.return_value = _mock_process(_fake_assistant_output("just text"))
    session = CursorSession(
        session_id="sess-1",
        model="m",
        workspace=Path("/tmp"),
        use_mcp=False,
        agent_id=uuid4(),
        daemon_socket="/tmp/test.sock",
    )
    chunks = await _aiter_to_list(session.send("hello"))
    assert chunks == ["just text"]
    assert mock_exec.call_count == 1


# --- suspend/restore with agent_id ---


@pytest.mark.asyncio
async def test_suspend_restore_preserves_agent_id() -> None:
    """agent_id and use_mcp survive suspend/restore."""
    aid = uuid4()
    session = CursorSession(
        session_id="sess-1",
        model="m",
        workspace=Path("/tmp"),
        use_mcp=False,
        agent_id=aid,
        daemon_socket="/tmp/d.sock",
    )
    state = await session.suspend()
    data = json.loads(state.decode())
    assert data["agent_id"] == aid.hex
    assert data["use_mcp"] is False
    assert data["daemon_socket"] == "/tmp/d.sock"


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_restore_rebuilds_no_mcp_session(
    mock_exec: AsyncMock, _mock_bin: MagicMock
) -> None:
    """restore() reconstructs use_mcp=False session from state blob."""
    aid = uuid4()
    state = json.dumps(
        {
            "session_id": "s1",
            "model": "m",
            "workspace": "/tmp",
            "system_prompt": "p",
            "use_mcp": False,
            "agent_id": aid.hex,
            "daemon_socket": "/tmp/d.sock",
        }
    ).encode()
    provider = CursorAgentProvider(use_mcp=False)
    restored = await provider.restore(state)
    assert restored._use_mcp is False
    assert restored._agent_id == aid
    assert restored._daemon_socket == "/tmp/d.sock"


# --- create() with use_mcp=False ---


@pytest.mark.asyncio
@patch("substrat.provider.cursor_agent._cursor_binary", return_value=FAKE_BINARY)
@patch("asyncio.create_subprocess_exec")
async def test_create_no_mcp_skips_mcp_config(
    mock_exec: AsyncMock, _mock_bin: MagicMock, tmp_path: Path
) -> None:
    """create() with use_mcp=False does not write mcp.json, injects tool prompt."""
    mock_exec.return_value = _mock_process(b"chat-id\n")
    tools = (ToolDef(name="ping", description="Ping."),)
    provider = CursorAgentProvider(tools=tools, use_mcp=False)
    session = await provider.create(
        model="m",
        system_prompt="be nice",
        workspace=tmp_path,
        agent_id=uuid4(),
    )
    # No mcp.json written.
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    # Rules file contains tool prompt.
    mdc = (tmp_path / ".cursor" / "rules" / "substrat.mdc").read_text()
    assert "## ping" in mdc
    assert "<tool_call>" in mdc
    # Session has use_mcp=False.
    assert session._use_mcp is False
