# Substrat — Implementation Details

Draft implementation design for the Substrat agent orchestration framework.
Companion to `substrat.md` (high-level design).

---

## 1. Project Structure

```
substrat/
├── pyproject.toml
├── src/
│   └── substrat/
│       ├── __init__.py
│       ├── __main__.py            # python -m substrat entry point.
│       ├── cli/
│       │   ├── __init__.py
│       │   ├── app.py             # Typer top-level app.
│       │   ├── daemon_cmds.py     # start/stop/status commands.
│       │   ├── session_cmds.py    # session CRUD commands.
│       │   ├── agent_cmds.py      # agent interaction commands.
│       │   └── workspace_cmds.py  # workspace management commands.
│       ├── daemon/
│       │   ├── __init__.py
│       │   ├── server.py          # UDS server, asyncio event loop.
│       │   ├── registry.py        # Session and agent registries.
│       │   ├── handler.py         # Request dispatch from CLI client.
│       │   └── lifecycle.py       # Daemon start/stop/pidfile.
│       ├── session/
│       │   ├── __init__.py
│       │   ├── model.py           # Session dataclass and state machine.
│       │   ├── store.py           # Persistence (save/load/list).
│       │   └── multiplexer.py     # Session slot management (LRU eviction).
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── tree.py            # Agent hierarchy tree.
│       │   ├── node.py            # Single agent node (identity, state).
│       │   └── messaging.py       # Envelope, inbox/outbox, multicast.
│       ├── provider/
│       │   ├── __init__.py
│       │   ├── base.py            # Provider protocol (abstract).
│       │   ├── cursor_agent.py    # cursor-agent CLI subprocess wrapper.
│       │   └── registry.py        # Provider discovery and instantiation.
│       ├── workspace/
│       │   ├── __init__.py
│       │   ├── model.py           # Workspace spec dataclass.
│       │   ├── bwrap.py           # bubblewrap invocation builder.
│       │   ├── symlinks.py        # Symlink composition (RO/RW).
│       │   └── hierarchy.py       # Parent-child workspace nesting.
│       ├── logging/
│       │   ├── __init__.py
│       │   ├── jsonl.py           # Structured JSONL writer/reader.
│       │   └── plaintext.py       # Human-readable log writer.
│       └── protocol/
│           ├── __init__.py
│           └── ipc.py             # UDS message framing (CLI ↔ daemon).
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_session_model.py
│   │   ├── test_agent_tree.py
│   │   ├── test_messaging.py
│   │   ├── test_workspace_model.py
│   │   ├── test_bwrap.py
│   │   └── test_provider_base.py
│   └── integration/
│       ├── test_daemon_lifecycle.py
│       ├── test_cli_daemon.py
│       └── test_session_persistence.py
└── docs/
    ├── substrat.md
    └── implementation.md           # This file.
```

Entry points in `pyproject.toml`:

```toml
[project.scripts]
substrat = "substrat.cli.app:main"
```

`__main__.py` delegates to the same CLI entry for `python -m substrat`.

---

## 2. Daemon Architecture

### Overview

The daemon is a long-running process that owns all agent sessions, manages the
agent tree, and brokers messages. The CLI is a thin client that talks to the
daemon over a Unix domain socket.

### Event Loop

Single `asyncio` event loop drives:
- UDS server accepting CLI connections.
- Agent provider I/O (stdin/stdout for subprocess providers, HTTP for API
  providers).
- Inter-agent message routing.

Blocking operations (bwrap setup, subprocess spawning, file I/O for large logs)
run in a `ThreadPoolExecutor` via `loop.run_in_executor`.

### UDS Server

```
~/.substrat/daemon.sock
```

The server uses `asyncio.start_unix_server`. Each accepted connection gets its
own handler coroutine.

Wire format (CLI ↔ daemon): newline-delimited JSON. Each message is a single
JSON object terminated by `\n`. See `protocol/ipc.py` in the project structure.

### Daemon Lifecycle

