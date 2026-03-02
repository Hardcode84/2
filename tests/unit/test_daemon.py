# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the daemon — handler dispatch and lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from substrat.daemon import ERR_INVALID, ERR_METHOD, ERR_NOT_FOUND, Daemon
from substrat.rpc import RpcError, async_call

# Reuse FakeProvider from test_orchestrator.
from tests.unit.test_orchestrator import FakeProvider

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture()
def daemon(tmp_path: Path, provider: FakeProvider) -> Daemon:
    return Daemon(
        tmp_path,
        default_provider="fake",
        default_model="test-model",
        providers={"fake": provider},
    )


# -- Handler tests (direct method calls) --------------------------------------


async def test_agent_create(daemon: Daemon) -> None:
    """agent.create returns agent_id and name."""
    await daemon.start()
    try:
        result = await daemon._h_agent_create(
            {"name": "alpha", "instructions": "do stuff"}
        )
        assert "agent_id" in result
        assert result["name"] == "alpha"
    finally:
        await daemon.stop()


async def test_agent_list_empty(daemon: Daemon) -> None:
    """agent.list returns empty list when no agents exist."""
    await daemon.start()
    try:
        result = await daemon._h_agent_list({})
        assert result == {"agents": []}
    finally:
        await daemon.stop()


async def test_agent_list_with_agents(daemon: Daemon) -> None:
    """agent.list returns all agents."""
    await daemon.start()
    try:
        await daemon._h_agent_create({"name": "a", "instructions": "i"})
        await daemon._h_agent_create({"name": "b", "instructions": "j"})
        result = await daemon._h_agent_list({})
        names = [a["name"] for a in result["agents"]]
        assert sorted(names) == ["a", "b"]
    finally:
        await daemon.stop()


async def test_agent_send(daemon: Daemon) -> None:
    """agent.send returns provider response."""
    await daemon.start()
    try:
        created = await daemon._h_agent_create({"name": "a", "instructions": "i"})
        result = await daemon._h_agent_send(
            {"agent_id": created["agent_id"], "message": "hello"}
        )
        assert result["response"] == "ok"
    finally:
        await daemon.stop()


async def test_agent_inspect(daemon: Daemon) -> None:
    """agent.inspect returns state, children, inbox."""
    await daemon.start()
    try:
        created = await daemon._h_agent_create({"name": "a", "instructions": "i"})
        result = await daemon._h_agent_inspect({"agent_id": created["agent_id"]})
        assert result["name"] == "a"
        assert result["state"] == "idle"
        assert result["children"] == []
        assert result["inbox"] == []
    finally:
        await daemon.stop()


async def test_agent_terminate(daemon: Daemon) -> None:
    """agent.terminate removes agent from tree."""
    await daemon.start()
    try:
        created = await daemon._h_agent_create({"name": "doomed", "instructions": "i"})
        await daemon._h_agent_terminate({"agent_id": created["agent_id"]})
        result = await daemon._h_agent_list({})
        assert result["agents"] == []
    finally:
        await daemon.stop()


async def test_tool_call_dispatch(daemon: Daemon) -> None:
    """tool.call dispatches to the agent's ToolHandler method."""
    await daemon.start()
    try:
        created = await daemon._h_agent_create({"name": "a", "instructions": "i"})
        result = await daemon._h_tool_call(
            {
                "agent_id": created["agent_id"],
                "tool": "check_inbox",
                "arguments": {},
            }
        )
        assert "messages" in result
    finally:
        await daemon.stop()


# -- Full lifecycle ------------------------------------------------------------


async def test_full_lifecycle(daemon: Daemon) -> None:
    """create → list → send → inspect → terminate → list(empty)."""
    await daemon.start()
    try:
        # Create.
        created = await daemon._h_agent_create(
            {"name": "worker", "instructions": "work hard"}
        )
        aid = created["agent_id"]

        # List.
        agents = (await daemon._h_agent_list({}))["agents"]
        assert len(agents) == 1
        assert agents[0]["name"] == "worker"

        # Send.
        resp = await daemon._h_agent_send({"agent_id": aid, "message": "go"})
        assert resp["response"] == "ok"

        # Inspect.
        info = await daemon._h_agent_inspect({"agent_id": aid})
        assert info["state"] == "idle"

        # Terminate.
        await daemon._h_agent_terminate({"agent_id": aid})

        # List (empty).
        agents = (await daemon._h_agent_list({}))["agents"]
        assert agents == []
    finally:
        await daemon.stop()


# -- Error cases ---------------------------------------------------------------


async def test_unknown_method_over_uds(daemon: Daemon) -> None:
    """Unknown RPC method returns ERR_METHOD."""
    await daemon.start()
    try:
        with pytest.raises(RpcError) as exc_info:
            await async_call(str(daemon.socket_path), "bogus.method", {})
        assert exc_info.value.code == ERR_METHOD
    finally:
        await daemon.stop()


async def test_unknown_agent_send(daemon: Daemon) -> None:
    """Send to nonexistent agent raises ERR_NOT_FOUND."""
    await daemon.start()
    try:
        from uuid import uuid4

        with pytest.raises(RpcError) as exc_info:
            await async_call(
                str(daemon.socket_path),
                "agent.send",
                {"agent_id": uuid4().hex, "message": "hi"},
            )
        assert exc_info.value.code == ERR_NOT_FOUND
    finally:
        await daemon.stop()


async def test_terminate_already_terminated(daemon: Daemon) -> None:
    """Terminate nonexistent agent returns error."""
    await daemon.start()
    try:
        from uuid import uuid4

        with pytest.raises(RpcError) as exc_info:
            await async_call(
                str(daemon.socket_path),
                "agent.terminate",
                {"agent_id": uuid4().hex},
            )
        assert exc_info.value.code == ERR_NOT_FOUND
    finally:
        await daemon.stop()


async def test_tool_call_unknown_tool(daemon: Daemon) -> None:
    """tool.call with unknown tool returns ERR_INVALID."""
    await daemon.start()
    try:
        created = await daemon._h_agent_create({"name": "a", "instructions": "i"})
        with pytest.raises(RpcError) as exc_info:
            await async_call(
                str(daemon.socket_path),
                "tool.call",
                {
                    "agent_id": created["agent_id"],
                    "tool": "nonexistent_tool",
                    "arguments": {},
                },
            )
        assert exc_info.value.code == ERR_INVALID
    finally:
        await daemon.stop()


# -- Stale socket cleanup -----------------------------------------------------


async def test_stale_socket_cleanup(tmp_path: Path, provider: FakeProvider) -> None:
    """Dead PID file and orphaned socket are cleaned up on start."""
    root = tmp_path / "stale"
    root.mkdir()
    sock = root / "daemon.sock"
    pid = root / "daemon.pid"

    sock.write_text("stale")
    pid.write_text("999999999")  # Very unlikely to be a real PID.

    d = Daemon(root, providers={"fake": provider}, default_provider="fake")
    await d.start()
    try:
        assert d.socket_path.exists()
    finally:
        await d.stop()


async def test_already_running_raises(tmp_path: Path, provider: FakeProvider) -> None:
    """Starting a daemon when one is already running raises RuntimeError."""
    d1 = Daemon(tmp_path, providers={"fake": provider}, default_provider="fake")
    await d1.start()
    try:
        d2 = Daemon(tmp_path, providers={"fake": provider}, default_provider="fake")
        with pytest.raises(RuntimeError, match="already running"):
            await d2.start()
    finally:
        await d1.stop()
