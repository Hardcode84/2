# Scripted Provider

Deterministic Python scripts running inside bwrap, same sandbox model as
LLM agents. No special access to the daemon, no callback injection, no
substrat imports. The script is an untrusted workload that happens to be
predictable.

## Motivation

The review pipeline needs a non-LLM orchestrator that gates workers,
fans out to critics, and synthesizes feedback. The original design injected
`ToolHandler` callbacks directly into the script — that's not isolation,
that's shared memory with extra steps. This provider treats scripts exactly
like any other agent: sandboxed subprocess, tool calls over a wire protocol.

## Architecture

```
daemon
├── orchestrator
│   └── agent "pipeline" (provider=scripted, model=review-pipeline)
│       ├── ScriptedSession
│       │   ├── long-lived: bwrap ... python /script/pipeline.py
│       │   ├── stdin  → JSON lines (turn messages, tool results)
│       │   └── stdout ← JSON lines (tool calls, done)
│       └── bridges tool calls to ToolHandler (same as any agent)
```

The provider is a thin bridge between the subprocess's stdin/stdout and
the orchestrator's tool handler. It does not interpret the script's logic.

## Process Lifecycle

The script subprocess is **long-lived** — it stays alive across turns.
Between turns, it blocks on `stdin.readline()` waiting for the next
message. Process memory is the state. No serialization between turns.

```
spawn ──► turn 1 ──► block on stdin ──► turn 2 ──► block ──► ... ──► exit
```

This is the key difference from the spawn-per-turn model: zero replay
overhead on the normal path. The process IS the ephemeral state.

### Why not pickle?

Stdlib `pickle` cannot serialize generators. `dill` can, but it's fragile
— fails on generators that capture file handles, locks, or certain
closures. Adding a third-party dependency for a core reliability mechanism
is the kind of decision that haunts you at 3am. A living process achieves
the same goal with zero serialization.

### Crash recovery

On crash the process dies. The daemon restarts, replays its own event log
to restore agent state, and re-wakes the scripted agent. The provider
spawns a fresh subprocess. The helper library inside the sandbox reads the
event log and replays previous turns' tool calls to fast-forward the
script to the correct state — then switches to live I/O for the current
turn. Slow, but only happens on crash.

See [State and Recovery](#state-and-recovery) for the mechanism.

### Multiplexer interaction

The multiplexer manages expensive LLM sessions (API connections, context
windows). A Python script blocked on stdin costs ~30MB memory, zero CPU.
Scripted sessions are exempt from LRU eviction — there's nothing to
reclaim.

If eviction is ever needed (many scripted agents), `suspend()` kills the
process and `restore()` replays the event log — same path as crash
recovery. No pickle needed.

## Wire Protocol

JSON lines on stdin/stdout. One JSON object per line, `\n`-terminated.

### Provider → script (stdin)

**Turn start:**
```json
{"type": "turn", "message": "implement feature X", "events_path": "/state/events.jsonl", "turn_seq": 3}
```

`events_path` points to the agent's event log inside the sandbox (RO bind).
May be `null` if no log exists yet (first turn).

`turn_seq` is a zero-based turn counter. On a fresh process with
`turn_seq > 0`, the helper replays turns 0..N-1 from the event log before
processing turn N live. On a long-lived process (normal operation),
`turn_seq` is informational — no replay needed.

**Tool result:**
```json
{"type": "result", "id": 1, "data": {"status": "gated"}}
```

**Tool error:**
```json
{"type": "result", "id": 1, "error": "agent not found: ghost"}
```

`id` matches the tool call that triggered it.

### Script → provider (stdout)

**Tool call:**
```json
{"type": "call", "id": 1, "tool": "gate", "args": {"agent_name": "worker"}}
```

Script assigns `id` values. They must be unique within a turn (monotonic
integer is fine). The script blocks on stdin after writing a call — one
outstanding call at a time.

**Done:**
```json
{"type": "done", "response": "worker dispatched, waiting for completion"}
```

The `response` field becomes the agent's turn output (same as an LLM's
final text). After writing `done`, the script loops back to `read_turn()`
and blocks on stdin — it does NOT exit.

**Error (script-initiated):**
```json
{"type": "error", "message": "unexpected state, cannot recover"}
```

