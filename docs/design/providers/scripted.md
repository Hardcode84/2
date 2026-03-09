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
│       ├── ScriptedSession.send()
│       │   ├── spawns: bwrap ... python /script/pipeline.py
│       │   ├── stdin  → JSON lines (turn message, tool results)
│       │   └── stdout ← JSON lines (tool calls, done)
│       └── bridges tool calls to ToolHandler (same as any agent)
```

The provider is a thin bridge between the subprocess's stdin/stdout and
the orchestrator's tool handler. It does not interpret the script's logic.

## Wire Protocol

JSON lines on stdin/stdout. One JSON object per line, `\n`-terminated.

### Provider → script (stdin)

**Turn start:**
```json
{"type": "turn", "message": "implement feature X", "events_path": "/state/events.jsonl"}
```

`events_path` points to the agent's event log inside the sandbox (RO bind).
The script reads it for WAL-based state recovery. May be `null` if no log
exists yet (first turn).

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
final text). The subprocess should exit after writing `done`.

**Error (script-initiated):**
```json
{"type": "error", "message": "unexpected state, cannot recover"}
```

Provider treats this as a turn failure. Logged, agent goes IDLE.

### Sequence diagram

```
provider              script (bwrap)
   │                      │
   ├─ stdin: turn ───────►│
   │                      ├── reads events.jsonl, recovers state
   │                      ├── decides: gate worker
   │◄── stdout: call ─────┤
   │                      │  (blocks on stdin)
   ├─ stdin: result ─────►│
   │                      ├── decides: send message to worker
   │◄── stdout: call ─────┤
   │                      │  (blocks on stdin)
   ├─ stdin: result ─────►│
   │                      ├── decides: done
   │◄── stdout: done ─────┤
   │                      └── exit(0)