```
~/.substrat/
├── daemon.sock      # UDS endpoint.
├── daemon.pid       # PID file for single-instance enforcement.
├── sessions/        # Persisted session state.
│   └── <uuid>/
│       └── state.json
├── agents/          # Per-agent logs.
│   └── <uuid>/
│       └── logs/
└── workspaces/      # Workspace roots.
    └── <uuid>/
```

Startup sequence:
1. Check for stale PID file, remove if process is dead.
2. Write PID file.
3. Create UDS socket.
4. Initialize session registry from `~/.substrat/sessions/`.
5. Enter event loop.

Shutdown: SIGTERM/SIGINT → graceful drain. Suspend all active sessions, flush
logs, remove socket and PID file.

### Session Registry

In-memory dict: `dict[UUID, Session]`. On mutation the affected session is
persisted to `~/.substrat/sessions/<uuid>/state.json`.

### Request Dispatch

Incoming CLI requests are JSON objects with a `method` field:

```json
{"id": "req-1", "method": "agent.create", "params": {...}}
```

`handler.py` maps method strings to handler coroutines. Responses carry the
same `id`:

```json
{"id": "req-1", "result": {...}}
{"id": "req-1", "error": {"code": 1, "message": "..."}}
```

---

## 3. CLI Design

Typer-based. Most commands map 1:1 to daemon RPC methods (exception: `attach`
uses a long-lived bidirectional stream instead of request/response).

```
substrat daemon start       # Fork and daemonize.
substrat daemon stop        # Send shutdown request.
substrat daemon status      # Print daemon state.

substrat agent create [--provider cursor-agent] [--name <name>]  # Create root agent.
substrat agent list                           # Show agent tree (all roots + children).
substrat agent attach <agent-id>              # Interactive REPL with an agent.
substrat agent inspect <agent-id>             # Show agent activity.
substrat agent send <agent-id> <message>      # One-shot message.

substrat session list                         # List all sessions.
substrat session suspend <uuid>
substrat session resume <uuid>
substrat session delete <uuid>

substrat workspace create [--parent <ws-uuid>]
substrat workspace list
substrat workspace link <ws-uuid> <host-path> <mount-path> [--ro|--rw]
substrat workspace delete <ws-uuid>
```

`agent create` is the primary entry point — it creates a root agent (and its
backing session) in one step. Sessions are an implementation detail the user
rarely manages directly; the `session` subcommands exist for low-level control
(suspend/resume/delete).

### Connection

CLI opens `~/.substrat/daemon.sock`, sends one request, reads one response,
closes. For `agent attach`, the connection stays open as a bidirectional
stream (daemon pushes agent output, CLI sends user input).

### Interactive Attach

`agent attach <agent-id>` enters a REPL with any agent (typically a root):
- User input → message to the attached agent.
- Agent output → streamed back to terminal.
- Ctrl-C → detach (agent stays alive in daemon).
- `/quit` → detach.

---

## 4. Agent Provider Abstraction

### Protocol

```python
from typing import Protocol, AsyncGenerator
from uuid import UUID

class AgentProvider(Protocol):
    """Interface that all LLM/agent backends must implement."""

    name: str

    async def start(self, session_id: UUID, instructions: str) -> None:
        """Start or resume a conversation. Called once per agent lifetime."""
        ...

    def send(self, message: str) -> AsyncGenerator[str, None]:
        """Send a message, yield streamed response chunks."""
        ...

    async def suspend(self) -> bytes:
        """Serialize provider-specific state for later resumption."""
        ...

    async def resume(self, state: bytes) -> None:
        """Restore from previously serialized state."""
        ...

    async def stop(self) -> None:
        """Terminate the provider. Release resources."""
        ...
```

### cursor-agent Implementation

`cursor-agent` is a CLI binary. Each agent instance spawns one subprocess.