Provider treats this as a turn failure. Logged, agent goes IDLE.

### Sequence diagram (multi-turn)

```
provider              script (bwrap)
   │                      │
   │  ┌── turn 1 ─────────────────────────────┐
   ├─ stdin: turn(seq=0)─►│                    │
   │                      ├── gate worker      │
   │◄── stdout: call ─────┤                    │
   ├─ stdin: result ─────►│                    │
   │                      ├── send to worker   │
   │◄── stdout: call ─────┤                    │
   ├─ stdin: result ─────►│                    │
   │◄── stdout: done ─────┤                    │
   │  └────────────────────────────────────────┘
   │                      │  (blocked on stdin — process alive)
   │                      │
   │  ┌── turn 2 ─────────────────────────────┐
   ├─ stdin: turn(seq=1)─►│                    │
   │                      ├── check inbox      │
   │◄── stdout: call ─────┤                    │
   ├─ stdin: result ─────►│                    │
   │◄── stdout: done ─────┤                    │
   │  └────────────────────────────────────────┘
   │                      │  (blocked on stdin)
   ...
```

## Provider Implementation

### ScriptedProvider (factory)

```python
class ScriptedProvider:
    name = "scripted"

    async def create(
        self,
        model: str | None,         # Script path.
        system_prompt: str,         # Passed in turn message, script may ignore.
        log: EventLog | None = None,
        *,
        workspace: Path | None = None,
        wrap_command: CommandWrapper | None = None,
        agent_id: UUID | None = None,
        daemon_socket: str | None = None,
    ) -> ScriptedSession: ...

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
        *,
        wrap_command: CommandWrapper | None = None,
    ) -> ScriptedSession: ...

    def models(self) -> list[str]: ...
```

`model` identifies which script to run. Absolute or workspace-relative
path to a `.py` file: `--model /scripts/review-pipeline.py`.

`system_prompt` is included in the turn message. Deterministic scripts
typically ignore it — their behavior is in the code.

### ScriptedSession

```python
class ScriptedSession:
    def __init__(self, ...):
        self._proc: asyncio.subprocess.Process | None = None
        self._turn_seq: int = 0

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        # 1. If no live process, spawn one:
        #    argv = ["python", script_path]
        #    wrap_command() adds bwrap (same as cursor-agent).
        #    asyncio.create_subprocess_exec(stdin=PIPE, stdout=PIPE, stderr=PIPE).
        # 2. Write {"type": "turn", "turn_seq": N, ...} to stdin.
        # 3. Read loop on stdout:
        #    - "call" → dispatch tool via daemon RPC, write result to stdin.
        #    - "done" → yield response. Do NOT close stdin.
        #    - "error" → raise, turn fails.
        # 4. Increment turn_seq.
        ...

    async def suspend(self) -> bytes:
        # Process stays alive — return metadata for reconnection.
        # If we must kill (eviction), replay on restore.
        return json.dumps({
            "script": str(self._script_path),
            "workspace": str(self._workspace),
            "turn_seq": self._turn_seq,
        }).encode()

    async def stop(self) -> None:
        # Close stdin → script sees EOF → exits.
        # Kill if it doesn't exit within timeout.
        ...
```

### Tool dispatch

The session bridges tool calls to the daemon the same way cursor-agent's
non-MCP path does — via `tool.call` RPC:

```python
resp = await async_call(
    daemon_socket,
    "tool.call",
    {"agent_id": agent_id.hex, "tool": name, "arguments": args},
)
```

No `ToolHandler` reference, no `HandlerResolver`, no lazy callbacks. The
session talks to the daemon over UDS like any other client. The daemon
socket path is bind-mounted into the sandbox.

### Deadlock prevention

Classic pipe deadlock: script blocks writing to full stdout buffer while
provider blocks reading stdout. Solution: `asyncio.create_subprocess_exec`
with `PIPE` for both stdin and stdout. The provider's read loop is async —
it reads stdout without blocking the event loop, then writes to stdin when
a result is ready. No threads needed.

The script side is synchronous: write call → flush → readline. Standard
blocking I/O. The pipe buffers (64KB default on Linux) are more than
sufficient for JSON tool calls.

## State and Recovery

### Two paths, one script

