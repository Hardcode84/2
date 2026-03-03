# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""E2E tests — full stack: CLI RPC → Daemon → CursorAgentProvider → cursor-agent.

Every other integration test either hits the real provider directly
(test_cursor_agent.py) or runs the full daemon with FakeProvider
(test_daemon_rpc.py). This file bridges both halves.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from substrat.daemon import Daemon
from substrat.rpc import async_call

pytestmark = pytest.mark.e2e

if shutil.which("cursor-agent") is None:
    pytest.skip("cursor-agent binary not found", allow_module_level=True)

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
async def daemon_sock(tmp_path: Path) -> AsyncGenerator[str, None]:
    """Start a real daemon with default CursorAgentProvider, yield socket path."""
    # Daemon defaults to "claude-sonnet-4-6" but cursor-agent expects "sonnet-4.6".
    daemon = Daemon(tmp_path, default_model="sonnet-4.6")
    await daemon.start()
    yield str(daemon.socket_path)
    await daemon.stop()


# -- Full lifecycle ------------------------------------------------------------


async def test_create_send_terminate(daemon_sock: str) -> None:
    """create → send → inspect → terminate → list(empty)."""
    created = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "e2e",
            "instructions": "You are a test agent.",
        },
    )
    aid = created["agent_id"]

    # Ask for a deterministic reply.
    resp = await async_call(
        daemon_sock,
        "agent.send",
        {
            "agent_id": aid,
            "message": "Say exactly: pong",
        },
    )
    assert "pong" in resp["response"].lower()

    # Session should be idle after the turn completes.
    info = await async_call(daemon_sock, "agent.inspect", {"agent_id": aid})
    assert info["state"] == "idle"

    # Terminate and verify cleanup.
    term = await async_call(daemon_sock, "agent.terminate", {"agent_id": aid})
    assert term["status"] == "terminated"

    listing = await async_call(daemon_sock, "agent.list", {})
    assert listing["agents"] == []


# -- Context persistence -------------------------------------------------------


async def test_session_context_persists(daemon_sock: str) -> None:
    """Two sends to the same agent — second sees context from the first."""
    created = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "memory",
            "instructions": "You are a test agent.",
        },
    )
    aid = created["agent_id"]

    await async_call(
        daemon_sock,
        "agent.send",
        {
            "agent_id": aid,
            "message": "Remember the code word: MANGO",
        },
    )

    resp = await async_call(
        daemon_sock,
        "agent.send",
        {
            "agent_id": aid,
            "message": "What code word did I tell you to remember?",
        },
    )
    assert "mango" in resp["response"].lower()

    # Cleanup.
    await async_call(daemon_sock, "agent.terminate", {"agent_id": aid})
