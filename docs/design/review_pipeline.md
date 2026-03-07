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

```
gate(agent_name)    — prevent agent from being woken, messages
                      accumulate in inbox but don't trigger wake
ungate(agent_name)  — allow agent to be woken normally
```

Implementation: gated flag on the agent node, checked in
`_process_wake` before attempting to start a turn. Parent-only
authority — only the parent can gate/ungate a child.

### Permit turn (atomic peek)

Sugar for the gate/ungate/turn/gate cycle. Atomically:
1. Ungate the agent.
2. Drain inbox, start turn (agent goes BUSY).
3. Re-gate immediately (before the turn completes).

```
permit_turn(agent_name)  — allow exactly one turn, auto-re-gate
```

The gate goes back up when the agent enters BUSY, not when it exits.
Zero race window — the agent is gated again before it finishes. After
the turn completes (BUSY → IDLE), the agent stays gated until the
next `permit_turn`.

Implementation: a `_permit_once` flag on the node. `_process_wake`
checks: if gated but `_permit_once` is set, allow wake and clear the
flag. Re-gate happens in `begin_turn()`.

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

When the transition fires, a system message is delivered to the
subscriber's inbox:

```
[state] worker: busy → idle
```

Persistent subscriptions survive across turns. One-shot subscriptions
(`once=true`) auto-remove after firing.

Visibility: parent can subscribe to children's transitions. Siblings
can subscribe to each other (one-hop routing applies).

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

    def register(self, name: str, fn: ScriptFn) -> None:
        self._registry[name] = fn

    async def create(
        self,
        model: str | None,
        system_prompt: str,
        **kwargs: object,
    ) -> ScriptedSession:
        fn = self._registry[model]
        return ScriptedSession(fn)


class ScriptedSession:
    """Session backed by a Python callable."""

    def __init__(self, fn: ScriptFn) -> None:
        self._fn = fn

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        result = await self._fn(message)
        yield result

    async def suspend(self) -> bytes:
        return b""  # Stateless — state is in the WAL.

    async def stop(self) -> None:
        pass
```

### Tool access

LLM providers call tools through MCP. Scripted providers call the
ToolHandler directly as Python:

```python
handler.send_message("worker", feedback)
handler.gate("worker")
handler.permit_turn("worker")
handler.subscribe("worker", "busy->idle")
handler.check_inbox()
```

The orchestrator injects the handler and workspace path when creating
the session. Deferred work (spawns) drains normally after the turn.

### Registration

```python
provider = ScriptedProvider()
provider.register("review-pipeline", review_pipeline_fn)
```

CLI:

```bash
substrat agent create pipeline \
    --provider scripted \
    --model review-pipeline \
    --parent project-A \
    --workspace project-A-ws
```

## Pipeline state machine

The pipeline is a multi-turn deterministic state machine. Each turn
processes one event, takes one action, goes idle.

```
BOOTSTRAP (receive "bootstrap" message):
  → gate("worker")
  → subscribe("worker", "busy->idle")
  → subscribe("critic-style", "busy->idle")
  → subscribe("critic-correctness", "busy->idle")
  → send_message("worker", initial_task)
  → permit_turn("worker")
  → state = WORKER_RUNNING

WORKER_RUNNING (receive "[state] worker: busy → idle"):
  → run git diff to detect changes
  → if no changes or trivial:
      permit_turn("worker")
      (state stays WORKER_RUNNING)
  → else:
      send_message("critic-style", diff)
      send_message("critic-correctness", diff)
      state = WAITING_FOR_CRITICS(pending={"critic-style", "critic-correctness"})

WAITING_FOR_CRITICS (receive "[state] critic-X: busy → idle"):
  → check_inbox() for critic-X's feedback
  → pending.remove(critic-X)
  → if pending empty:
      synthesize feedback
      send_message("worker", feedback)
      permit_turn("worker")
      state = WORKER_RUNNING

WORKER_RUNNING (receive "[state] worker: * → terminated"):
  → send_message(parent, "worker terminated: <result>")
  → state = DONE
```

### Routing rules

The pipeline decides which critics to wake based on the diff:

```python
def route_critics(diff: str, config: PipelineConfig) -> list[str]:
    """Return critic names to wake for this delta."""
    critics = []
    files = parse_changed_files(diff)

    if not files:
        return []

    lines_changed = count_lines_changed(diff)
    if lines_changed > 20:
        critics.append("critic-style")

    if any(is_security_sensitive(f) for f in files):
        critics.append("critic-security")

    if any(not f.startswith("tests/") for f in files):
        critics.append("critic-correctness")

    return critics