```

## Provider Implementation

### ScriptedProvider (factory)

```python
class ScriptedProvider:
    name = "scripted"

    async def create(
        self,
        model: str | None,         # Script identifier (path or name).
        system_prompt: str,         # Ignored — scripts don't need prompts.
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

`model` identifies which script to run. Two modes:

1. **Path** — absolute or workspace-relative path to a `.py` file.
   `--model /scripts/review-pipeline.py`
2. **Registry name** — short name registered at daemon startup.
   `--model review-pipeline` → resolved to a path by the provider.

For v1, path-only. Registry is a convenience for later.

`system_prompt` is passed through to the turn message but the script is
free to ignore it. Deterministic scripts typically don't need it — their
behavior is in the code.

### ScriptedSession

```python
class ScriptedSession:
    async def send(self, message: str) -> AsyncGenerator[str, None]:
        # 1. Build argv: ["python", script_path]
        # 2. wrap_command() adds bwrap (same as cursor-agent).
        # 3. Spawn subprocess (asyncio.create_subprocess_exec).
        # 4. Write {"type": "turn", ...} to stdin.
        # 5. Read loop on stdout:
        #    - "call" → dispatch tool via daemon RPC, write result to stdin.
        #    - "done" → yield response, wait for exit.
        #    - "error" → raise, turn fails.
        # 6. On subprocess death → check exit code, clean up.
        ...

    async def suspend(self) -> bytes:
        return b""  # Stateless. State is in the event log.

    async def stop(self) -> None:
        # Kill subprocess if still running.
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

## Sandbox

Same bwrap model as cursor-agent. The `wrap_command` callback adds the
sandbox prefix. The scripted provider declares its own bind requirements
(minimal compared to cursor-agent — no `~/.cursor`, no network).

### Required bind mounts

| Host path | Mount path | Mode | Why |
|-----------|-----------|------|-----|
| Script file | `/script/<name>.py` | ro | The script to execute. |
| Event log | `/state/events.jsonl` | ro | WAL recovery. |
| Workspace | `/workspace/` | ro/rw | Repo, views, workspace content. |
| Daemon socket | `/run/substrat.sock` | ro | Tool call RPC. |
| Python interpreter | (system path) | ro | System binds cover this. |

### What the script does NOT get

- Network access. Deterministic scripts don't call APIs.
- Write access to the event log. The provider logs events, not the script.
- Any substrat library code. The script uses stdlib only.
- Direct access to other agents' state. Everything goes through tools.

## Helper Library

Optional `substrat_script.py` — a tiny (~50 lines) stdlib-only module that
wraps the JSON protocol so script authors don't hand-roll `json.loads` loops.

```python
"""Helper for scripts running under the scripted provider.

Zero dependencies beyond stdlib. Bind-mount into the sandbox or
copy into the script's directory.
"""
import json
import sys
from typing import Any


def read_turn() -> tuple[str, str | None]:
    """Read the turn message. Returns (message, events_path)."""
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("stdin closed before turn message")
    msg = json.loads(line)
    assert msg["type"] == "turn", f"expected turn, got {msg['type']}"
    return msg["message"], msg.get("events_path")


def call_tool(tool: str, **args: Any) -> dict[str, Any]:
    """Call a tool and block for the result."""
    call_id = call_tool._next_id  # type: ignore[attr-defined]
    call_tool._next_id += 1  # type: ignore[attr-defined]
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

call_tool._next_id = 1  # type: ignore[attr-defined]


def done(response: str) -> None:
    """Signal turn completion."""
    sys.stdout.write(json.dumps({"type": "done", "response": response}) + "\n")
    sys.stdout.flush()
```

Usage in a script:

```python
#!/usr/bin/env python3
from substrat_script import read_turn, call_tool, done

message, events_path = read_turn()

call_tool("gate", agent_name="worker")
call_tool("send_message", recipient="worker", text=message)
call_tool("permit_turn", agent_name="worker")

done("task dispatched to worker")
```

The helper is not required. Scripts can speak the protocol directly. It
exists to reduce boilerplate for the common case.

## Event Log Access

The script needs its own event log for WAL-based crash recovery. On each
turn, it replays the log to reconstruct state (e.g., the pipeline state
machine position).

### What gets bound

The agent's event log file — the same JSONL file the daemon appends to.
Bound read-only at a known path (`/state/events.jsonl` inside the sandbox,
actual host path is `~/.substrat/sessions/<uuid>/events.jsonl`).

### Consistency

The event log is append-only. The daemon flushes before spawning the
script subprocess. The script sees all events up to the current turn start.
Events from the current turn (tool calls the script is about to make) are
not in the log yet — they're logged by the provider after each tool call
completes.

This means the script's `reconstruct_state()` always sees the state as of
the previous turn's end. Current-turn actions are implicit in the script's
own execution flow.

### Open question: what else to bind?

The event log is the minimum. But the script might also need:

- **Child agents' event logs** — to inspect what happened in a worker's
  session. Probably not — the script should use `inspect_agent` /
  `check_inbox` tools instead.
- **Workspace files** — to run `git diff` or read config. Yes — the
  workspace is already bound (same as any agent).
- **Config file** — `pipeline.toml` or similar. Lives in the workspace,
  no extra bind needed.

For v1: event log + workspace + daemon socket. Expand if proven insufficient.

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

## Registration and CLI

```bash
# Create a scripted agent with a script path.
substrat agent create pipeline \
    --provider scripted \
    --model /path/to/review_pipeline.py \
    --parent project-A \
    --workspace project-A-ws
```

The provider resolves `model` as a path to the script file. The file must
be readable on the host (it gets bound RO into the sandbox).

## Suspend / Restore

Scripted sessions are stateless. `suspend()` returns a minimal blob with
the script path and workspace — enough to reconstruct the session.

```python
async def suspend(self) -> bytes:
    return json.dumps({
        "script": str(self._script_path),
        "workspace": str(self._workspace),
    }).encode()

async def restore(self, state: bytes, ...) -> ScriptedSession:
    data = json.loads(state)
    # Reconstruct session from path + workspace.
    ...
```

No conversation history to preserve — the event log is the only state,
and it's managed by the daemon.

## Comparison with cursor-agent Provider

| | cursor-agent | scripted |
|---|---|---|
| Subprocess | cursor-agent CLI | python script.py |
| Tool dispatch | MCP server (child process) or daemon RPC | Daemon RPC (stdin/stdout bridge) |
| Sandbox binds | ~/.cursor, ~/.local, ~/.config/cursor | Script file, event log, daemon socket |
| Network | Required (API calls) | Forbidden |
| State | SQLite session DB | None (event log) |
| System prompt | .cursor/rules/*.mdc | Ignored (behavior is in code) |
| Suspend blob | Session ID + workspace | Script path + workspace |

## What This Replaces in review_pipeline.md

The `ScriptFn` / `HandlerResolver` / callback-injection design in the
Scripted Provider section of `review_pipeline.md` is superseded by this
document. The pipeline state machine, routing rules, and WAL recovery
sections remain valid — only the provider plumbing changes.
