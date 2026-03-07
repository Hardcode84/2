# Review Pipeline

Automated review loop between a worker agent and a set of critic agents,
orchestrated by a scripted (non-LLM) provider. The system, not an LLM,
handles routing, gating, and synthesis.

## Problem

LLMs cannot reliably execute multi-step tool choreography. Asking a
project agent to inspect diffs, route to critics, collect feedback, and
relay to workers fails because the agent drops steps. The orchestration
must be deterministic.

## Architecture

```
project-A (CLI-created, LLM, persistent)
└── pipeline (scripted provider, deterministic)
    ├── worker (LLM, does coding)
    ├── critic-style (LLM, persistent)
    └── critic-correctness (LLM, persistent)
```

The pipeline owns the worker and critics. It is the single entry point
for tasks — external messages go to the pipeline, which controls the
worker's turn cycle. This gives the pipeline `gate`, `permit_turn`,
and `set_agent_metadata` authority over all its children.

## New tools

### Gate / ungate

Orthogonal to messaging and subscriptions. Controls wake eligibility.
Parent-only authority — only the parent can gate/ungate a child.

```
gate(agent_name)    — prevent agent from being woken, messages
                      accumulate in inbox but don't trigger wake
ungate(agent_name)  — allow agent to be woken normally
```

Implementation: `gated` flag on AgentNode (attribute the orchestrator
reads/writes — the node itself is a dumb state machine). Checked in
`_process_wake` before attempting to start a turn.

### Permit turn (atomic peek)

Atomically allows exactly one turn, then re-gates:
1. Set `_permit_once` flag on the node.
2. `_process_wake` sees gated + `_permit_once` → allows wake, clears
   the flag.
3. Orchestrator re-gates the agent in `_process_wake` immediately
   after calling `begin_turn()`, before the turn executes.

```
permit_turn(agent_name)  — allow exactly one turn, auto-re-gate
```

The gate goes back up when the agent enters BUSY, not when it exits.
Zero race window. Re-gate logic lives in the orchestrator (at the
`_process_wake` call site), not inside `AgentNode.begin_turn()` — the
node stays a pure state machine.

### Subscribe

Notify an agent when another agent transitions between states.
Orthogonal to gating — subscribe delivers notifications, gate controls
wake eligibility.

```
subscribe(agent_name, transition, once=false)
  → {"subscription_id": "...", "status": "active"}

unsubscribe(subscription_id)
  → {"status": "removed"}
```

`transition` is a string: `"busy->idle"`, `"*->terminated"`, etc.
Format is ASCII `->` everywhere (in tool params and delivered messages).

When the transition fires, a system message is delivered to the
subscriber's inbox:

```
[state] worker: busy -> idle
```

Persistent subscriptions survive across turns. One-shot subscriptions
(`once=true`) auto-remove after firing.

Visibility: parent can subscribe to children's transitions. Siblings
can subscribe to each other (one-hop routing applies).

Durability: subscriptions are persisted as part of agent state.
Subscription delivery is logged as `message.enqueued` in the
subscriber's event log (same as any other message). On crash recovery,
if a notification was enqueued but not yet processed, the subscriber
re-processes it on wake — same as any inbox message.

## Scripted provider

New provider type. Same protocol as LLM providers (`create`, `send`,
`suspend`, `stop`) but `send()` calls a Python function instead of
querying a model.

### Interface

