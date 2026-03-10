# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the scripted provider helper library (substrat_script.py).

Uses monkeypatched stdin/stdout to simulate the wire protocol without
a real subprocess.
"""

import io
import json
from typing import Any

import pytest

from substrat.provider.substrat_script import _Runtime

# -- Helpers ----------------------------------------------------------------


def _turn_line(message: str, history: list[dict[str, Any]] | None = None) -> str:
    """Build a JSON turn line for feeding into stdin."""
    msg: dict[str, Any] = {"type": "turn", "message": message}
    if history is not None:
        msg["history"] = history
    return json.dumps(msg) + "\n"


def _result_line(call_id: int, data: dict[str, Any]) -> str:
    return json.dumps({"type": "result", "id": call_id, "data": data}) + "\n"


def _error_result_line(call_id: int, error: str) -> str:
    return json.dumps({"type": "result", "id": call_id, "error": error}) + "\n"


# -- _Runtime replay --------------------------------------------------------


def test_replay_returns_cached_messages() -> None:
    """Replay mode returns messages from history in order."""
    rt = _Runtime()
    history = [
        {"message": "first", "calls": [], "response": "r1"},
        {"message": "second", "calls": [], "response": "r2"},
    ]
    rt.init({"type": "turn", "message": "live msg", "history": history})
    assert rt.replaying
    assert rt.replay_message() == "first"
    rt.replay_done()
    assert rt.replaying
    assert rt.replay_message() == "second"
    rt.replay_done()
    assert not rt.replaying


def test_replay_tool_results() -> None:
    """Replay returns cached tool results and raises on errors."""
    rt = _Runtime()
    history = [
        {
            "message": "go",
            "calls": [
                {"tool": "gate", "args": {"x": 1}, "result": {"ok": True}},
                {"tool": "fail", "args": {}, "error": "boom"},
            ],
            "response": "done",
        }
    ]
    rt.init({"type": "turn", "message": "live", "history": history})
    assert rt.replay_tool_result("gate") == {"ok": True}
    with pytest.raises(RuntimeError, match="boom"):
        rt.replay_tool_result("fail")


def test_replay_divergence_raises() -> None:
    """Replay aborts if script calls a different tool than history."""
    rt = _Runtime()
    history = [
        {
            "message": "go",
            "calls": [{"tool": "gate", "args": {}, "result": {}}],
            "response": "done",
        }
    ]
    rt.init({"type": "turn", "message": "live", "history": history})
    with pytest.raises(AssertionError, match="replay divergence"):
        rt.replay_tool_result("wrong_tool")


def test_pending_live_after_replay() -> None:
    """After replay exhausts history, the buffered live message is available."""
    rt = _Runtime()
    history = [{"message": "old", "calls": [], "response": "r"}]
    rt.init({"type": "turn", "message": "the live one", "history": history})
    # Exhaust replay.
    rt.replay_done()
    assert not rt.replaying
    assert rt._pending_live == "the live one"


def test_no_history_no_replay() -> None:
    """No history means no replay mode."""
    rt = _Runtime()
    rt.init({"type": "turn", "message": "live"})
    assert not rt.replaying
    assert rt._pending_live == ""


# -- Full protocol via monkeypatch ------------------------------------------


def test_read_turn_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_turn reads from stdin in live mode."""
    from substrat.provider import substrat_script as mod

    # Reset singleton.
    monkeypatch.setattr(mod, "_rt", _Runtime())
    stdin = io.StringIO(_turn_line("hello world"))
    monkeypatch.setattr("sys.stdin", stdin)

    result = mod.read_turn()
    assert result == "hello world"


def test_call_tool_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_tool writes to stdout and reads result from stdin."""
    from substrat.provider import substrat_script as mod

    monkeypatch.setattr(mod, "_rt", _Runtime())
    # First a turn message, then a result for call_id=1.
    stdin_data = _turn_line("go") + _result_line(1, {"status": "ok"})
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))

    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)

    msg = mod.read_turn()
    assert msg == "go"
    result = mod.call_tool("gate", agent_name="w1")
    assert result == {"status": "ok"}

    # Verify what was written to stdout.
    written = json.loads(stdout.getvalue().strip())
    assert written == {
        "type": "call",
        "id": 1,
        "tool": "gate",
        "args": {"agent_name": "w1"},
    }


def test_call_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_tool raises RuntimeError on error response."""
    from substrat.provider import substrat_script as mod

    monkeypatch.setattr(mod, "_rt", _Runtime())
    stdin_data = _turn_line("go") + _error_result_line(1, "not found")
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
    monkeypatch.setattr("sys.stdout", io.StringIO())

    mod.read_turn()
    with pytest.raises(RuntimeError, match="not found"):
        mod.call_tool("gate")


def test_done_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """done writes the done message to stdout."""
    from substrat.provider import substrat_script as mod

    monkeypatch.setattr(mod, "_rt", _Runtime())
    monkeypatch.setattr("sys.stdin", io.StringIO(_turn_line("go")))
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)

    mod.read_turn()
    mod.done("all good")

    written = json.loads(stdout.getvalue().strip())
    assert written == {"type": "done", "response": "all good"}


def test_read_turn_replay_then_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_turn replays from history, then returns the live message."""
    from substrat.provider import substrat_script as mod

    monkeypatch.setattr(mod, "_rt", _Runtime())
    history = [{"message": "old turn", "calls": [], "response": "r"}]
    stdin_data = _turn_line("live turn", history=history)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
    monkeypatch.setattr("sys.stdout", io.StringIO())

    # First read_turn: reads stdin, sees history, returns replay message.
    msg1 = mod.read_turn()
    assert msg1 == "old turn"
    mod.done("r")  # Replay done is a no-op.

    # Second read_turn: replay exhausted, returns buffered live message.
    msg2 = mod.read_turn()
    assert msg2 == "live turn"


def test_stdin_eof_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_turn raises SystemExit on EOF."""
    from substrat.provider import substrat_script as mod

    monkeypatch.setattr(mod, "_rt", _Runtime())
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with pytest.raises(SystemExit, match="stdin closed"):
        mod.read_turn()
