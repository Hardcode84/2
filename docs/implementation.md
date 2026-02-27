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

---

## 5. Agent Hierarchy

The agent hierarchy sits above the session layer. It owns the tree structure,
parent-child relationships, and the mapping from agents to their sessions.

```python
@dataclass
class AgentNode:
    id: UUID
    name: str = ""
    session_id: UUID = ...         # 1:1 backing session.
    parent_id: UUID | None = None  # None for root agents.
    children: list[UUID] = field(default_factory=list)
    instructions: str = ""
    role: str = "worker"           # "manager" | "worker" | "reviewer" (advisory).
    workspace_id: UUID | None = None
    state: str = "idle"            # "idle" | "busy" | "waiting" | "terminated".
```

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
@dataclass
class MessageEnvelope:
    id: UUID
    timestamp: str                    # ISO 8601.
    sender: UUID                      # Agent UUID, or SYSTEM/USER sentinel.
    recipient: UUID | None = None     # None for broadcasts.
    reply_to: UUID | None = None
    kind: str = "request"             # "request" | "response" | "notification" | "multicast".
    payload: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
```

### Delivery

Each agent has an inbox (async queue). The daemon's message router validates
sender/recipient exist in the tree, enforces one-hop routing, and enqueues.

All tool calls are non-blocking — see
[design/tool_integration.md](design/tool_integration.md) for the execution
model and full tool catalog. Synchronous messaging uses a two-turn pattern
orchestrated by the daemon; the agent process is never blocked waiting for
a reply.

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