```python
class CursorAgentProvider:
    name = "cursor-agent"

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._session_dir: Path | None = None

    async def start(self, session_id: UUID, instructions: str) -> None:
        # Launch: cursor-agent --session-dir <path> --instructions <instructions>
        # Communicate via stdin/stdout JSON lines.
        ...

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        # Write to stdin, read streamed response lines from stdout.
        yield ...

    async def suspend(self) -> bytes:
        # cursor-agent manages its own session state on disk.
        # Return the session dir path as the opaque state blob.
        ...

    async def resume(self, state: bytes) -> None:
        # Restore session dir path from state, relaunch subprocess.
        ...

    async def stop(self) -> None:
        # Send EOF to stdin, wait for exit, kill if timeout.
        ...
```

Communication with the subprocess is via stdin/stdout. Stderr is captured to
the session log. The provider reads stdout line-by-line, each line being a JSON
object from cursor-agent's output protocol.

### Future Providers

- **Claude CLI**: same subprocess pattern, different CLI flags and output
  format. Claude CLI supports `--resume <session-id>` for native session
  persistence. `suspend()` serializes the Claude session ID string;
  `resume()` passes it back as `--resume`. No conversation history management
  needed — Claude's server retains the context.
- **OpenRouter API**: HTTP-based, uses `aiohttp` or `httpx`. `send()` hits the
  chat completions endpoint with streaming. `suspend()`/`resume()` serialize
  the full conversation history as the opaque state blob (no server-side
  session).

---

## 5. Session Model

Sessions are the lowest layer. A session is a 1:1 wrapper around a single
provider instance for a single agent. Sessions handle provider lifecycle and
context persistence only — they know nothing about agent trees or messaging.

### Data Model

```python
import enum
from dataclasses import dataclass, field
from uuid import UUID, uuid4

class SessionState(enum.Enum):
    CREATED = "created"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"

@dataclass
class Session:
    id: UUID = field(default_factory=uuid4)
    state: SessionState = SessionState.CREATED
    provider_name: str = "cursor-agent"
    created_at: str = ""           # ISO 8601.
    suspended_at: str | None = None
    provider_state: bytes = b""    # Opaque blob from provider.suspend().
```

### State Machine

```
CREATED → ACTIVE → SUSPENDED → ACTIVE  (cycle)
                 → TERMINATED
           ACTIVE → TERMINATED
```

Transitions are validated; invalid transitions raise `SessionStateError`.

### Persistence

Each session persists to `~/.substrat/sessions/<uuid>/state.json` as JSON.
`provider_state` is base64-encoded in the JSON file. On daemon startup, all
session directories are scanned and sessions in `ACTIVE` state are moved to
`SUSPENDED` (since the daemon was not running).

### Multiplexing

There is a limited number of "active slots" (configurable, default 4).
Slots represent concurrently running provider instances, keyed by session ID.
When a new agent needs an active provider and all slots are full, the
least-recently-used session is suspended to free a slot.

```python
class SessionMultiplexer:
    def __init__(self, max_slots: int = 4) -> None:
        self._slots: dict[UUID, AgentProvider] = {}   # Keyed by session_id.
        self._lru: list[UUID] = []

    async def acquire(self, session_id: UUID) -> AgentProvider:
        """Get or create a live provider for this session."""
        ...

    async def release(self, session_id: UUID) -> None:
        """Suspend the session's provider and free the slot."""
        ...
```

---

## 6. Agent Hierarchy

The agent hierarchy sits above the session layer. It owns the tree structure,
parent-child relationships, and the mapping from agents to their sessions.

### Tree Structure

```python
@dataclass
class AgentNode:
    id: UUID = field(default_factory=uuid4)
    name: str = ""                          # Human-readable label.
    session_id: UUID = ...                  # 1:1 backing session (provider wrapper).
    parent_id: UUID | None = None           # None for root agents.
    children: list[UUID] = field(default_factory=list)
    instructions: str = ""                  # System prompt / custom instructions.
    role: str = "worker"                    # "manager" | "worker" | "reviewer".
    workspace_id: UUID | None = None        # Assigned workspace.
    state: str = "idle"                     # "idle" | "busy" | "waiting" | "terminated".
```

Every agent has exactly one `Session` (§5) backing it. The agent layer manages
hierarchy and messaging; the session layer manages provider lifecycle. Root
agents (`parent_id is None`) are the entry points for user interaction. There
may be multiple root agents in the system.