```python
ScriptFn = Callable[[str, ToolHandler, Path | None], Awaitable[str]]


class ScriptedProvider:
    """Provider that runs deterministic Python functions."""

    name = "scripted"

    def __init__(self) -> None:
        self._registry: dict[str, ScriptFn] = {}
        self._handler_resolver: HandlerResolver | None = None

    def register(self, name: str, fn: ScriptFn) -> None:
        self._registry[name] = fn

    def set_handler_resolver(self, resolver: HandlerResolver) -> None:
        """Set by daemon after orchestrator is created."""
        self._handler_resolver = resolver

    async def create(
        self,
        model: str | None,
        system_prompt: str,
        log: EventLog | None = None,
        *,
        workspace: Path | None = None,
        wrap_command: CommandWrapper | None = None,
        agent_id: UUID | None = None,
        daemon_socket: str | None = None,
    ) -> ScriptedSession:
        if model not in self._registry:
            raise ValueError(f"unknown script: {model!r}")
        fn = self._registry[model]
        return ScriptedSession(
            fn,
            agent_id=agent_id,
            workspace=workspace,
            handler_resolver=self._handler_resolver,
        )

    def models(self) -> list[str]:
        return list(self._registry)

    async def restore(
        self, state: bytes, log: EventLog | None = None, **kwargs: object
    ) -> ScriptedSession:
        raise NotImplementedError("scripted sessions are stateless")
```

### Handler injection

The handler cannot be injected at `create()` time because the handler
is created *after* the session (see `_make_handler` in orchestrator).
Solution: lazy resolver.

```python
HandlerResolver = Callable[[UUID], tuple[ToolHandler, Path | None]]
```

The daemon wires the resolver at startup:

```python
scripted = ScriptedProvider()
scripted.register("review-pipeline", review_pipeline_fn)
scripted.set_handler_resolver(
    lambda agent_id: (orch.get_handler(agent_id), orch.get_workspace_path(agent_id))
)
```

`ScriptedSession.send()` resolves the handler lazily on first call:

```python
class ScriptedSession:
    def __init__(
        self,
        fn: ScriptFn,
        agent_id: UUID | None,
        workspace: Path | None,
        handler_resolver: HandlerResolver | None,
    ) -> None:
        self._fn = fn
        self._agent_id = agent_id
        self._workspace = workspace
        self._handler_resolver = handler_resolver

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        handler, ws = self._handler_resolver(self._agent_id)
        result = await self._fn(message, handler, ws or self._workspace)
        yield result

    async def suspend(self) -> bytes:
        return b""  # Stateless — state is in the WAL.

    async def stop(self) -> None:
        pass
```

### Registration

```python
provider = ScriptedProvider()
provider.register("review-pipeline", review_pipeline_fn)
```

CLI (requires Phase 5 `--parent` support):

```bash
substrat agent create pipeline \
    --provider scripted \
    --model review-pipeline \
    --parent project-A \
    --workspace project-A-ws
```

## Sender-side event logging

The existing event log records `message.enqueued` on the *recipient's*
log. For WAL recovery, the pipeline needs to know what it sent. New
event: `tool.send_message`, logged to the *caller's* session.

```python
# In ToolHandler.send_message(), after successful delivery:
self._log_event(
    "tool.send_message",
    {
        "recipient": recipient_name,
        "recipient_id": target_id.hex,
        "message_id": envelope.id.hex,
    },
)
```

Similarly for other tool calls that affect state:

```
tool.send_message   — logged on send
tool.gate           — logged on gate/ungate
tool.permit_turn    — logged on permit
tool.spawn_agent    — already logged as agent.created
```

This makes each agent's log self-contained for recovery.

## Pipeline state machine

The pipeline is a multi-turn deterministic state machine. Each turn
processes one event, takes one action, goes idle. The first message
the pipeline receives IS the task (no separate "bootstrap" literal).

```
INIT (receive first message — this is the task):
  → gate("worker")
  → subscribe("worker", "busy->idle")
  → send_message("worker", task)
  → permit_turn("worker")
  → state = WORKER_RUNNING

WORKER_RUNNING (receive "[state] worker: busy -> idle"):
  → run git diff to detect changes
  → if no changes or trivial:
      permit_turn("worker")
      (state stays WORKER_RUNNING)
  → else:
      route critics (constrained to existing children)
      send_message to each selected critic with diff
      state = WAITING_FOR_CRITICS(pending={...})

WAITING_FOR_CRITICS (receive "[state] critic-X: busy -> idle"):
  → check_inbox() for critic-X's feedback
  → pending.discard(critic-X)
  → if pending empty:
      synthesize feedback (concatenate)
      send_message("worker", feedback)
      permit_turn("worker")
      state = WORKER_RUNNING

WORKER_RUNNING (receive "[state] worker: * -> terminated"):
  → send_message("<parent-name>", "worker terminated: <result>")
  → state = DONE

DONE:
  → ignore further messages
```