| | Normal operation | Crash recovery |
|---|---|---|
| Process | Long-lived, stays alive | Fresh spawn |
| State source | Process memory | Event log replay |
| Replay cost | Zero | O(N) where N = past turns |
| Who handles it | Nobody — it's in memory | Helper library |

The script author writes the same linear code for both paths. The helper
library switches between live I/O and replay transparently.

### How replay works

On process start, the helper checks `turn_seq` from the turn message. If
`turn_seq > 0` and the process is fresh (no prior state), the helper
reads the event log and replays previous turns:

1. Parse the event log at `events_path`.
2. Extract tool call/result pairs grouped by turn (using `tool.*` events
   and `turn.start`/`turn.end` markers).
3. Build a replay queue: `list[list[tuple[call, result]]]` — one list
   per past turn.
4. For turns 0..N-1:
   - `read_turn()` returns the cached message (from `message.delivered`
     events in the log).
   - `call_tool()` returns cached results from the replay queue instead
     of writing to stdout. Verifies the call matches the log (tool name
     + args) as a sanity check.
   - `done()` is a no-op (the provider already knows this turn completed).
5. Replay queue exhausted → switch to live mode.
6. Turn N: `read_turn()` blocks on stdin for the real message.
   `call_tool()` writes to stdout and reads results from stdin.

From the script's perspective, every call behaves identically. The helper
swaps the backing I/O source internally.

### Helper replay internals

```python
class _Runtime:
    """Manages replay/live mode switching. Internal to the helper."""

    def __init__(self) -> None:
        self._replay_turns: list[_ReplayTurn] | None = None
        self._replay_idx: int = 0
        self._call_idx: int = 0

    def _load_replay(self, events_path: str, turn_seq: int) -> None:
        """Parse event log into replay turns if recovery is needed."""
        if turn_seq == 0:
            return
        events = _parse_events(events_path)
        self._replay_turns = _group_by_turn(events)
        # Sanity: log should have entries for turns 0..turn_seq-1.
        assert self._replay_turns is not None
        assert len(self._replay_turns) >= turn_seq - 1

    @property
    def replaying(self) -> bool:
        return (
            self._replay_turns is not None
            and self._replay_idx < len(self._replay_turns)
        )

    def next_turn_message(self) -> str:
        """Return cached message for current replay turn."""
        assert self._replay_turns is not None
        turn = self._replay_turns[self._replay_idx]
        return turn.message

    def next_tool_result(self, tool: str, args: dict) -> dict:
        """Return cached tool result, verify call matches log."""
        assert self._replay_turns is not None
        turn = self._replay_turns[self._replay_idx]
        expected = turn.calls[self._call_idx]
        assert expected.tool == tool, (
            f"replay mismatch: expected {expected.tool}, got {tool}"
        )
        self._call_idx += 1
        return expected.result

    def finish_replay_turn(self) -> None:
        """Advance to next replay turn."""
        self._replay_idx += 1
        self._call_idx = 0
```

### What the event log must contain

For replay to work, the log needs sender-side events (Phase 3 of the
review pipeline plan). Specifically:

- `turn.start` / `turn.end` — turn boundaries for grouping.
- `tool.send_message`, `tool.gate`, `tool.permit_turn`, etc. — the tool
  calls the script made, with arguments and results.
- `message.delivered` — the message that triggered each turn (so replay
  can feed it back to `read_turn()`).

These events are logged by the provider/orchestrator, not by the script.
The script makes tool calls via stdout; the provider logs them after
dispatching via daemon RPC.

### Replay sanity checks

During replay, the helper verifies that each `call_tool()` invocation
matches the logged event (tool name, arguments). A mismatch means the
script's logic diverged from the log — this is a bug in the script (or
the log is corrupt). The helper aborts with a clear error rather than
silently producing wrong state.

## Sandbox

Same bwrap model as cursor-agent. The `wrap_command` callback adds the
sandbox prefix. The scripted provider declares its own bind requirements
(minimal compared to cursor-agent — no `~/.cursor`, no network).

### Required bind mounts

