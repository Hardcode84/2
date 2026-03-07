# Review: review_pipeline.md

Four-persona parallel review. Reviewers had no shared context.

## Consensus findings

### WAL recovery is broken as specified

`reconstruct_state()` assumes `send.result` with a `recipient` field
and `message.delivered` with a `sender` field. Neither event exists.
`send_message` logs `message.enqueued` to the *recipient's* log, not
the pipeline's. The pipeline's own event log cannot reconstruct its
state.

Fix: log pipeline outbound actions to the pipeline's own session log.
Align `reconstruct_state` with the actual event schema.

### Bootstrap semantics are contradictory

The state machine expects `BOOTSTRAP (receive "bootstrap" message)`.
The setup example sends the task directly:

```bash
substrat agent send pipeline "Check README for typos and fix them"
```

`initial_task` is never defined. Unclear whether the first message
*is* the bootstrap or a separate literal `"bootstrap"` is required.

### `route_critics` references `critic-security`, which is never created

Routing can return `"critic-security"`, but setup only creates
`critic-style` and `critic-correctness`. Sending to a non-existent
agent will fail. Either create the agent or make routing conditional
on configured critics.

### ScriptFn signature vs. invocation mismatch

```python
ScriptFn = Callable[[str, ToolHandler, Path | None], Awaitable[str]]
```

Takes 3 args. `ScriptedSession.send()` calls `self._fn(message)` with
1 arg. Handler/workspace injection is described in prose but never
wired through `create()` or `send()`.

### `--parent` flag doesn't exist

The entire external setup relies on `substrat agent create --parent`,
which is not implemented. Phase 5 acknowledges this, but the setup
section is written as if it works.

## The Skeptic

Race conditions, silent failures, crash recovery gaps.

### Critical

**`permit_turn` durability gap.** Crash between setting `_permit_once`
and the worker's `begin_turn()` loses the flag with no WAL record.
Recovery cannot know a turn was permitted. The doc does not specify
whether `permit_turn` is synchronous or enqueues a wake, or how
`_permit_once` is cleared relative to `begin_turn()`.

**Subscription delivery is not crash-safe.** The doc does not say
whether subscription delivery is logged, whether a crash between
"critic goes idle" and "pipeline receives notification" can lose the
notification, or whether duplicate notifications are possible after
recovery.

**`reconstruct_state` uses `"critic" in sent_to`.** Substring check.
Brittle — matches any name containing "critic". Also assumes `sent_to`
exists, which it does not in the current event format.

### Major

**`ScriptedProvider.create()` raises `KeyError`.** No validation on
`model`. A typo in the CLI crashes the daemon.

**`check_inbox()` semantics unspecified.** The pipeline needs feedback
per-critic. The doc does not say whether `check_inbox` filters by
sender, whether other messages can be mixed in, or how ordering works.

**`git diff` is an external dependency.** No handling for: repo not
initialized, `git` missing, wrong working directory, staged vs.
unstaged changes. A failed diff treated as "no changes" causes a loop.

**Routing vs. bootstrap subscriptions.** Bootstrap subscribes to
`critic-style` and `critic-correctness`. If routing returns
`["critic-security"]` only, the pipeline never subscribes to it and
waits forever.

**`PipelineState.SYNTHESIZING` is never handled.** Recovery can set
this state, but the main state machine has no `SYNTHESIZING` transition
or handling. Pipeline gets stuck in an undefined state.

### Minor

**Handler injection for scripted provider.** The doc says the
orchestrator injects the handler when creating the session. The
`create()` signature and `send()` call path do not show how.

**Phase 6 timeout is unimplemented.** If a critic hangs, the pipeline
blocks indefinitely.

## The Nitpicker

Type mismatches, undocumented semantics, off-by-one logic.

### Critical

**ScriptFn / ScriptedSession mismatch.** ScriptFn expects
`(str, ToolHandler, Path | None)`. `send()` calls `self._fn(message)`
with one arg. Type error at runtime.

**`create()` bare `KeyError`.** `self._registry[model]` with no guard.
`model` can be `None`.

**Recovery events vs. actual schema.** `send.result` does not exist.
`message.delivered` logs `{"message_id": m.id.hex}`, not `sender`.
Recovery assumes fields that are never written.

**Recovery introduces states not in the main machine.** `IDLE` and
`SYNTHESIZING` appear in recovery but not in the state diagram.
`PipelineState(state=state, pending=pending_critics)` is a constructor
call for an undefined type.

### Major

