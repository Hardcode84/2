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

from substrat.provider.scripted import ScriptedProvider, ScriptedSession

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
