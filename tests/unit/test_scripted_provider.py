# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ScriptedSession and ScriptedProvider.

Uses real subprocesses with tiny Python scripts to test the full
stdin/stdout protocol bridge.
"""

import json
import textwrap
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from substrat.provider.scripted import (
    ScriptedProvider,
    ScriptedSession,
    reconstruct_history,
)

# -- Helpers ----------------------------------------------------------------


def _write_script(tmp_path: Path, code: str, name: str = "script.py") -> Path:
    """Write a script file and return its path."""
    helper_dir = (Path(__file__).parent / "../../src/substrat/provider").resolve()
    preamble = (
        "import sys\n"
        f"sys.path.insert(0, {str(helper_dir)!r})\n"
        "from substrat_script import read_turn, call_tool, done\n"
    )
    full = preamble + textwrap.dedent(code)
    p = tmp_path / name
    p.write_text(full)
    return p


async def _collect(gen: AsyncGenerator[str, None]) -> str:
    """Collect all chunks from an async generator into a string."""
    parts: list[str] = []
    async for chunk in gen:
        parts.append(chunk)
    return "".join(parts)


# -- ScriptedSession basic send ---------------------------------------------


async def test_simple_echo(tmp_path: Path) -> None:
    """Script that echoes the message back via done()."""
    script = _write_script(
        tmp_path,
        """
        msg = read_turn()
        done(f"echo: {msg}")
    """,
    )
    session = ScriptedSession(script_path=script)
    response = await _collect(session.send("hello"))
    assert response == "echo: hello"
    await session.stop()


async def test_multi_turn(tmp_path: Path) -> None:
    """Script that handles two turns sequentially."""
    script = _write_script(
        tmp_path,
        """
        m1 = read_turn()
        done(f"turn1: {m1}")
        m2 = read_turn()
        done(f"turn2: {m2}")
    """,
    )
    session = ScriptedSession(script_path=script)
    r1 = await _collect(session.send("first"))
    assert r1 == "turn1: first"
    r2 = await _collect(session.send("second"))
    assert r2 == "turn2: second"
    await session.stop()


# -- Tool calls (no daemon, so we test the error path) ---------------------


async def test_tool_call_no_daemon(tmp_path: Path) -> None:
    """Tool call without daemon connection returns error to script."""
    script = _write_script(
        tmp_path,
        """
        read_turn()
        try:
            call_tool("gate", agent_name="w1")
            done("should not reach here")
        except RuntimeError as e:
            done(f"error: {e}")
    """,
    )
    session = ScriptedSession(script_path=script)
    response = await _collect(session.send("go"))
    assert "no daemon connection" in response
    await session.stop()


# -- Suspend/restore -------------------------------------------------------


async def test_suspend_restore(tmp_path: Path) -> None:
    """Suspend serializes history, restore replays it."""
    script = _write_script(
        tmp_path,
        """
        m1 = read_turn()
        done(f"first: {m1}")
        m2 = read_turn()
        done(f"second: {m2}")
    """,
    )
    session = ScriptedSession(script_path=script)
    r1 = await _collect(session.send("hello"))
    assert r1 == "first: hello"

    # Suspend: get state blob.
    blob = await session.suspend()
    await session.stop()

    data = json.loads(blob)
    assert len(data["history"]) == 1
    assert data["history"][0]["message"] == "hello"

    # Restore: new session from blob.
    provider = ScriptedProvider()
    session2 = await provider.restore(blob)
    # Next send replays history then runs live.
    r2 = await _collect(session2.send("world"))
    assert r2 == "second: world"
    await session2.stop()


# -- Script error ----------------------------------------------------------


async def test_script_error(tmp_path: Path) -> None:
    """Script that writes an error message."""
    script = _write_script(
        tmp_path,
        """
        import json, sys
        read_turn()
        sys.stdout.write(json.dumps({"type": "error", "message": "kaboom"}) + "\\n")
        sys.stdout.flush()
    """,
    )
    session = ScriptedSession(script_path=script)
    with pytest.raises(RuntimeError, match="kaboom"):
        await _collect(session.send("go"))
    await session.stop()


# -- Script exit -----------------------------------------------------------


async def test_script_unexpected_exit(tmp_path: Path) -> None:
    """Script that exits mid-turn."""
    script = _write_script(
        tmp_path,
        """
        read_turn()
        import sys; sys.exit(0)
    """,
    )
    session = ScriptedSession(script_path=script)
    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        await _collect(session.send("go"))
    await session.stop()


# -- ScriptedProvider factory -----------------------------------------------


async def test_provider_create(tmp_path: Path) -> None:
    """Provider.create produces a working session."""
    script = _write_script(
        tmp_path,
        """
        msg = read_turn()
        done(f"got: {msg}")
    """,
    )
    provider = ScriptedProvider()
    session = await provider.create(str(script), "ignored system prompt")
    response = await _collect(session.send("test"))
    assert response == "got: test"
    await session.stop()


async def test_provider_create_no_model() -> None:
    """Provider.create without model raises."""
    provider = ScriptedProvider()
    with pytest.raises(ValueError, match="requires model"):
        await provider.create(None, "prompt")


async def test_provider_models() -> None:
    """models() returns empty list."""
    provider = ScriptedProvider()
    assert provider.models() == []
    assert provider.name == "scripted"


# -- Stop ------------------------------------------------------------------


async def test_stop_kills_hanging_process(tmp_path: Path) -> None:
    """stop() kills a process that ignores stdin close."""
    script = _write_script(
        tmp_path,
        """
        import time
        read_turn()
        done("ok")
        # Ignore EOF, hang forever.
        while True:
            time.sleep(1)
    """,
    )
    session = ScriptedSession(script_path=script)
    await _collect(session.send("go"))
    # stop() should kill after timeout.
    await session.stop()
    assert session._proc is not None
    assert session._proc.returncode is not None


# -- reconstruct_history ---------------------------------------------------


def test_reconstruct_empty() -> None:
    """Empty entries produce empty history."""
    assert reconstruct_history([]) == []


def test_reconstruct_single_turn() -> None:
    """Single complete turn with tool calls."""
    entries = [
        {"event": "turn.start", "data": {"prompt": "go"}},
        {
            "event": "tool.call",
            "data": {"tool": "gate", "args": {"x": 1}, "result": {"ok": True}},
        },
        {
            "event": "tool.call",
            "data": {"tool": "fail", "args": {}, "error": "boom"},
        },
        {"event": "turn.complete", "data": {"response": "done"}},
    ]
    history = reconstruct_history(entries)
    assert len(history) == 1
    turn = history[0]
    assert turn["message"] == "go"
    assert turn["response"] == "done"
    assert len(turn["calls"]) == 2
    assert turn["calls"][0] == {
        "tool": "gate",
        "args": {"x": 1},
        "result": {"ok": True},
    }
    assert turn["calls"][1] == {"tool": "fail", "args": {}, "error": "boom"}


def test_reconstruct_multi_turn() -> None:
    """Multiple complete turns."""
    entries = [
        {"event": "turn.start", "data": {"prompt": "first"}},
        {"event": "turn.complete", "data": {"response": "r1"}},
        {"event": "turn.start", "data": {"prompt": "second"}},
        {
            "event": "tool.call",
            "data": {"tool": "check", "args": {}, "result": {"ok": True}},
        },
        {"event": "turn.complete", "data": {"response": "r2"}},
    ]
    history = reconstruct_history(entries)
    assert len(history) == 2
    assert history[0]["message"] == "first"
    assert history[0]["calls"] == []
    assert history[1]["message"] == "second"
    assert len(history[1]["calls"]) == 1


def test_reconstruct_drops_incomplete_turn() -> None:
    """Incomplete turn (no turn.complete) is dropped."""
    entries = [
        {"event": "turn.start", "data": {"prompt": "complete"}},
        {"event": "turn.complete", "data": {"response": "ok"}},
        {"event": "turn.start", "data": {"prompt": "incomplete"}},
        {"event": "tool.call", "data": {"tool": "x", "args": {}, "result": {}}},
        # No turn.complete -- crash happened here.
    ]
    history = reconstruct_history(entries)
    assert len(history) == 1
    assert history[0]["message"] == "complete"


def test_reconstruct_ignores_non_turn_events() -> None:
    """Non-turn events (agent.created, message.enqueued, etc.) are skipped."""
    entries = [
        {"event": "agent.created", "data": {"agent_id": "abc", "name": "x"}},
        {"event": "session.created", "data": {"provider": "scripted"}},
        {"event": "turn.start", "data": {"prompt": "go"}},
        {"event": "message.enqueued", "data": {"message_id": "123"}},
        {"event": "turn.complete", "data": {"response": "done"}},
    ]
    history = reconstruct_history(entries)
    assert len(history) == 1
    assert history[0]["calls"] == []
