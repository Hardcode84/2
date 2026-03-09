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

### State model

The analogy with bare LLM providers (e.g. OpenRouter API) is direct: no
server-side state, conversation history replayed on each restore. For the
scripted provider, the "conversation" is the turn history — messages
received plus tool calls made and results returned. Plain dicts and
strings, nothing fancy.

Three tiers:

1. **Process alive** — state is in memory. Zero cost. Normal operation.
2. **Suspend/restore** — turn history serialized as JSON in the session
   state blob. On restore, spawn fresh process, feed cached turns through
   the helper. Cost = O(N) CPU but no I/O beyond reading the blob.
3. **Crash** — blob may be stale (last checkpoint, not last turn). Fall
   back to event log replay. Same mechanism, different data source.

The session checkpoints the turn history blob after every turn (cheap —
a few KB of JSON). If checkpointing is current, even crash recovery uses
the blob and the event log is not needed for replay.

See [State and Recovery](#state-and-recovery) for the mechanism.

### Multiplexer interaction

The multiplexer manages expensive LLM sessions (API connections, context
windows). A Python script blocked on stdin costs ~30MB memory, zero CPU.
Scripted sessions are exempt from LRU eviction — there's nothing to
reclaim.

If eviction is ever needed (many scripted agents), `suspend()` kills the
process and `restore()` replays from the turn history blob.

## Wire Protocol

JSON lines on stdin/stdout. One JSON object per line, `\n`-terminated.

### Provider → script (stdin)

**Turn start:**
```json
{"type": "turn", "message": "implement feature X", "history": [...]}
```

`history` is the turn history — a JSON array of past turns, each
containing the message, tool calls, and responses. Present when the
process is fresh and needs to recover state (suspend/restore, crash).
Empty array or absent on a long-lived process (normal operation).

```json
{"type": "turn", "message": "...", "history": [
  {"message": "implement feature X",
   "calls": [{"tool": "gate", "args": {"agent_name": "worker"}, "result": {"status": "gated"}},
             {"tool": "send_message", "args": {"recipient": "worker", "text": "..."}, "result": {"status": "delivered"}}],
   "response": "task dispatched"},
  {"message": "[state] worker: busy -> idle",
   "calls": [{"tool": "check_inbox", "args": {}, "result": {"messages": [...]}}],
   "response": "routing to critics"}
]}
```

On a long-lived process, `history` is omitted — no replay needed. The
helper detects whether replay is required based on the presence and
length of `history`.

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
   ├─ stdin: turn(h=[]) ─►│                    │
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
   ├─ stdin: turn(h=[]) ─►│                    │
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
        self._history: list[dict] = []  # Completed turns.
        self._current_turn: dict | None = None  # In-progress turn.

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        # 1. If no live process, spawn one:
        #    argv = ["python", script_path]
        #    wrap_command() adds bwrap (same as cursor-agent).
        #    asyncio.create_subprocess_exec(stdin=PIPE, stdout=PIPE, stderr=PIPE).
        # 2. Build turn message:
        #    - If process is fresh (just spawned) and history is non-empty,
        #      include history for replay.
        #    - Otherwise, omit history (process is live, no replay needed).
        # 3. Write {"type": "turn", "message": ..., "history": ...} to stdin.
        # 4. Track current turn: {"message": message, "calls": []}.
        # 5. Read loop on stdout:
        #    - "call" → dispatch via daemon RPC, record call+result in
        #      current_turn["calls"], write result to stdin.
        #    - "done" → yield response. Append current_turn to history.
        #      Checkpoint. Do NOT close stdin.
        #    - "error" → raise, turn fails.
        ...

    async def _checkpoint(self) -> None:
        """Persist turn history after each completed turn.

        Writes the state blob to the session store so crash recovery
        can use it instead of parsing the event log.
        """
        blob = self.suspend_sync()
        if self._log is not None:
            self._log.log("session.checkpoint", {"size": len(blob)})
        # SessionStore.save_state(self._session_id, blob)
        ...

    def suspend_sync(self) -> bytes:
        """Serialize state — script path, workspace, turn history."""
        return json.dumps({
            "script": str(self._script_path),
            "workspace": str(self._workspace),
            "history": self._history,
            "pid": self._proc.pid if self._proc else None,
        }).encode()

    async def suspend(self) -> bytes:
        return self.suspend_sync()

    async def stop(self) -> None:
        # Close stdin → script sees EOF → exits.
        # Kill if it doesn't exit within timeout.
        ...
```

The turn history is the session's "conversation context" — same role as
conversation messages in a bare LLM provider. Primitive data only: nested
dicts/lists with strings and numbers. `json.dumps` handles it. No pickle.

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

### The LLM analogy

A bare LLM API provider (no server-side state) stores the conversation
history and replays it on each API call. The scripted provider does the
same thing: the turn history IS the conversation context. On restore,
feed it through the script to rebuild state.

| | Bare LLM provider | Scripted provider |
|---|---|---|
| "Conversation" | Messages (user/assistant) | Turns (message + tool calls + results) |
| State blob | Serialized messages | Serialized turn history |
| Replay | Send history to API | Feed history through helper |
| Data format | JSON (primitive types) | JSON (primitive types) |

### Three tiers

| | Normal operation | Suspend/restore | Crash recovery |
|---|---|---|---|
| Process | Long-lived | Dead (killed or new spawn) | Dead |
| State source | Process memory | Turn history blob | Turn history blob (checkpoint) |
| Replay cost | Zero | O(N) — feed history to helper | O(N) — same mechanism |
| History in blob | Accumulating in memory | Complete | Last checkpoint |

### Turn history format

The state blob is JSON. The `history` field is a list of completed turns:

```json
{
  "script": "/path/to/pipeline.py",
  "workspace": "/path/to/workspace",
  "history": [
    {
      "message": "implement feature X",
      "calls": [
        {"tool": "gate", "args": {"agent_name": "worker"}, "result": {"status": "gated"}},
        {"tool": "send_message", "args": {"recipient": "worker", "text": "..."}, "result": {"status": "delivered"}}
      ],
      "response": "task dispatched"
    },
    {
      "message": "[state] worker: busy -> idle",
      "calls": [
        {"tool": "check_inbox", "args": {}, "result": {"messages": [...]}}
      ],
      "response": "routing to critics"
    }
  ]
}
```

Plain dicts, strings, numbers. No pickle, no custom types, no fragile
serialization. `json.dumps` handles everything. Debuggable with `jq`.

### Checkpointing

The session checkpoints the turn history blob after every completed turn.
Cost: a few KB of JSON written to the session store. Negligible for
scripted sessions (the turn history is small — tool calls and short
messages, not LLM-sized responses).

With per-turn checkpointing, the blob is always current. Even crash
recovery (no explicit suspend) uses the blob. The event log is still
written (by the provider/orchestrator) for debugging and auditing, but
is not needed for the script's own state recovery.

If checkpointing proves too frequent, we can reduce to every N turns
and fall back to the event log for the gap. But for scripts with ~10
tool calls per turn and ~50 turns, the history is <100KB. Not worth
optimizing.

### How replay works

When `send()` spawns a fresh process (after restore or crash), it includes
the turn history in the first turn message. The helper inside the sandbox
detects the history and replays:

1. `read_turn()` sees `history` is non-empty → enters replay mode.
2. For each past turn in history:
   - `read_turn()` returns the cached message.
   - `call_tool()` returns cached results. Verifies tool name matches
     as a sanity check (divergence = bug in script or corrupt history).
   - `done()` is a no-op — advances to next cached turn.
3. History exhausted → switch to live mode.
4. `read_turn()` returns the real message from the current turn.
5. `call_tool()` writes to stdout and reads from stdin (real dispatch).

From the script's perspective, every call behaves identically. The helper
swaps the backing I/O source internally.

### Replay sanity checks

During replay, the helper verifies that each `call_tool()` invocation
matches the cached history (tool name). A mismatch means the script's
logic diverged — this is a bug in the script (nondeterminism) or a
corrupt blob. The helper aborts with a clear error rather than silently
producing wrong state.

### Event log role

The event log is still written by the provider/orchestrator (tool calls,
turn boundaries, message delivery). It serves:

- **Debugging** — `cat events.jsonl | jq` shows the full history.
- **Other agents' recovery** — gate state, subscriptions, etc. are
  restored from the event log by the daemon, not by the script.
- **Fallback** — if the checkpoint blob is missing or corrupt, the
  event log can reconstruct the turn history. This path requires
  sender-side event logging (Phase 3) but is not the primary mechanism.

## Sandbox

Same bwrap model as cursor-agent. The `wrap_command` callback adds the
sandbox prefix. The scripted provider declares its own bind requirements
(minimal compared to cursor-agent — no `~/.cursor`, no network).

### Required bind mounts

| Host path | Mount path | Mode | Why |
|-----------|-----------|------|-----|
| Script file | `/script/<name>.py` | ro | The script to execute. |
| Helper library | `/script/substrat_script.py` | ro | Optional but recommended. |
| Workspace | `/workspace/` | ro/rw | Repo, views, workspace content. |
| Daemon socket | `/run/substrat.sock` | ro | Tool call RPC. |
| Python interpreter | (system path) | ro | System binds cover this. |

Note: the event log is NOT bound into the sandbox. Replay uses the turn
history passed inline via stdin. The event log stays on the host for
daemon-level recovery and debugging.

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
and transparent state recovery via inline turn history.
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
        self._live_message: str = ""

    def init(self, msg: dict[str, Any]) -> str:
        """Process turn message. Load history for replay if present."""
        self._live_message = msg["message"]
        history = msg.get("history", [])
        if history:
            self._history = history
            self._replay_turn = 0
            self._replay_call = 0
        return self._live_message

    @property
    def replaying(self) -> bool:
        return self._replay_turn < len(self._history)

    def replay_message(self) -> str:
        return self._history[self._replay_turn]["message"]

    def replay_tool_result(self, tool: str) -> dict[str, Any]:
        calls = self._history[self._replay_turn]["calls"]
        expected = calls[self._replay_call]
        assert expected["tool"] == tool, (
            f"replay divergence: history has {expected['tool']}, "
            f"script called {tool}"
        )
        self._replay_call += 1
        return expected["result"]

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

    line = sys.stdin.readline()
    if not line:
        raise SystemExit("stdin closed")
    msg = json.loads(line)
    assert msg["type"] == "turn", f"expected turn, got {msg['type']}"

    _rt.init(msg)

    # If history was provided, replay from the first cached turn.
    if _rt.replaying:
        return _rt.replay_message()

    return msg["message"]


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
    assert resp["id"] == call_id, (
        f"id mismatch: expected {call_id}, got {resp['id']}"
    )
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["data"]


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
```

No event log parsing. No file I/O. The history arrives inline via stdin
as part of the turn message. The helper just indexes into a list.

### Usage

```python
#!/usr/bin/env python3
"""Review pipeline — deterministic orchestrator."""
from substrat_script import read_turn, call_tool, done

# Turn 1: receive task, dispatch to worker.
task = read_turn()
call_tool("gate", agent_name="worker")
call_tool("send_message", recipient="worker", text=task)
call_tool("permit_turn", agent_name="worker")
done("task dispatched to worker")

# Turn 2: worker finished, route to critics.
msg = read_turn()  # blocks until worker signals completion
children = call_tool("list_children")
# ... fan out to critics, collect feedback ...
done("review complete")
```

Linear code. No state machine, no `match state:` blocks. The execution
position IS the state. On recovery, the helper replays past turns from
the inline history (cached tool results, no real dispatch), then the
current turn runs live.

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
| Replay divergence | Helper asserts tool name match against history. Abort with clear error. |
| Process dies between turns | Next `send()` detects dead process, spawns fresh one with history. Replay kicks in. |
| History blob missing/corrupt | Session starts with empty history. Script runs from scratch (first turn must handle this gracefully or fail). |

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

`suspend()` and `restore()` use the same turn history blob that
checkpointing writes. The blob is the session's portable state.

```python
async def restore(self, state: bytes, ...) -> ScriptedSession:
    data = json.loads(state)
    session = ScriptedSession(
        script_path=Path(data["script"]),
        workspace=Path(data["workspace"]),
        ...
    )
    session._history = data.get("history", [])
    # Process is dead (restore always means fresh start).
    # Next send() spawns a new process with history for replay.
    return session
```

Three cases:
1. **Process alive** (normal operation): `send()` writes to existing
   stdin. No history needed, no replay. Zero cost.
2. **Process dead** (suspend, eviction, crash): `send()` spawns fresh
   process, includes history in the turn message. Helper replays.
   Cost = O(past turns) CPU, no disk I/O.
3. **First create** (empty history): `send()` spawns process, no replay.

## Comparison with cursor-agent Provider

| | cursor-agent | scripted |
|---|---|---|
| Subprocess | cursor-agent CLI (per-turn) | python script.py (long-lived) |
| Tool dispatch | MCP server or daemon RPC | Daemon RPC (stdin/stdout bridge) |
| Sandbox binds | ~/.cursor, ~/.local, ~/.config/cursor | Script, helper, daemon socket |
| Network | Required (API calls) | Forbidden |
| State (normal) | SQLite session DB | Process memory |
| State (restore) | SQLite `--resume` | Turn history blob → replay |
| System prompt | .cursor/rules/*.mdc | Ignored (behavior is in code) |

## What This Replaces in review_pipeline.md

The `ScriptFn` / `HandlerResolver` / callback-injection design in the
Scripted Provider section of `review_pipeline.md` is superseded by this
document. The pipeline state machine, routing rules, and WAL recovery
sections remain valid — only the provider plumbing changes.