The daemon maintains `AgentTree`:

```python
class AgentTree:
    def __init__(self) -> None:
        self._nodes: dict[UUID, AgentNode] = {}

    def add(self, node: AgentNode) -> None: ...
    def remove(self, agent_id: UUID) -> None: ...
    def children(self, agent_id: UUID) -> list[AgentNode]: ...
    def parent(self, agent_id: UUID) -> AgentNode | None: ...
    def subtree(self, agent_id: UUID) -> list[AgentNode]: ...
    def roots(self) -> list[AgentNode]: ...
    def team(self, agent_id: UUID) -> list[AgentNode]:
        """Siblings: all children of this agent's parent, excluding self."""
        ...
```

### Spawning Subagents

An agent requests subagent creation via a tool call (exposed to the LLM as a
function). The daemon:
1. Creates a new `AgentNode` as a child of the requesting agent.
2. Creates or assigns a workspace (possibly a subdirectory of the parent's
   workspace).
3. Creates a new `Session` for the child and acquires a provider slot via the
   multiplexer.
4. Starts the provider with the child's instructions.

### Team Semantics

A "team" is the set of children of a single parent agent. The parent is the
implicit manager. Teams are not a separate data structure — they emerge from the
tree. Manager/worker/reviewer roles are advisory labels that the parent sets on
spawn; they influence the parent's orchestration strategy but not the system's
message routing.

---

## 7. Communication Protocol

### Message Envelope

```python
@dataclass
class MessageEnvelope:
    id: UUID = field(default_factory=uuid4)
    timestamp: str = ""               # ISO 8601.
    sender: UUID = ...                # Agent UUID, or SYSTEM / USER sentinel.
    recipient: UUID | None = None     # None for broadcasts.
    reply_to: UUID | None = None      # Links to id of the message being replied to.
    kind: str = "request"             # "request" | "response" | "notification" | "multicast".
    payload: str = ""                 # Free-form text body (LLM-native).
    metadata: dict[str, str] = field(default_factory=dict)
    # Known metadata keys:
    #   "timeout": "30" — sync request timeout in seconds.
    #   "priority": "high" | "normal" — advisory, for inbox ordering.
```

JSON serialization example:

```json
{
  "id": "a1b2c3d4-...",
  "timestamp": "2026-02-26T12:00:00Z",
  "sender": "aaaa-...",
  "recipient": "bbbb-...",
  "reply_to": null,
  "kind": "request",
  "payload": "Please review the changes in src/auth.py and check for SQL injection.",
  "metadata": {"priority": "high"}
}
```

### Routing Patterns

**Synchronous request-response.** Sender emits `kind=request`. Daemon delivers
it to recipient's inbox. Sender is blocked (its provider is not polled for new
output) until a `kind=response` with matching `reply_to` arrives. Timeout
configurable per-message (default: none, waits indefinitely).

**Asynchronous notification.** Sender emits `kind=notification`. Delivered to
recipient's inbox. Sender continues immediately. Recipient processes it when it
next checks its inbox.

**Multicast.** Sender emits `kind=multicast` with `recipient=None`. The daemon
resolves the sender's team (siblings sharing the same parent) and fans out
individual copies to each sibling. Each recipient replies independently. The
sender collects responses as they arrive (async aggregation).

### Routing Rules

Agents are restricted to **one-hop** communication:
- **Up**: direct parent only (not grandparent).
- **Down**: direct children only (not grandchildren).
- **Horizontal**: siblings only (children of the same parent, i.e. the team).

The daemon enforces these constraints at routing time. If agent A tries to
message agent B, the router checks that B is A's parent, A's child, or A's
sibling. All other routes are rejected with a routing error.

### Message Delivery

Each agent has an inbox (async queue). The daemon's message router:
1. Validates sender/recipient exist in the agent tree.
2. Validates one-hop routing rules (see above).
3. Enqueues into recipient's inbox.
4. If synchronous, registers a pending-reply record and pauses the sender.

### Exposing Messaging to LLM Agents

Messaging capabilities are surfaced as tool calls available to the LLM:

- `send_message(recipient_name, text, sync=True)` — send to a specific agent.
- `broadcast(text)` — multicast to the team.
- `check_inbox()` — retrieve pending async messages.
- `spawn_agent(name, instructions, role, workspace_subdir=None)` — create a
  subagent. If `workspace_subdir` is set, that subdirectory of the parent's
  workspace becomes the child's workspace root.
- `inspect_agent(name)` — view a subordinate's recent activity.
- `read_file(path)` / `write_file(path, content)` — access files in the
  agent's workspace for long-term context (notes, todos, scratchpads).

The daemon intercepts these tool calls from the provider's output stream,
executes them, and injects the result back into the provider's input.

---

## 8. Workspace Model

### Workspace Spec

```python
@dataclass
class WorkspaceSpec:
    id: UUID = field(default_factory=uuid4)
    parent_id: UUID | None = None          # For hierarchical nesting.
    root_path: Path = ...                   # Absolute path on host.
    network_access: bool = False
    symlinks: list[SymlinkSpec] = field(default_factory=list)

@dataclass
class SymlinkSpec:
    host_path: Path          # Source on the host or parent workspace.
    mount_path: Path         # Target inside the workspace (relative).
    mode: str = "ro"         # "ro" | "rw".
```

### bwrap Invocation

`bwrap.py` builds the `bwrap` command line from a `WorkspaceSpec`:

```python
def build_bwrap_argv(spec: WorkspaceSpec) -> list[str]:
    argv = ["bwrap", "--die-with-parent"]
    # Base filesystem (minimal /usr, /lib, /bin from host, read-only).
    argv += ["--ro-bind", "/usr", "/usr"]
    argv += ["--ro-bind", "/lib", "/lib"]
    argv += ["--ro-bind", "/bin", "/bin"]
    argv += ["--ro-bind", "/lib64", "/lib64"]
    # Workspace root as writable.
    argv += ["--bind", str(spec.root_path), "/workspace"]
    # Symlinks.
    for s in spec.symlinks:
        flag = "--bind" if s.mode == "rw" else "--ro-bind"
        argv += [flag, str(s.host_path), f"/workspace/{s.mount_path}"]
    # Network.
    if not spec.network_access:
        argv += ["--unshare-net"]
    # Proc/dev.
    argv += ["--proc", "/proc", "--dev", "/dev"]
    return argv
```

Provider subprocesses are launched inside bwrap. The provider's `start()` method
receives the bwrap prefix and prepends it to the actual command:

```
bwrap <flags> -- cursor-agent --session-dir /workspace/.session ...
```

### Hierarchical Nesting

A parent workspace at `/workspace/` can designate `/workspace/team-a/` as a
child workspace. The child workspace gets its own `WorkspaceSpec` with
`parent_id` set. The child's bwrap binds only the subdirectory as its root:

```
parent workspace: ~/.substrat/workspaces/<parent-uuid>/
child workspace:  ~/.substrat/workspaces/<parent-uuid>/team-a/
                  (bwrap mounts this as /workspace inside the child sandbox)
```

Intra-workspace symlinks (parent shares a file with child) are modeled as
additional `SymlinkSpec` entries on the child's spec, with `host_path` pointing
into the parent's directory.

### Multi-Agent Workspace Sharing

Multiple agents can reference the same `workspace_id`. Each agent may have
different permissions (e.g., one agent gets RW to `src/`, another gets RO). This
is implemented by giving each agent its own bwrap invocation with different bind
flags for the same underlying directories.

---

## 9. Logging

Logging spans two layers. The session layer logs provider-level events
(start/stop/suspend/resume). The agent layer logs hierarchy and messaging
events. Both write to a shared per-agent log directory (since session and agent
are 1:1).

### Per-Agent Log Directory

```
~/.substrat/agents/<uuid>/logs/
├── events.jsonl       # Machine-readable structured log.
└── transcript.txt     # Human-readable conversation transcript.
```

### JSONL Schema

Each line in `events.jsonl` is a self-contained JSON object:

