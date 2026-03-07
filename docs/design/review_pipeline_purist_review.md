# Review Pipeline — Purist Critique

Architectural review from The Purist's perspective. Organized by severity.

---

## Critical

### 1. WAL recovery assumes events that do not exist

**Section:** "WAL-based crash recovery" → `reconstruct_state()`

The design's recovery function expects:

```python
case "send.result":
    sent_to = ev.data.get("recipient", "")
```

**Actual contract:** The event log (see `crash_recovery.md`, `tools.py`) uses:
- `message.enqueued` — logged to the **recipient's** session before `inbox.deliver()`
- `message.delivered` — logged after `inbox.collect()` drains

There is no `send.result` event on the **sender's** log. The `send.result` that exists in the codebase is from `@log_method` on the provider session's `send()` — it logs the turn result, not tool-call semantics. The pipeline's `events.jsonl` would have `turn.start`, `turn.complete`, and provider-decorator events, but nothing that records "pipeline sent to worker" on the pipeline's own log.

**Impact:** Recovery cannot reconstruct `WAITING_FOR_CRITICS` or `WORKER_RUNNING` from the pipeline's event log alone. The design would need either:
- A new log event (e.g. `tool.send_message`) fired to the **caller's** session when `send_message` succeeds, or
- Cross-session log reads (pipeline reads worker's/critics' logs to infer what was sent) — which couples session logs and violates the per-session WAL model.

**Recommendation:** Add explicit `tool.send_message` (or equivalent) logging to the caller's session in `ToolHandler.send_message()`, with `recipient` in `data`. Update the design to name the event and document the new log point.

---

### 2. Handler injection path is underspecified

**Section:** "Tool access" → "The orchestrator injects the handler and workspace path when creating the session."

**Actual flow:** The orchestrator does not create sessions directly. `scheduler.create_session()` calls `provider.create()`. The scheduler has no reference to the orchestrator. The handler is created in `_make_handler()` which runs **after** `tree.add()` — for root agents, the session is created **before** the handler exists.

**Chicken-egg:** Session is created → tree.add → handler created. So the scripted session cannot receive the handler at `create()` time.

**Missing from design:**
- How the handler reaches the scripted provider (resolver? two-phase wiring?)
- That the daemon must wire `ScriptedProvider` to the orchestrator after both exist (e.g. `providers["scripted"].set_handler_resolver(orch.get_handler)`)
- That the handler lookup must be lazy (at first `send()`), not at create

**Recommendation:** Document the injection path: resolver callable passed to `ScriptedProvider` at daemon init, invoked lazily when `send()` runs. Specify that the resolver returns `(ToolHandler, Path | None)` for a given `agent_id`.

---

### 3. Bootstrap vs. first-message semantics

**Section:** "Pipeline state machine" vs. "External setup"

State machine says:
```
BOOTSTRAP (receive "bootstrap" message):
  → gate("worker")
  ...
  → send_message("worker", initial_task)
```

External setup says:
```bash
substrat agent send pipeline "Check README for typos and fix them"
```

So the first message is the **task**, not the literal string `"bootstrap"`. The design does not specify whether:
- The pipeline treats the first message as bootstrap + task, or
- A separate `"bootstrap"` message is required before the task.

**Recommendation:** Clarify: "BOOTSTRAP: receive first message — treat as initial task. Gate worker, subscribe, forward task, permit first turn."

---

## Major

### 4. Routing rules reference non-existent critics

**Section:** "Routing rules" → `route_critics()`

```python
if any(is_security_sensitive(f) for f in files):
    critics.append("critic-security")
```

The setup creates `critic-style` and `critic-correctness` only. `critic-security` is never created. The design does not specify:
- That routing must be constrained to critics that exist in the tree, or
- That `send_message("critic-security", ...)` would fail (no such agent).

**Recommendation:** Either add `critic-security` to the setup, or document that routing rules must only return critic names that exist. Add a validation step: `route_critics()` filtered by `tree.children(pipeline_id)`.

---

### 5. Gate / permit_turn: re-gate placement

**Section:** "Permit turn" → "Re-gate happens in `begin_turn()`"

`AgentNode.begin_turn()` is a pure state transition (IDLE → BUSY). The design says re-gate happens "in begin_turn()". Two interpretations:

1. **Inside** `AgentNode.begin_turn()` — the node would mutate `gated` and `_permit_once`. That puts wake-eligibility logic into the node. The node currently "knows nothing about messages or routing" (`node.py` docstring). Gate is wake-eligibility — an orchestrator concern.

2. **At the call site** — the orchestrator re-gates immediately after calling `begin_turn()`. That keeps gate logic in the orchestrator.

**Recommendation:** Specify that re-gate happens in the **orchestrator** at the point it calls `begin_turn()` (or in `_process_wake` before/after the transition). Keep `AgentNode` as a dumb state machine; gate flags are attributes the orchestrator reads/writes.

---

### 6. Event log: per-session vs. pipeline-centric

**Section:** "WAL-based crash recovery"