**`pending.remove()` raises `KeyError`.** Should be `discard()`.
Duplicate or unexpected notifications would crash.

**`send_message(parent, ...)` — parent identity undefined.** The
pipeline needs the parent name. The doc does not say how it obtains it.

**Transition format inconsistency.** Subscribe uses ASCII `->`,
delivered messages use Unicode `→`. Parsing must handle both.

**Provider protocol mismatch.** `create()` omits `log`, `workspace`,
`agent_id`, etc. from the real `AgentProvider` protocol.

### Minor

**Undefined helpers.** `parse_changed_files`, `count_lines_changed`,
`is_security_sensitive` — no signatures, return types, or semantics.

**`PipelineConfig` undefined.** No fields or documentation.

**Dead variable.** `last_action: str = ""` is declared but never used.

**`lines_changed > 20` threshold.** Arbitrary, not configurable,
not justified.

**DONE state behavior.** Whether the pipeline ignores further messages
or can be restarted is not specified.

## The Purist

Layer boundaries, abstraction granularity, mixed concerns.

### Critical

**Handler injection crosses session/tree boundary.** The scheduler
creates sessions (session layer). The handler is a tree-layer concept.
The design says "the orchestrator injects the handler" but the
scheduler creates sessions, not the orchestrator, and the handler is
created after the session. Needs a concrete wiring story (resolver
callable, lazy lookup at first `send()`).

**Event log scope.** The pipeline's log alone cannot reconstruct state
because `message.enqueued` is written to the recipient's log. Either
add sender-side logging or document cross-session log reads.

### Major

**Re-gate placement.** Re-gate in `begin_turn()` mixes
wake-eligibility control into the node. Wake eligibility is an
orchestrator concern. The gate check belongs in `_process_wake` (which
is fine), but the re-gate side effect in `begin_turn()` leaks
orchestrator policy into the node.

**`SYNTHESIZING` state.** Used in recovery but not in the main state
machine. Unclear which layer owns the concept.

### Minor

**Scripted provider must satisfy full `AgentProvider` protocol.**
The design shows a minimal interface. The real protocol has more
parameters.

**No new layers (good).** Gate, permit_turn, subscribe extend the
existing tool surface. Scripted provider is a new provider type,
not a new layer.

**Policy defaults.** Routing thresholds and critic names are in the
pipeline script, not in library constructors. Correct placement.

## The Pragmatist

CLI ergonomics, operational visibility, bootstrap friction.

### Critical

**Bootstrap is impossible today.** No `--parent` on `agent create`.
The described setup cannot be executed. `_handle_agent_create` only
calls `create_root_agent`.

### Major

**No operational visibility when the pipeline stalls.** `substrat
pipeline status` is Phase 6. Until then, debugging requires reading
raw event logs. No way to see current state, pending critics, or
whether the pipeline is gated.

**Error messages are unspecified.** What does the user see when
`gate("nonexistent")` is called? When `permit_turn("worker")` targets
a BUSY agent? When the scripted function raises? Current tools return
`tool_error(str(exc))` but the design doesn't describe surfacing.

**Scripted provider registration is unspecified.** Where does
`provider.register("review-pipeline", review_pipeline_fn)` run?
How is the scripted provider added to `default_providers`?

### Minor

**Workspace scope ambiguity.** `--workspace wave-ws` — if multiple
workspaces share that name, resolution is ambiguous.

**Critic timeout deferred.** A hung critic blocks the pipeline forever.
No default or fallback.

**`check_inbox()` semantics.** Without sender filter, the pipeline may
consume messages from other sources before routing.

## Actions

| Priority | Action |
|----------|--------|
| P0 | Fix WAL recovery: log pipeline outbound actions to the pipeline's own session, align `reconstruct_state` with actual event schema |
| P0 | Resolve bootstrap semantics: define what triggers bootstrap vs. normal task message, define `initial_task` |
| P1 | Wire ScriptFn signature to match actual invocation (pass handler + workspace through `create` and `send`) |
| P1 | Implement `--parent` on `agent create` or acknowledge as blocker |
| P1 | Make routing conditional on which critics actually exist |
| P2 | Specify `permit_turn` and subscription durability/idempotency guarantees |
| P2 | Add basic pipeline status visibility before Phase 6 |
| P2 | Define error behavior for gate/permit_turn/subscribe edge cases |
| P3 | Standardize transition format (ASCII vs. Unicode) |
| P3 | Fix dead `last_action` variable, use `discard()` over `remove()` |
| P3 | Define `PipelineConfig`, `PipelineState` types |
