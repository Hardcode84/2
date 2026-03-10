# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Helper for scripts running under the scripted provider.

Zero dependencies beyond stdlib. Handles the stdin/stdout JSON protocol
and transparent state recovery via inline turn history.

Bind-mounted into the sandbox at /script/substrat_script.py. Scripts
import it and use read_turn/call_tool/done -- nothing else needed.
"""

import json
import sys
from typing import Any


class _Runtime:
    """Manages replay/live mode. Singleton."""

    def __init__(self) -> None:
        self._history: list[dict[str, Any]] = []
        self._replay_turn: int = 0
        self._replay_call: int = 0
        self._call_id: int = 0
        self._pending_live: str = ""

    def init(self, msg: dict[str, Any]) -> None:
        """Process turn message. Load history for replay if present."""
        history = msg.get("history", [])
        if history:
            self._history = history
            self._replay_turn = 0
            self._replay_call = 0
            # Buffer the live message for after replay finishes.
            self._pending_live = msg["message"]
        else:
            self._pending_live = ""

    @property
    def replaying(self) -> bool:
        return self._replay_turn < len(self._history)

    def replay_message(self) -> str:
        result: str = self._history[self._replay_turn]["message"]
        return result

    def replay_tool_result(self, tool: str) -> dict[str, Any]:
        calls = self._history[self._replay_turn]["calls"]
        expected = calls[self._replay_call]
        if expected["tool"] != tool:
            raise AssertionError(
                f"replay divergence: history has {expected['tool']}, "
                f"script called {tool}"
            )
        self._replay_call += 1
        if "error" in expected:
            raise RuntimeError(expected["error"])
        result: dict[str, Any] = expected["result"]
        return result

    def replay_done(self) -> None:
        self._replay_turn += 1
        self._replay_call = 0

    def next_call_id(self) -> int:
        self._call_id += 1
        return self._call_id


_rt = _Runtime()


def read_turn() -> str:
    """Read the next turn message. Blocks between turns.

    On recovery, returns cached messages from the inline history
    until replay catches up, then returns the live message.
    """
    if _rt.replaying:
        return _rt.replay_message()

    # Return the live message if replay just finished and we have it buffered.
    if _rt._pending_live:
        pending = _rt._pending_live
        _rt._pending_live = ""
        return pending

    line = sys.stdin.readline()
    if not line:
        raise SystemExit("stdin closed")
    payload: dict[str, Any] = json.loads(line)
    if payload["type"] != "turn":
        raise ValueError(f"expected turn, got {payload['type']}")

    _rt.init(payload)

    # If history was provided, replay from the first cached turn.
    if _rt.replaying:
        return _rt.replay_message()

    result: str = payload["message"]
    return result


def call_tool(tool: str, **args: Any) -> dict[str, Any]:
    """Call a tool and block for the result.

    During replay, returns cached results from the turn history.
    """
    if _rt.replaying:
        return _rt.replay_tool_result(tool)

    call_id = _rt.next_call_id()
    req = {"type": "call", "id": call_id, "tool": tool, "args": args}
    sys.stdout.write(json.dumps(req) + "\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("stdin closed while waiting for tool result")
    resp = json.loads(line)
    if resp["id"] != call_id:
        raise ValueError(f"id mismatch: expected {call_id}, got {resp['id']}")
    if "error" in resp:
        raise RuntimeError(resp["error"])
    if "data" not in resp:
        raise RuntimeError("malformed result: missing 'data' and 'error'")
    data: dict[str, Any] = resp["data"]
    return data


def done(response: str) -> None:
    """Signal turn completion. Script should loop back to read_turn().

    During replay, advances to the next cached turn. When replay is
    exhausted, the next read_turn() returns the live message.
    """
    if _rt.replaying:
        _rt.replay_done()
        return

    sys.stdout.write(json.dumps({"type": "done", "response": response}) + "\n")
    sys.stdout.flush()