| Host path | Mount path | Mode | Why |
|-----------|-----------|------|-----|
| Script file | `/script/<name>.py` | ro | The script to execute. |
| Helper library | `/script/substrat_script.py` | ro | Optional but recommended. |
| Event log | `/state/events.jsonl` | ro | Crash recovery replay. |
| Workspace | `/workspace/` | ro/rw | Repo, views, workspace content. |
| Daemon socket | `/run/substrat.sock` | ro | Tool call RPC. |
| Python interpreter | (system path) | ro | System binds cover this. |

### What the script does NOT get

- Network access. Deterministic scripts don't call APIs.
- Write access to the event log. The provider logs events, not the script.
- Any substrat library code. The script uses stdlib only (+ the helper).
- Direct access to other agents' state. Everything goes through tools.

## Helper Library

`substrat_script.py` — stdlib-only module that wraps the JSON protocol
and manages replay transparently. Bind-mounted into the sandbox at
`/script/substrat_script.py`.

```python
"""Helper for scripts running under the scripted provider.

Zero dependencies beyond stdlib. Handles the stdin/stdout JSON protocol
and transparent crash recovery replay.
"""
import json
import sys
from typing import Any


class _ReplayTurn:
    """A single turn's worth of cached data for replay."""

    __slots__ = ("message", "calls")

    def __init__(self, message: str, calls: list[dict[str, Any]]) -> None:
        self.message = message
        self.calls = calls  # [{"tool": ..., "args": ..., "result": ...}, ...]


class _Runtime:
    """Manages replay/live mode. Singleton, created on first read_turn()."""

    def __init__(self) -> None:
        self._replay_turns: list[_ReplayTurn] = []
        self._replay_turn_idx: int = 0
        self._replay_call_idx: int = 0
        self._live: bool = True
        self._call_id: int = 0

    def init_from_turn(self, turn_seq: int, events_path: str | None) -> None:
        """Load replay data if this is a fresh process recovering state."""
        if turn_seq == 0 or events_path is None:
            return
        self._replay_turns = _parse_replay_turns(events_path)
        if self._replay_turns:
            self._live = False

    @property
    def replaying(self) -> bool:
        return not self._live

    def replay_message(self) -> str:
        turn = self._replay_turns[self._replay_turn_idx]
        return turn.message

    def replay_tool_result(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        turn = self._replay_turns[self._replay_turn_idx]
        expected = turn.calls[self._replay_call_idx]
        assert expected["tool"] == tool, (
            f"replay divergence: log has {expected['tool']}, script called {tool}"
        )
        self._replay_call_idx += 1
        return expected["result"]

    def replay_done(self) -> None:
        self._replay_turn_idx += 1
        self._replay_call_idx = 0
        if self._replay_turn_idx >= len(self._replay_turns):
            self._live = True

    def next_call_id(self) -> int:
        self._call_id += 1
        return self._call_id


_rt = _Runtime()


def _parse_replay_turns(events_path: str) -> list[_ReplayTurn]:
    """Parse event log into per-turn replay data."""
    # Implementation reads JSONL, groups tool.* events by turn boundaries.
    # Returns list of _ReplayTurn with message and tool call/result pairs.
    ...


def read_turn() -> tuple[str, str | None]:
    """Read the next turn message. Blocks between turns.

    On crash recovery, returns cached messages from the event log
    until replay catches up, then switches to live stdin.
    """
    if _rt.replaying:
        return _rt.replay_message(), None

    line = sys.stdin.readline()
    if not line:
        raise SystemExit("stdin closed")
    msg = json.loads(line)
    assert msg["type"] == "turn", f"expected turn, got {msg['type']}"

    # First call initialises replay if needed.
    _rt.init_from_turn(msg.get("turn_seq", 0), msg.get("events_path"))
    if _rt.replaying:
        return _rt.replay_message(), None

    return msg["message"], msg.get("events_path")


def call_tool(tool: str, **args: Any) -> dict[str, Any]:
    """Call a tool and block for the result.

    During replay, returns cached results from the event log.
    """
    if _rt.replaying:
        return _rt.replay_tool_result(tool, args)

    call_id = _rt.next_call_id()
    req = {"type": "call", "id": call_id, "tool": tool, "args": args}
    sys.stdout.write(json.dumps(req) + "\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("stdin closed while waiting for tool result")
    resp = json.loads(line)
    assert resp["id"] == call_id, f"id mismatch: expected {call_id}, got {resp['id']}"
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["data"]


def done(response: str) -> None:
    """Signal turn completion. Script should loop back to read_turn().

    During replay, advances to the next cached turn.
    """
    if _rt.replaying:
        _rt.replay_done()
        return

    sys.stdout.write(json.dumps({"type": "done", "response": response}) + "\n")
    sys.stdout.flush()
```

