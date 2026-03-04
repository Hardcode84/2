# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Full stack E2E: CLI RPC → Daemon → CursorAgentProvider → cursor-agent → MCP tool.

Exercises the one path no other test covers: a real daemon with real
cursor-agent inside a bwrap sandbox making MCP tool calls.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from substrat.daemon import Daemon
from substrat.rpc import async_call
from substrat.workspace.bwrap import check_available

pytestmark = pytest.mark.e2e

if shutil.which("cursor-agent") is None:
    pytest.skip("cursor-agent binary not found", allow_module_level=True)
if check_available() is None:
    pytest.skip("bwrap unavailable", allow_module_level=True)

_MCP_SERVER = Path(__file__).resolve().parent.parent / "fixtures" / "mcp_add_server.py"

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
async def daemon_sock(tmp_path: Path) -> AsyncGenerator[str, None]:
    """Start a real daemon, yield socket path."""
    daemon = Daemon(tmp_path)
    await daemon.start()
    yield str(daemon.socket_path)
    await daemon.stop()


def _inject_mcp_config(ws_root: Path) -> None:
    """Write .cursor/mcp.json with the add-server into the workspace.

    Called after agent creation so _write_mcp_config has already run.
    Overwrites the substrat-only config — we don't need substrat tools here.
    """
    server_dest = ws_root / "mcp_add_server.py"
    shutil.copy2(_MCP_SERVER, server_dest)

    cursor_dir = ws_root / ".cursor"
    cursor_dir.mkdir(exist_ok=True)
    config = {
        "mcpServers": {
            "add-server": {
                "command": "python3",
                "args": [str(server_dest)],
            }
        }
    }
    (cursor_dir / "mcp.json").write_text(json.dumps(config))


# -- Test ----------------------------------------------------------------------


async def test_mcp_tool_call_through_daemon(daemon_sock: str) -> None:
    """daemon → workspace/bwrap → cursor-agent → MCP add tool → result."""
    # Create workspace with network access (cursor-agent needs it).
    ws = await async_call(
        daemon_sock,
        "workspace.create",
        {"name": "mcp-e2e", "network_access": True},
    )
    scope = ws["scope"]

    # Inspect to get root_path.
    info = await async_call(
        daemon_sock,
        "workspace.inspect",
        {"scope": scope, "name": "mcp-e2e"},
    )
    ws_root = Path(info["root_path"])

    # Create agent attached to the workspace.
    created = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "mcp-agent",
            "instructions": "You are a test agent with access to MCP tools.",
            "workspace": "mcp-e2e",
        },
    )
    aid = created["agent_id"]

    # Inject MCP add-server config after agent creation.
    _inject_mcp_config(ws_root)

    # Ask the agent to use the add tool.
    resp = await async_call(
        daemon_sock,
        "agent.send",
        {
            "agent_id": aid,
            "message": "Use the add tool to compute 2 + 3. Report the exact numeric result.",
        },
    )
    assert "5" in resp["response"], f"expected '5' in response: {resp['response']!r}"

    # Cleanup.
    await async_call(daemon_sock, "agent.terminate", {"agent_id": aid})
    await async_call(
        daemon_sock,
        "workspace.delete",
        {"scope": scope, "name": "mcp-e2e"},
    )