```

Rules are configurable per-project via a config file in the workspace
or metadata on the pipeline agent.

## WAL-based crash recovery

No separate state file. The pipeline's state is a pure function of its
event log (`events.jsonl`). On crash recovery, the scripted provider
replays the log to reconstruct the state machine.

### Recovery function

```python
def reconstruct_state(events: list[Event]) -> PipelineState:
    """Replay event log to recover pipeline state."""
    state = PipelineState.IDLE
    pending_critics: set[str] = set()
    last_action: str = ""

    for ev in events:
        match ev.event:
            case "send.result":
                sent_to = ev.data.get("recipient", "")
                if "critic" in sent_to:
                    pending_critics.add(sent_to)
                    state = PipelineState.WAITING_FOR_CRITICS
                elif sent_to == "worker":
                    state = PipelineState.WORKER_RUNNING
                    pending_critics.clear()
            case "message.delivered":
                sender = ev.data.get("sender", "")
                if sender in pending_critics:
                    pending_critics.discard(sender)
                    if not pending_critics:
                        state = PipelineState.SYNTHESIZING

    if pending_critics:
        state = PipelineState.WAITING_FOR_CRITICS
    return PipelineState(state=state, pending=pending_critics)
```

Every turn, the pipeline calls `reconstruct_state()` first. This makes
it idempotent — replaying the same event twice doesn't corrupt state.
The event log is the single source of truth.

### What the WAL buys us

- No sync issues between state file and actual state.
- Crash at any point is safe — replay reconstructs exactly where we
  were.
- Debugging: `cat events.jsonl | jq` shows the full history.
- The logging infrastructure already exists.

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

Everything is created by CLI/scripts. The pipeline receives a bootstrap
message and initializes its state. No agent creates other agents at
setup time.

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
    --instructions "You are a worker. Make small atomic changes, commit each one. The repo is at /repo." \
    --workspace wave-ws

# Critics. Parent is pipeline.
substrat agent create critic-style \
    --parent pipeline \
    --instructions "You review diffs for style issues. Be specific: file, line, problem. Keep NOTES.md with patterns across rounds." \
    --workspace wave-ws

substrat agent create critic-correctness \
    --parent pipeline \
    --instructions "You review diffs for correctness: edge cases, error handling, logic errors. Keep NOTES.md." \
    --workspace wave-ws

# Bootstrap the pipeline with the task.
substrat agent send pipeline "Check README for typos and fix them"
```

The pipeline's bootstrap turn gates the worker, sets up subscriptions,
forwards the task, and permits the first turn. From there the cycle
is self-sustaining.

## Implementation plan

### Phase 1: Gate / ungate / permit_turn tools

New tools in `tools.py`. Gate flag on AgentNode. `_process_wake` checks
gate. `permit_turn` sets `_permit_once` flag, clears after BUSY entry.

Scope: ~60 lines in tools.py, ~15 lines in orchestrator, ~10 in node.

### Phase 2: Subscribe tool

Subscription registry in orchestrator. State transition notifications
delivered as system messages. Persistent and one-shot modes.

Scope: ~80 lines (registry + delivery hook in state transitions).

### Phase 3: Scripted provider

New provider in `src/substrat/providers/scripted.py`. Implements
provider protocol. `create()` takes callable via registry. `send()`
calls function with message + handler + workspace.

Scope: ~80 lines, provider + session + registry.

### Phase 4: Review pipeline script

The pipeline function itself. State machine, routing rules,
fan-out/fan-in, WAL recovery. First version: simple routing by file
count/paths, concatenate critic feedback without synthesis.

Scope: ~150 lines.

### Phase 5: Init scripts + CLI

`init-pipeline.sh` — creates pipeline, worker, critics, sends
bootstrap. CLI support for `--parent` flag on agent create.

### Phase 6: Polish

- Configurable routing rules (per-project config file).
- Timeout on critic responses (pipeline auto-releases after N seconds).
- Synthesis agent (optional, for 3+ critics).
- `substrat pipeline status` CLI command.
- Commit SHA in subscription notifications.
