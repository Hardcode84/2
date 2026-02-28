# Substrat — Implementation Overview

Map of the codebase. For high-level design see `substrat.md`. For component
details see the relevant doc in `docs/design/`. Design docs are the source of
truth for their respective components; this file is a summary that ties them
together.

---

## 1. Daemon Architecture

Long-running process that owns all state. The CLI is a thin client that talks
to the daemon over a Unix domain socket (`~/.substrat/daemon.sock`).

Single `asyncio` event loop drives UDS server, provider I/O, and message
routing. Blocking operations (bwrap, subprocess spawning) run in a
`ThreadPoolExecutor` via `loop.run_in_executor`.

### Filesystem

```
~/.substrat/
├── daemon.sock
├── daemon.pid
├── agents/
│   └── <uuid>/
│       ├── session.json    # Atomic snapshot of session state.
│       ├── events.jsonl    # Append-only event log (source of truth).
│       ├── transcript.txt  # Human-readable conversation log.
│       └── mcp.json        # Generated MCP config for this agent.
└── workspaces/
    └── <uuid>/
```

### Request Dispatch

CLI ↔ daemon wire format is newline-delimited JSON:

```json
{"id": "req-1", "method": "agent.create", "params": {...}}
{"id": "req-1", "result": {...}}
```

---

## 2. CLI Design

Typer-based. Most commands map 1:1 to daemon RPC methods.

```
substrat daemon start|stop|status

substrat agent create [--provider cursor-agent] [--name <name>]
substrat agent list
substrat agent attach <agent-id>       # Interactive REPL.
substrat agent inspect <agent-id>
substrat agent send <agent-id> <message>

substrat session list|suspend|resume|delete <uuid>

substrat workspace create|list|link|delete
```

`agent create` is the primary entry point — creates a root agent and its
backing session in one step. Session commands exist for low-level control.

`agent attach` opens a long-lived bidirectional stream (daemon pushes agent
output, CLI sends user input). Ctrl-C detaches without killing the agent.

---

## 3. Agent Provider Abstraction

See [design/provider.md](design/provider.md).

Provider-specific details: [design/providers/](design/providers/).

---

## 4. Session Model

See [design/session.md](design/session.md).

**Key files:**
- `session/model.py` — `Session` dataclass and state machine.
- `session/store.py` — `SessionStore`, atomic JSON persistence, startup recovery.
- `session/multiplexer.py` — `SessionMultiplexer`, fixed-slot LRU scheduling.
- `scheduler.py` — `TurnScheduler`, turn execution lifecycle and deferred work.

---

## 5. Agent Hierarchy

The agent hierarchy sits above the session layer. It owns the tree structure,
parent-child relationships, and the mapping from agents to their sessions.

```python
@dataclass
class AgentNode:
    session_id: UUID               # 1:1 backing session. Required positional.
    id: UUID = field(default_factory=uuid4)
    name: str = ""
    parent_id: UUID | None = None  # None for root agents.
    children: list[UUID] = field(default_factory=list)
    instructions: str = ""
    workspace_id: UUID | None = None
    state: AgentState = AgentState.IDLE  # IDLE | BUSY | WAITING | TERMINATED.
    created_at: str = field(default_factory=now_iso)
```

`AgentState` is an enum with a state-machine enforced via `transition()`.

`AgentTree` provides queries: `children()`, `parent()`, `team()` (siblings
excluding self), `roots()`, `subtree()`.

Teams are not a separate data structure — they emerge from the tree as
children of a single parent.

### Routing

One-hop only: parent, children, siblings. The daemon enforces this at routing
time. See [design/tool_integration.md](design/tool_integration.md) for the
tool surface agents use for messaging.

---

## 6. Communication Protocol

### Message Envelope

