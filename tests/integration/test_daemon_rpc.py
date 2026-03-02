# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests — real daemon on real UDS, exercised through async_call."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from substrat.daemon import Daemon
from substrat.rpc import RpcError, async_call

# Reuse FakeProvider from unit tests.
from tests.unit.test_orchestrator import FakeProvider

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
async def daemon_sock(tmp_path: Path) -> asyncio.AsyncGenerator[str, None]:
    """Start a daemon with FakeProvider, yield socket path, stop on teardown."""
    provider = FakeProvider()
    daemon = Daemon(
        tmp_path,
        default_provider="fake",
        default_model="test-model",
        providers={"fake": provider},
    )
    await daemon.start()
    yield str(daemon.socket_path)
    await daemon.stop()


# -- Full lifecycle ------------------------------------------------------------


async def test_full_lifecycle(daemon_sock: str) -> None:
    """create → list → send → inspect → terminate → list(empty)."""
    # Create.
    created = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "worker",
            "instructions": "do the work",
        },
    )
    assert "agent_id" in created
    aid = created["agent_id"]

    # List.
    listing = await async_call(daemon_sock, "agent.list", {})
    names = [a["name"] for a in listing["agents"]]
    assert "worker" in names

    # Send.
    resp = await async_call(
        daemon_sock,
        "agent.send",
        {
            "agent_id": aid,
            "message": "hello",
        },
    )
    assert resp["response"] == "ok"

    # Inspect.
    info = await async_call(daemon_sock, "agent.inspect", {"agent_id": aid})
    assert info["name"] == "worker"
    assert info["state"] == "idle"
    assert info["children"] == []

    # Terminate.
    term = await async_call(daemon_sock, "agent.terminate", {"agent_id": aid})
    assert term["status"] == "terminated"

    # List (empty).
    listing = await async_call(daemon_sock, "agent.list", {})
    assert listing["agents"] == []


# -- tool.call round-trip -----------------------------------------------------


async def test_tool_call_round_trip(daemon_sock: str) -> None:
    """Create agent, call check_inbox via tool.call RPC."""
    created = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "tooluser",
            "instructions": "i",
        },
    )
    result = await async_call(
        daemon_sock,
        "tool.call",
        {
            "agent_id": created["agent_id"],
            "tool": "check_inbox",
            "arguments": {},
        },
    )
    assert "messages" in result


# -- Error propagation --------------------------------------------------------


async def test_send_to_nonexistent_agent(daemon_sock: str) -> None:
    """Send to nonexistent agent returns ERR_NOT_FOUND."""
    with pytest.raises(RpcError) as exc_info:
        await async_call(
            daemon_sock,
            "agent.send",
            {
                "agent_id": uuid4().hex,
                "message": "hi",
            },
        )
    assert exc_info.value.code == 1  # ERR_NOT_FOUND.


async def test_unknown_method(daemon_sock: str) -> None:
    """Unknown RPC method returns ERR_METHOD."""
    with pytest.raises(RpcError) as exc_info:
        await async_call(daemon_sock, "bogus.method", {})
    assert exc_info.value.code == 4  # ERR_METHOD.


# -- Concurrent requests ------------------------------------------------------


async def test_concurrent_requests(daemon_sock: str) -> None:
    """Two agents created and sent to concurrently."""
    c1 = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "a1",
            "instructions": "i",
        },
    )
    c2 = await async_call(
        daemon_sock,
        "agent.create",
        {
            "name": "a2",
            "instructions": "i",
        },
    )

    r1, r2 = await asyncio.gather(
        async_call(
            daemon_sock,
            "agent.send",
            {
                "agent_id": c1["agent_id"],
                "message": "go1",
            },
        ),
        async_call(
            daemon_sock,
            "agent.send",
            {
                "agent_id": c2["agent_id"],
                "message": "go2",
            },
        ),
    )
    assert r1["response"] == "ok"
    assert r2["response"] == "ok"


# -- Recovery ------------------------------------------------------------------


async def test_recovery(tmp_path: Path) -> None:
    """Create agent, stop daemon, start fresh, verify agent is recovered."""
    provider = FakeProvider()
    providers: dict[str, FakeProvider] = {"fake": provider}

    # First daemon: create agent.
    d1 = Daemon(
        tmp_path,
        default_provider="fake",
        default_model="test-model",
        providers=providers,
    )
    await d1.start()
    sock = str(d1.socket_path)
    created = await async_call(
        sock,
        "agent.create",
        {
            "name": "persistent",
            "instructions": "survive me",
        },
    )
    aid = created["agent_id"]
    await d1.stop()

    # Second daemon: same root, fresh instance.
    d2 = Daemon(
        tmp_path,
        default_provider="fake",
        default_model="test-model",
        providers=providers,
    )
    await d2.start()
    sock = str(d2.socket_path)
    try:
        listing = await async_call(sock, "agent.list", {})
        agent_ids = [a["agent_id"] for a in listing["agents"]]
        assert aid in agent_ids
    finally:
        await d2.stop()