The design says "the pipeline's state is a pure function of its event log (`events.jsonl`)". Per `crash_recovery.md`, each session has its own `events.jsonl` under `~/.substrat/agents/<session-uuid>/`. The pipeline is one agent = one session = one log.

`message.enqueued` is logged to the **recipient's** log. So when the pipeline sends to the worker, the event goes to the worker's log, not the pipeline's. The pipeline's log alone does not contain a record of outbound sends.

**Recommendation:** Either (a) add sender-side logging for `send_message` so the pipeline's log is self-contained, or (b) explicitly document that recovery requires reading child session logs (and the resulting cross-session dependency).

---

### 7. Recovery introduces `PipelineState.SYNTHESIZING`

**Section:** "Recovery function"

```python
if not pending_critics:
    state = PipelineState.SYNTHESIZING
```

The main state machine diagram does not include `SYNTHESIZING`. It has `WORKER_RUNNING`, `WAITING_FOR_CRITICS`, `DONE`. `SYNTHESIZING` is a transient moment between "last critic done" and "send to worker". The recovery logic uses it, but the state machine spec does not.

**Recommendation:** Add `SYNTHESIZING` to the state diagram, or fold it into `WORKER_RUNNING` (e.g. "synthesizing" as a sub-state or same-state action).

---

## Minor

### 8. `--parent` flag not yet implemented

**Section:** "External setup", "Phase 5"

The design assumes `substrat agent create pipeline --parent wave`. The current CLI (`cli/app.py`) has no `--parent` option. `agent.create` RPC creates root agents only. The implementation plan correctly marks this as Phase 5, but the setup examples use it as if it exists.

**Recommendation:** Add a note: "Requires Phase 5 (CLI `--parent` support). Until then, use init scripts that create via spawn or a different mechanism."

---

### 9. Policy defaults in pipeline logic

**Section:** "Routing rules"

`lines_changed > 20`, `is_security_sensitive()`, `not f.startswith("tests/")` are policy defaults. CLAUDE.md: "Policy defaults belong at the CLI/entry-point boundary, not in library constructors."

The pipeline script is application logic, not library code. The design says rules are "configurable per-project via a config file in the workspace or metadata on the pipeline agent." So defaults live in the pipeline, config overrides from workspace/metadata. That is acceptable — the pipeline is the entry point for this workflow.

**Recommendation:** No change. Just ensure the config file lives in the workspace (operator-controlled) and is not hardcoded in library constructors.

---

### 10. Scripted provider protocol alignment

**Section:** "Scripted provider" → `ScriptedProvider.create()`

The design's `create()` signature:

```python
async def create(
    self,
    model: str | None,
    system_prompt: str,
    **kwargs: object,
) -> ScriptedSession:
```

The `AgentProvider` protocol (`provider/base.py`) requires:

```python
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
) -> ProviderSession:
```

The scripted provider must accept `log`, `workspace`, `wrap_command`, `agent_id` via `**kwargs` to satisfy the protocol. The design omits these. The scripted session is stateless (`suspend` returns `b""`), so `log` is still needed for `turn.start` / `turn.complete` (scheduler writes those).

**Recommendation:** Document that the scripted provider implements the full protocol; `**kwargs` carries `log`, `workspace`, `agent_id`, etc. The pipeline function receives `workspace` from the injected context, not from the provider's `create` args directly.

---

### 11. `send_message(parent, ...)` — parent resolution

**Section:** "Pipeline state machine" → `WORKER_RUNNING (receive "[state] worker: * → terminated")`

The design says `send_message(parent, "worker terminated: <result>")`. `ToolHandler.send_message(recipient, text)` takes a **name**. The parent's name is set at creation (e.g. `"wave"`). The design uses `parent` as a placeholder.

**Recommendation:** Clarify: the pipeline must use the parent's actual name (e.g. `send_message("wave", ...)`). Consider a convenience: `send_to_parent(text)` if the pattern is common, or document that the pipeline captures the parent name from tree metadata at bootstrap.

---

## Summary Verdict

**The design is ambitious and mostly coherent, but it has critical gaps between the stated contract and the existing implementation.**

1. **WAL recovery is broken as specified** — the events `reconstruct_state` expects do not exist. This must be fixed before implementation.

2. **Handler injection is hand-wavy** — the design says "orchestrator injects" but the actual call chain (scheduler → provider) and handler creation order make that non-trivial. The wiring must be spelled out.

3. **Layer boundaries are mostly respected** — the scripted provider is a new provider type, not a new layer. The pipeline uses tools (handler) like any agent; the session remains a dumb pipe. The main risk is that the handler-resolver wiring could leak orchestrator concerns into the provider if done carelessly.

4. **No new layers** — good. Gate, permit_turn, subscribe extend the existing tree/orchestrator surface.

5. **Policy defaults** — correctly placed at the pipeline/workspace boundary.

**Recommendation:** Address the critical items (WAL events, handler injection, bootstrap semantics) before implementation. The major items (routing validation, gate placement, recovery cross-session reads) should be resolved in the design. The minor items are documentation polish.