```python
SYSTEM: UUID = UUID(int=0)  # Daemon-originated messages.
USER: UUID = UUID(int=1)    # CLI/user-originated messages.

class MessageKind(enum.Enum):
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    MULTICAST = "multicast"

@dataclass
class MessageEnvelope:
    sender: UUID                          # Required positional. Agent UUID or sentinel.
    id: UUID = field(default_factory=uuid4)
    timestamp: str = field(default_factory=now_iso)  # ISO 8601.
    recipient: UUID | None = None         # None for broadcasts.
    reply_to: UUID | None = None
    kind: MessageKind = MessageKind.REQUEST
    payload: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
```

Routing validation and broadcast resolution live in `agent/router.py` — pure
functions, no mutable state. Sentinels (SYSTEM, USER) bypass one-hop checks
but recipients must exist in the tree.

### Delivery

Each agent has an inbox (async queue). The daemon's message router validates
sender/recipient exist in the tree, enforces one-hop routing, and enqueues.

All tool calls are non-blocking — see
[design/tool_integration.md](design/tool_integration.md) for the execution
model and full tool catalog. Synchronous messaging uses a two-turn pattern
orchestrated by the daemon; the agent process is never blocked waiting for
a reply.

Tool logic lives in `agent/tools.py` — pure operations on the tree and
inboxes, no MCP protocol or I/O. `ToolHandler` is instantiated per-agent
with deps injected; see the design doc for the full catalog and deferred
spawn pattern.

---

## 7. Workspace Model

```python
@dataclass
class WorkspaceSpec:
    id: UUID
    parent_id: UUID | None = None
    root_path: Path
    network_access: bool = False
    symlinks: list[SymlinkSpec] = field(default_factory=list)

@dataclass
class SymlinkSpec:
    host_path: Path
    mount_path: Path       # Relative, inside workspace.
    mode: str = "ro"       # "ro" | "rw".
```

`bwrap.py` builds the `bwrap` command line from a `WorkspaceSpec`. Hierarchical
nesting: a subdirectory of a parent workspace becomes the child's root.
Multiple agents can share a workspace with different permissions (different
bwrap bind flags for the same directories).

---

## 8. Logging

Per-agent, at `~/.substrat/agents/<uuid>/`:

- `events.jsonl` — append-only structured log. Source of truth for crash
  recovery. Every send/response, state transition, and message routing event.
  Fsynced per turn. See [design/crash_recovery.md](design/crash_recovery.md).
- `transcript.txt` — human-readable conversation log. Observability only.

All log entries are plain JSON (strings, numbers, bools, lists, dicts — no
opaque Python objects). This is a stability contract for replayability.

Log rotation is out of scope for v1.

---

## 9. Crash Recovery

See [design/crash_recovery.md](design/crash_recovery.md).

---

## 10. Testing Strategy

- **Strict mypy** (`--strict`) across the entire codebase.
- **pytest** as the test runner.
- `tests/unit/` — pure logic, no I/O.
- `tests/integration/` — real I/O, mock providers.
- e2e tests gated behind `--run-e2e` flag.
- Stress tests gated behind `@pytest.mark.stress`.

Mock provider implements `AgentProvider` + `ProviderSession` protocols with
canned responses for fast, deterministic integration tests.

---

## Open Questions

- **Configuration format.** TOML? YAML? CLI flags only?
- **Authentication.** Daemon trusts any local socket connection. Multi-user
  not addressed.
- **Resource limits.** CPU/memory per workspace, token budgets per session.
- **Streaming UX.** How `agent attach` handles interleaved output from
  multiple agents.
- **Sentinel-as-recipient.** Agents cannot currently route messages to
  SYSTEM/USER (they're not in the tree). The daemon will need to intercept
  these at the boundary layer. Decide whether `validate_route` should
  whitelist sentinel recipients or keep routing pure and handle it above.
- **Root-to-root routing.** Multiple root agents cannot communicate (no
  parent, so no siblings). Intentional for now — document or add a
  mechanism if multi-root topologies become real.