The parent name is captured at init from the tree (the pipeline knows
its parent by construction — `list_children` on self or metadata).

### Routing rules

The pipeline decides which critics to wake based on the diff.
**Routing is constrained to critics that exist** — `list_children()`
filters the candidates.

```python
def route_critics(
    diff: str,
    available_critics: list[str],
    config: dict[str, Any],
) -> list[str]:
    """Return critic names to wake for this delta."""
    candidates = []
    files = parse_changed_files(diff)

    if not files:
        return []

    lines_changed = count_lines_changed(diff)
    threshold = config.get("style_threshold", 20)
    if lines_changed > threshold:
        candidates.append("critic-style")

    security_patterns = config.get("security_patterns", [])
    if any(matches_pattern(f, security_patterns) for f in files):
        candidates.append("critic-security")

    if any(not f.startswith("tests/") for f in files):
        candidates.append("critic-correctness")

    # Only return critics that actually exist.
    return [c for c in candidates if c in available_critics]
```

Thresholds and patterns come from a config file in the workspace
(`pipeline.toml`) or metadata on the pipeline agent. Defaults are
in the pipeline script (entry-point boundary, not library code).

## WAL-based crash recovery

The pipeline's state is a pure function of its event log. On crash
recovery, the scripted provider replays the log to reconstruct the
state machine.

### Recovery function

```python
def reconstruct_state(events: list[Event]) -> PipelineState:
    """Replay event log to recover pipeline state."""
    state = "init"
    pending_critics: set[str] = set()
    task: str = ""

    for ev in events:
        match ev.event:
            case "tool.send_message":
                recipient = ev.data["recipient"]
                if recipient == "worker":
                    # Sent task or feedback to worker.
                    state = "worker_running"
                    pending_critics.clear()
                elif recipient.startswith("critic"):
                    pending_critics.add(recipient)
                    state = "waiting_for_critics"
            case "message.delivered":
                # Pipeline consumed a message from its inbox.
                mid = ev.data["message_id"]
                # Cross-reference with enqueued events if needed.
            case "tool.permit_turn":
                # Worker was released for a turn.
                pass
            case "tool.gate":
                pass

    if pending_critics:
        state = "waiting_for_critics"
    return PipelineState(state=state, pending=pending_critics)
```

Every turn, the pipeline calls `reconstruct_state()` first. Idempotent
— replaying the same event twice doesn't corrupt state. The event log
is the single source of truth.

### What the WAL buys us

- No separate state file to sync.
- Crash at any point is safe — replay reconstructs exactly where we
  were.
- Debugging: `cat events.jsonl | jq` shows the full history.
- Each agent's log is self-contained (with sender-side logging).

## Critic agents

Persistent LLM agents. Maintain context across review rounds via
NOTES.md in their workspace.

### Lifecycle

- Created externally (CLI/init script), never by the pipeline.
- Woken by pipeline with a diff.
- Review the code against accumulated knowledge.
- Message the pipeline with findings (pipeline is their parent).
- Go idle. Context persists for next round.
- Never call complete(). Persistent across features.

### Context management

After N rounds, critic context is large. Mitigations:
- NOTES.md: critics write per-round summaries to disk.
- Compaction: context window compresses automatically, notes survive.
- Reset: if quality degrades, terminate and respawn with NOTES.md
  preserved in the workspace.

## External setup

Everything is created by CLI/scripts. The pipeline receives its first
message (the task) and bootstraps from there. No agent creates other
agents at setup time.

