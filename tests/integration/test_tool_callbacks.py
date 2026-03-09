# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests — tool callbacks during active turns via real UDS.

Exercises the concurrent-connection path: an agent's send() is in progress
while the turn function makes tool.call RPCs back to the daemon over UDS.
"""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from substrat.daemon import Daemon
from substrat.logging.event_log import read_log
from substrat.rpc import async_call
from tests.helpers import ScriptedProvider, poll_until

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
async def scripted_env(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Daemon, ScriptedProvider], None]:
    """Start a daemon with ScriptedProvider, yield (daemon, provider)."""
    sock_path = str(tmp_path / "daemon.sock")
    provider = ScriptedProvider(sock_path)
    daemon = Daemon(
        tmp_path,
        default_provider="scripted",
        default_model="test-model",
        max_slots=4,
        providers={"scripted": provider},
    )
    await daemon.start()
    yield daemon, provider
    await daemon.stop()
    # Force collection of orphaned coroutines before the event loop closes,
    # so their finalizers run on a live loop instead of poisoning the next test.
    gc.collect()
    await asyncio.sleep(0)


# -- Tool callback during turn -------------------------------------------------


async def test_tool_callback_during_turn(
    scripted_env: tuple[Daemon, ScriptedProvider],
) -> None:
    """Agent calls check_inbox via UDS mid-turn — two concurrent connections."""
    daemon, provider = scripted_env
    sock = str(daemon.socket_path)

    async def turn_with_callback(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        result = await async_call(
            socket_path,
            "tool.call",
            {"agent_id": agent_id, "tool": "check_inbox", "arguments": {}},
        )
        assert "messages" in result
        yield "checked inbox"

    provider.add_agent_script([turn_with_callback])

    created = await async_call(
        sock,
        "agent.create",
        {
            "name": "checker",
            "instructions": "check things",
        },
    )
    resp = await async_call(
        sock,
        "agent.send",
        {
            "agent_id": created["agent_id"],
            "message": "go",
        },
    )
    assert resp["response"] == "checked inbox"
    assert provider.completed_turns == 1


# -- Spawn creates child session -----------------------------------------------


async def test_spawn_creates_child_session(
    scripted_env: tuple[Daemon, ScriptedProvider],
) -> None:
    """spawn_agent via UDS mid-turn; child gets a real session after drain."""
    daemon, provider = scripted_env
    sock = str(daemon.socket_path)

    async def parent_spawns(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        result = await async_call(
            socket_path,
            "tool.call",
            {
                "agent_id": agent_id,
                "tool": "spawn_agent",
                "arguments": {"name": "worker", "instructions": "work hard"},
            },
        )
        assert result["status"] == "accepted"
        yield "spawned"

    async def child_noop(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        yield "ready"

    provider.add_agent_script([parent_spawns])
    provider.add_agent_script([child_noop])

    created = await async_call(
        sock,
        "agent.create",
        {
            "name": "boss",
            "instructions": "spawn workers",
        },
    )
    resp = await async_call(
        sock,
        "agent.send",
        {
            "agent_id": created["agent_id"],
            "message": "spawn one",
        },
    )
    assert resp["response"] == "spawned"

    # Child should exist after deferred drain.
    listing = await async_call(sock, "agent.list", {})
    names = [a["name"] for a in listing["agents"]]
    assert "boss" in names
    assert "worker" in names


# -- Multi-agent coordination --------------------------------------------------


async def test_multi_agent_coordination(
    scripted_env: tuple[Daemon, ScriptedProvider],
) -> None:
    """Full spawn -> message -> complete -> wake cycle across two agents."""
    daemon, provider = scripted_env
    sock = str(daemon.socket_path)

    # Parent turn 1: spawn worker + send message.
    async def parent_turn_1(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        spawn = await async_call(
            socket_path,
            "tool.call",
            {
                "agent_id": agent_id,
                "tool": "spawn_agent",
                "arguments": {"name": "worker", "instructions": "do it"},
            },
        )
        assert spawn["status"] == "accepted"
        await async_call(
            socket_path,
            "tool.call",
            {
                "agent_id": agent_id,
                "tool": "send_message",
                "arguments": {"recipient": "worker", "text": "go"},
            },
        )
        yield "dispatched"

    # Parent turn 2: woken by child's RESPONSE.
    async def parent_turn_2(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        assert "done" in message
        yield "got result"

    # Child turn 1: woken with parent's message, calls complete.
    async def child_turn_1(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        assert "go" in message
        await async_call(
            socket_path,
            "tool.call",
            {
                "agent_id": agent_id,
                "tool": "complete",
                "arguments": {"result": "done"},
            },
        )
        yield "completing"

    provider.add_agent_script([parent_turn_1, parent_turn_2])
    provider.add_agent_script([child_turn_1])

    created = await async_call(
        sock,
        "agent.create",
        {
            "name": "coordinator",
            "instructions": "coordinate",
        },
    )
    resp = await async_call(
        sock,
        "agent.send",
        {
            "agent_id": created["agent_id"],
            "message": "start",
        },
    )
    assert resp["response"] == "dispatched"

    # Wait for all three turns (parent*2 + child*1).
    await poll_until(lambda: provider.completed_turns >= 3)

    # Child terminated after complete().
    listing = await async_call(sock, "agent.list", {})
    names = [a["name"] for a in listing["agents"]]
    assert "worker" not in names
    assert "coordinator" in names


# -- Inspect child after spawn -------------------------------------------------


async def test_inspect_child_after_spawn(
    scripted_env: tuple[Daemon, ScriptedProvider],
) -> None:
    """Parent spawns child in turn 1, inspects it in turn 2 via tool.call."""
    daemon, provider = scripted_env
    sock = str(daemon.socket_path)

    async def parent_spawns(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        result = await async_call(
            socket_path,
            "tool.call",
            {
                "agent_id": agent_id,
                "tool": "spawn_agent",
                "arguments": {"name": "minion", "instructions": "obey"},
            },
        )
        assert result["status"] == "accepted"
        yield "spawned"

    async def parent_inspects(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        result = await async_call(
            socket_path,
            "tool.call",
            {
                "agent_id": agent_id,
                "tool": "inspect_agent",
                "arguments": {"name": "minion"},
            },
        )
        assert result["state"] == "idle"
        yield "inspected"

    async def child_noop(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        yield "ready"

    provider.add_agent_script([parent_spawns, parent_inspects])
    provider.add_agent_script([child_noop])

    created = await async_call(
        sock,
        "agent.create",
        {
            "name": "overseer",
            "instructions": "watch them",
        },
    )
    resp = await async_call(
        sock,
        "agent.send",
        {
            "agent_id": created["agent_id"],
            "message": "spawn",
        },
    )
    assert resp["response"] == "spawned"

    # Turn 2: manual send triggers inspection.
    resp2 = await async_call(
        sock,
        "agent.send",
        {
            "agent_id": created["agent_id"],
            "message": "check on minion",
        },
    )
    assert resp2["response"] == "inspected"


# -- tool.call events logged to caller's session log -------------------------


async def test_tool_call_events_logged_to_caller(
    scripted_env: tuple[Daemon, ScriptedProvider],
    tmp_path: Path,
) -> None:
    """tool.call RPC logs a tool.call event to the caller's own session log."""
    daemon, provider = scripted_env

    sock = str(daemon.socket_path)

    async def turn_with_tools(
        agent_id: str,
        socket_path: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        # Two tool calls in one turn.
        await async_call(
            socket_path,
            "tool.call",
            {"agent_id": agent_id, "tool": "check_inbox", "arguments": {}},
        )
        await async_call(
            socket_path,
            "tool.call",
            {
                "agent_id": agent_id,
                "tool": "send_message",
                "arguments": {"recipient": "nobody", "text": "hi"},
            },
        )
        yield "done"

    provider.add_agent_script([turn_with_tools])

    created = await async_call(
        sock,
        "agent.create",
        {"name": "logger-test", "instructions": "test logging"},
    )
    agent_id = created["agent_id"]
    resp = await async_call(
        sock,
        "agent.send",
        {"agent_id": agent_id, "message": "go"},
    )
    assert resp["response"] == "done"

    # Read the event log for the agent's session.
    node = daemon.orchestrator.tree.resolve("logger-test")
    log_path = tmp_path / "agents" / node.session_id.hex / "events.jsonl"
    events = read_log(log_path)

    tool_calls = [e for e in events if e["event"] == "tool.call"]
    assert len(tool_calls) == 2

    # First: check_inbox (success).
    assert tool_calls[0]["data"]["tool"] == "check_inbox"
    assert tool_calls[0]["data"]["args"] == {}
    assert "result" in tool_calls[0]["data"]

    # Second: send_message to nonexistent agent (error).
    assert tool_calls[1]["data"]["tool"] == "send_message"
    assert "error" in tool_calls[1]["data"]