```json
{
  "ts": "2026-02-26T12:00:00.123Z",
  "event": "provider.started",
  "agent_id": "...",
  "data": { "provider": "cursor-agent" }
}
```

Session-layer event types (provider lifecycle):
- `provider.started`, `provider.stopped`, `provider.suspended`, `provider.resumed`, `provider.error`.

Agent-layer event types (hierarchy and messaging):
- `agent.spawned`, `agent.terminated`.
- `message.sent`, `message.delivered`, `message.response`.
- `workspace.created`, `workspace.deleted`.
- `tool_call.invoked`, `tool_call.result`.

### Plaintext Transcript

`transcript.txt` is a human-friendly log of the conversation:

```
[12:00:00] USER → agent-alpha: Please review src/auth.py
[12:00:05] agent-alpha → agent-beta: Check for SQL injection in src/auth.py
[12:00:12] agent-beta → agent-alpha: Found one issue on line 42...
[12:00:15] agent-alpha → USER: Review complete. One SQL injection found...
```

### Implementation

Both writers share a common interface:

```python
class LogWriter(Protocol):
    async def write(self, event: dict) -> None: ...
    async def flush(self) -> None: ...
    async def close(self) -> None: ...
```

`JsonlWriter` appends JSON + newline. `PlaintextWriter` formats a readable line
from the event dict's key fields. Both are instantiated per-session and stored
in the session's log directory.

Log rotation is out of scope for v1. Sessions are expected to be finite.

---

## 10. Testing Strategy

### Principles

- **Strict mypy** (`--strict`) across the entire codebase.
- **pytest** as the test runner. No unittest subclasses.
- Tests live in `tests/unit/` and `tests/integration/`.

### Unit Tests

Unit tests cover pure logic with no I/O:
- `test_session_model.py` — state machine transitions, valid/invalid.
- `test_agent_tree.py` — add/remove nodes, parent/children/team queries.
- `test_messaging.py` — envelope construction, routing validation,
  serialization round-trip.
- `test_workspace_model.py` — spec construction, symlink composition.
- `test_bwrap.py` — bwrap argv generation from spec (no actual bwrap calls).
- `test_provider_base.py` — ensure protocol compliance via mock providers.

### Integration Tests

Integration tests use real I/O but mock LLM providers:

- `test_daemon_lifecycle.py` — start daemon, verify socket exists, stop
  daemon, verify cleanup.
- `test_cli_daemon.py` — start daemon, run CLI commands, verify responses.
- `test_session_persistence.py` — create session, suspend, restart daemon,
  resume, verify state.

### Mocking Providers

A `MockProvider` implements `AgentProvider` with canned responses:

```python
class MockProvider:
    name = "mock"

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)

    async def start(self, session_id: UUID, instructions: str) -> None:
        pass

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        response = next(self._responses)
        yield response

    async def suspend(self) -> bytes:
        return b""

    async def resume(self, state: bytes) -> None:
        pass

    async def stop(self) -> None:
        pass
```

This keeps integration tests fast and deterministic. Real provider tests
(actually calling cursor-agent or an API) are gated behind a `--run-e2e` flag.

### CI

- `mypy --strict src/ tests/` must pass.
- `pytest tests/unit/` must pass.
- `pytest tests/integration/` must pass.
- e2e tests are manual or triggered by explicit flag.

---

## Open Questions

Items deferred to later design iterations:

- **Configuration format.** TOML? YAML? CLI flags only? TBD.
- **Authentication/authorization.** Daemon currently trusts any local socket
  connection. Multi-user scenarios not addressed.
- **Agent memory/tools beyond files.** Basic file read/write is exposed as tool
  calls (§7). The high-level design mentions "other tools TBD" for long-term
  context. Possible extensions: structured todo lists, vector store, knowledge
  base. The convention for workspace-local context files (naming, format) is
  not yet defined.
- **Hot-reload of providers.** Can we add a new provider without restarting the
  daemon?
- **Resource limits.** CPU/memory caps per workspace, token budgets per session.
- **Streaming UX.** How does `agent attach` handle interleaved output from
  multiple agents in the tree?