**Prerequisite:** `--parent` flag on `substrat agent create` (Phase 5).
Until implemented, agents can only be created as roots. The init script
would need to use a different mechanism (e.g. direct RPC, or have the
project agent spawn children).

```bash
# Daemon.
substrat daemon start --max-slots 6

# Root + project.
./templates/scripts/init-root.sh root
./templates/scripts/init-project.sh root wave ~/iree/wave

# Review pipeline (scripted). Parent is project agent.
substrat agent create pipeline \
    --parent wave \
    --provider scripted --model review-pipeline \
    --workspace wave-ws

# Worker. Parent is pipeline.
substrat agent create worker \
    --parent pipeline \
    --instructions "You are a worker. Make small atomic changes, commit each one. The repo is at /repo. When done, call complete(result) with a summary." \
    --workspace wave-ws

# Critics. Parent is pipeline.
substrat agent create critic-style \
    --parent pipeline \
    --instructions "You review diffs for style issues. Be specific: file, line, problem. Keep NOTES.md with patterns across rounds. Message your parent with findings." \
    --workspace wave-ws

substrat agent create critic-correctness \
    --parent pipeline \
    --instructions "You review diffs for correctness: edge cases, error handling, logic errors. Keep NOTES.md. Message your parent with findings." \
    --workspace wave-ws

# Send the task — this IS the bootstrap.
substrat agent send pipeline "Check README for typos and fix them"
```

## Error handling

| Scenario | Behavior |
|----------|----------|
| `gate("nonexistent")` | `tool_error("agent not found: nonexistent")` |
| `permit_turn("worker")` when worker is BUSY | `tool_error("agent is not idle")` |
| `permit_turn("worker")` when worker is not gated | No-op, runs normally |
| Scripted function raises | Turn fails, agent goes IDLE, error logged. Parent notified via error message (same as LLM turn failure). |
| Critic hangs (no response) | Phase 6: timeout. Until then, pipeline blocks. Operator can inspect via event logs. |
| `git diff` fails | Pipeline treats as "no changes", permits next turn, logs warning. |
| Unknown script name in `--model` | `ValueError` at session creation, surfaced as RPC error. |

## Implementation plan

### Phase 1: Gate / ungate / permit_turn tools

New tools in `tools.py`. `gated` and `_permit_once` flags on AgentNode
(orchestrator-managed attributes). `_process_wake` checks gate, handles
permit_once, re-gates after `begin_turn()`.

Scope: ~60 lines in tools.py, ~20 lines in orchestrator.

### Phase 2: Subscribe tool

Subscription registry in orchestrator. Keyed by (agent_id, transition).
State transition notifications delivered as system messages. Persistent
and one-shot modes. Persisted in agent state for crash recovery.

Scope: ~80 lines (registry + delivery hook in state transitions).

### Phase 3: Sender-side event logging

Add `tool.send_message` event to ToolHandler. Logged to caller's
session on successful send. Similarly for `tool.gate`, `tool.permit_turn`.

Scope: ~20 lines in tools.py.

### Phase 4: Scripted provider

New provider in `src/substrat/providers/scripted.py`. Full provider
protocol. Lazy handler resolver wired at daemon startup.

Scope: ~100 lines, provider + session + resolver.

### Phase 5: CLI `--parent` flag

Extend `substrat agent create` to accept `--parent`. RPC handler
creates non-root agents via `spawn_agent` internally.

Scope: ~30 lines in CLI + daemon.

### Phase 6: Review pipeline script

The pipeline function itself. State machine, routing rules,
fan-out/fan-in, WAL recovery. First version: simple routing,
concatenated critic feedback.

Scope: ~150 lines.

### Phase 7: Init scripts

`init-pipeline.sh` — creates pipeline, worker, critics, sends task.

### Phase 8: Polish

- Configurable routing rules (workspace config file).
- Timeout on critic responses (pipeline auto-releases after N seconds).
- Synthesis agent (optional, for 3+ critics).
- `substrat pipeline status` CLI command.
- Commit SHA in subscription notifications.
