# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the real scripted provider.

Spawns a daemon with the actual ScriptedProvider, creates agents backed
by real Python script files, and verifies the full pipeline: turn
execution, tool dispatch via daemon RPC, event log contents, and
suspend/restore via multiplexer eviction.
"""

from __future__ import annotations

import asyncio
import gc
import json
import textwrap
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from substrat.daemon import Daemon
from substrat.logging.event_log import read_log
from substrat.provider.scripted import ScriptedProvider
from substrat.rpc import async_call

# Path to the helper library that scripts import.
_HELPER_DIR = (Path(__file__).parent / "../../src/substrat/provider").resolve()


# -- Helpers ----------------------------------------------------------------


def _write_script(tmp_path: Path, code: str, name: str = "script.py") -> Path:
    """Write a script file with the substrat_script import preamble."""
    preamble = (
        "import sys\n"
        f"sys.path.insert(0, {str(_HELPER_DIR)!r})\n"
        "from substrat_script import read_turn, call_tool, done\n"
    )
    p = tmp_path / name
    p.write_text(preamble + textwrap.dedent(code))
    return p


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
async def daemon_env(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Daemon, Path], None]:
    """Start a daemon with the real ScriptedProvider, yield (daemon, tmp_path)."""
    provider = ScriptedProvider()
    daemon = Daemon(
        tmp_path,
        default_provider="scripted",
        default_model=None,
        max_slots=2,
        providers={"scripted": provider},
    )
    await daemon.start()
    yield daemon, tmp_path
    await daemon.stop()
    gc.collect()
    await asyncio.sleep(0)


# -- Basic turn execution ---------------------------------------------------


async def test_simple_turn(
    daemon_env: tuple[Daemon, Path],
) -> None:
    """Script receives message, returns response via done()."""
    daemon, tmp_path = daemon_env
    sock = str(daemon.socket_path)

    script = _write_script(
        tmp_path,
        """
        msg = read_turn()
        done(f"echo: {msg}")
        """,
    )
    created = await async_call(
        sock,
        "agent.create",
        {"name": "echo", "instructions": "echo things", "model": str(script)},
    )
    resp = await async_call(
        sock,
        "agent.send",
        {"agent_id": created["agent_id"], "message": "hello"},
    )
    assert resp["response"] == "echo: hello"


# -- Multi-turn -------------------------------------------------------------


async def test_multi_turn(
    daemon_env: tuple[Daemon, Path],
) -> None:
    """Script handles two sequential turns."""
    daemon, tmp_path = daemon_env
    sock = str(daemon.socket_path)

    script = _write_script(
        tmp_path,
        """
        m1 = read_turn()
        done(f"first: {m1}")
        m2 = read_turn()
        done(f"second: {m2}")
        """,
    )
    created = await async_call(
        sock,
        "agent.create",
        {"name": "multi", "instructions": "multi", "model": str(script)},
    )
    aid = created["agent_id"]
    r1 = await async_call(sock, "agent.send", {"agent_id": aid, "message": "a"})
    assert r1["response"] == "first: a"
    r2 = await async_call(sock, "agent.send", {"agent_id": aid, "message": "b"})
    assert r2["response"] == "second: b"


# -- Tool dispatch via daemon RPC ------------------------------------------


async def test_tool_call_via_daemon(
    daemon_env: tuple[Daemon, Path],
) -> None:
    """Script calls check_inbox via stdout, bridged through daemon RPC."""
    daemon, tmp_path = daemon_env
    sock = str(daemon.socket_path)

    script = _write_script(
        tmp_path,
        """
        import json
        read_turn()
        result = call_tool("check_inbox")
        done(json.dumps(result))
        """,
    )
    created = await async_call(
        sock,
        "agent.create",
        {"name": "tooler", "instructions": "use tools", "model": str(script)},
    )
    resp = await async_call(
        sock,
        "agent.send",
        {"agent_id": created["agent_id"], "message": "go"},
    )
    # check_inbox returns {"messages": [...]}.
    parsed = json.loads(resp["response"])
    assert "messages" in parsed


# -- Event log contents ----------------------------------------------------


async def test_event_log_records_turns_and_tools(
    daemon_env: tuple[Daemon, Path],
) -> None:
    """Event log contains turn.start, tool.call, and turn.complete events."""
    daemon, tmp_path = daemon_env
    sock = str(daemon.socket_path)

    script = _write_script(
        tmp_path,
        """
        read_turn()
        call_tool("check_inbox")
        done("done")
        """,
    )
    created = await async_call(
        sock,
        "agent.create",
        {"name": "logged", "instructions": "log test", "model": str(script)},
    )
    aid = created["agent_id"]
    await async_call(sock, "agent.send", {"agent_id": aid, "message": "go"})

    # Read event log.
    node = daemon.orchestrator.tree.resolve("logged")
    log_path = tmp_path / "agents" / node.session_id.hex / "events.jsonl"
    events = read_log(log_path)

    event_types = [e["event"] for e in events]
    assert "turn.start" in event_types
    assert "tool.call" in event_types
    assert "turn.complete" in event_types

    # Verify tool.call content.
    tool_events = [e for e in events if e["event"] == "tool.call"]
    assert len(tool_events) == 1
    assert tool_events[0]["data"]["tool"] == "check_inbox"
    assert "result" in tool_events[0]["data"]


# -- Suspend/restore via eviction ------------------------------------------


async def test_eviction_and_restore(
    daemon_env: tuple[Daemon, Path],
) -> None:
    """Scripted session survives multiplexer eviction via history replay."""
    daemon, tmp_path = daemon_env
    sock = str(daemon.socket_path)

    # Two-turn script. After eviction, the second turn must replay the first.
    script = _write_script(
        tmp_path,
        """
        m1 = read_turn()
        done(f"first: {m1}")
        m2 = read_turn()
        done(f"second: {m2}")
        """,
    )
    created = await async_call(
        sock,
        "agent.create",
        {"name": "evictee", "instructions": "survive", "model": str(script)},
    )
    aid = created["agent_id"]

    # Turn 1.
    r1 = await async_call(sock, "agent.send", {"agent_id": aid, "message": "a"})
    assert r1["response"] == "first: a"

    # Force eviction by creating enough agents to fill the scripted pool.
    # The daemon has max_slots=2, scripted pool = max_slots*8 = 16.
    # We need to fill all 16 slots. But that's a lot of agents...
    # Instead, let's directly suspend via the scheduler.
    node = daemon.orchestrator.tree.resolve("evictee")
    sid = node.session_id
    mux = daemon.orchestrator._scheduler._mux
    # Manually evict: suspend + remove from mux.
    if mux.contains(sid):
        # Acquire to get the provider session, then remove triggers stop.
        # But we want suspend, not remove. Access internals.
        pool_name = mux._session_pool.get(sid)
        if pool_name:
            pool = mux._pools[pool_name]
            ps = pool.slots.get(sid)
            if ps is not None:
                state = await ps.suspend()
                session = daemon.orchestrator._scheduler._sessions[sid]
                session.suspend(state)
                daemon.orchestrator._scheduler._store.save(session)
                await ps.stop()
                # Remove from mux without going through normal path.
                del pool.slots[sid]
                if sid in pool.held:
                    pool.held.discard(sid)
                if sid in pool.lru:
                    pool.lru.remove(sid)
                del mux._session_pool[sid]

    # Turn 2 should restore from blob and replay turn 1.
    r2 = await async_call(sock, "agent.send", {"agent_id": aid, "message": "b"})
    assert r2["response"] == "second: b"