### Usage

```python
#!/usr/bin/env python3
"""Review pipeline — deterministic orchestrator."""
from substrat_script import read_turn, call_tool, done

# Turn 1: receive task, dispatch to worker.
task, _ = read_turn()
call_tool("gate", agent_name="worker")
call_tool("send_message", recipient="worker", text=task)
call_tool("permit_turn", agent_name="worker")
done("task dispatched to worker")

# Turn 2: worker finished, route to critics.
msg, _ = read_turn()  # blocks until worker signals completion
children = call_tool("list_children")
# ... fan out to critics, collect feedback ...
done("review complete")
```

Linear code. No state machine, no `match state:` blocks. The execution
position IS the state. On crash, the helper replays turns 1..N-1 silently,
then turn N runs live.

## Stderr

Script stderr is forwarded to the agent's event log as diagnostic output.
Same treatment as cursor-agent stderr — captured and logged on turn
completion (or on failure, included in the error message).

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Script exits non-zero | Turn failure. Stderr logged. Agent goes IDLE. |
| Script writes invalid JSON | Parse error. Turn failure. |
| Script hangs (no output) | Timeout (configurable, default 60s). Kill. Turn failure. |
| Tool call returns error | Script receives `{"type": "result", "id": N, "error": "..."}`. Script decides whether to retry, skip, or abort. |
| Stdin closed unexpectedly | Script gets empty readline, should exit. |
| Script writes unknown type | Provider ignores it, logs warning. |
| Replay divergence | Helper asserts tool name match. Abort with clear error. |
| Process dies between turns | Next `send()` detects dead process, spawns fresh one. Replay kicks in. |

## Registration and CLI

```bash
substrat agent create pipeline \
    --provider scripted \
    --model /path/to/review_pipeline.py \
    --parent project-A \
    --workspace project-A-ws
```

The provider resolves `model` as a path to the script file. The file must
be readable on the host (it gets bound RO into the sandbox).

## Suspend / Restore

```python
async def suspend(self) -> bytes:
    # Process stays alive. Record enough to reconnect or replay.
    return json.dumps({
        "script": str(self._script_path),
        "workspace": str(self._workspace),
        "turn_seq": self._turn_seq,
        "pid": self._proc.pid if self._proc else None,
    }).encode()

async def restore(self, state: bytes, ...) -> ScriptedSession:
    data = json.loads(state)
    # Try to reconnect to living process (check PID).
    # If dead, create session with turn_seq — next send() will
    # spawn fresh process and helper will replay.
    ...
```

Three cases:
1. **Process alive** (normal suspend/restore): reconnect to existing
   stdin/stdout. Zero cost.
2. **Process dead** (crash or eviction kill): spawn fresh subprocess on
   next `send()`. Helper replays from event log. Cost = O(past turns).
3. **First create** (`turn_seq=0`): spawn subprocess, no replay needed.

## Comparison with cursor-agent Provider

| | cursor-agent | scripted |
|---|---|---|
| Subprocess | cursor-agent CLI (per-turn) | python script.py (long-lived) |
| Tool dispatch | MCP server or daemon RPC | Daemon RPC (stdin/stdout bridge) |
| Sandbox binds | ~/.cursor, ~/.local, ~/.config/cursor | Script, helper, event log, daemon socket |
| Network | Required (API calls) | Forbidden |
| State (normal) | SQLite session DB | Process memory |
| State (crash) | SQLite survives (WAL mode) | Event log replay |
| System prompt | .cursor/rules/*.mdc | Ignored (behavior is in code) |

## What This Replaces in review_pipeline.md

The `ScriptFn` / `HandlerResolver` / callback-injection design in the
Scripted Provider section of `review_pipeline.md` is superseded by this
document. The pipeline state machine, routing rules, and WAL recovery
sections remain valid — only the provider plumbing changes.
