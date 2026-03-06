# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Full stack E2E: Daemon → bwrap → cursor-agent → substrat MCP server → tool.call.

Unlike test_daemon_mcp_e2e which overwrites .cursor/mcp.json with a custom
add-server, this test relies on the substrat-generated MCP config. Proves
the full MCP round-trip: cursor-agent discovers the substrat MCP server,
spawns it inside bwrap, the server connects back to the daemon via
SUBSTRAT_SOCKET, and the tool.call RPC completes.
"""

from __future__ import annotations

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

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
async def daemon_sock(tmp_path: Path) -> AsyncGenerator[str, None]:
    """Start a real daemon, yield socket path."""
    daemon = Daemon(tmp_path, default_provider="cursor-agent", max_slots=4)
    await daemon.start()
    yield str(daemon.socket_path)
    await daemon.stop()


# -- Test ----------------------------------------------------------------------


async def test_substrat_mcp_round_trip(daemon_sock: str) -> None:
    """daemon → workspace/bwrap → cursor-agent → substrat MCP → tool.call."""
    # Create workspace with network access (cursor-agent needs it).
    ws = await async_call(
        daemon_sock,
        "workspace.create",
        {"name": "substrat-mcp-e2e", "network_access": True},
    )
    scope = ws["scope"]

    # Create agent attached to the workspace.
    # _write_mcp_config writes .cursor/mcp.json pointing to substrat MCP server.
    created = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "substrat-mcp-agent",
            "instructions": "You are a test agent with access to substrat MCP tools.",
            "workspace": "substrat-mcp-e2e",
        },
    )
    aid = created["agent_id"]

    # Ask the agent to call check_inbox — no args, returns {"messages": []}.
    resp = await async_call(
        daemon_sock,
        "agent.send",
        {
            "agent_id": aid,
            "message": "Use the check_inbox tool to check your inbox. "
            "Report the exact result you get back.",
        },
    )
    assert "messages" in resp["response"], (
        f"expected 'messages' in response: {resp['response']!r}"
    )

    # Cleanup.
    await async_call(daemon_sock, "agent.terminate", {"agent_id": aid})
    await async_call(
        daemon_sock,
        "workspace.delete",
        {"scope": scope, "name": "substrat-mcp-e2e"},
    )
