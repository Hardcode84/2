# Wake Management

How Substrat drives message-triggered turns without polling. An IDLE agent
that receives a message gets woken automatically — no external RPC needed.

---

## Problem

Without auto-wake, messages rot in inboxes. Agent A sends to agent B, B stays
IDLE. Someone has to call `agent.send` via RPC to drive B's next turn. This
breaks the sync-message flow described in [tool_integration.md](tool_integration.md)
and makes the spawn-then-send pattern (parent spawns child, sends it work in
the same turn) impossible without a second external push.

---

## Data Flow

```
ToolHandler._deliver(recipient_id, envelope)
  → inbox.deliver(envelope)
  → wake_callback(recipient_id)           # Sync, inline.
    → Orchestrator._notify_wake(agent_id)
      → asyncio.Queue.put_nowait(agent_id)
                              ┊
           ┌──────────────────┘
           ▼
      _wake_loop()                         # Background asyncio.Task.
        → await queue.get()
        → batch-drain (up to _WAKE_LIMIT)
        → deduplicate per agent
        → _process_wake(agent_id) for each
           → guard: in tree? IDLE? inbox non-empty?
           → begin_turn() (IDLE → BUSY)
           → _format_wake_prompt()
              → inbox.peek() (non-destructive)
              → format prompt string
           → try: _execute_turn(node, prompt)
                → scheduler.send_turn()
                → end_turn() (BUSY → IDLE)
                → _drain_deferred()
                → inbox.collect() + log message.delivered
             except:
                → end_turn() (BUSY → IDLE)
                → deliver ERROR to parent inbox
                → wake parent
                → (messages stay in child inbox)
```

The callback is synchronous — `_notify_wake` just does `put_nowait()`. No
awaiting, no blocking. The actual wake processing happens in the background
task, after the sender's turn continues or completes.

---

## Triggers

Three sources enqueue wake notifications.

### 1. Message delivery

`send_message` and `broadcast` both call `_deliver()`, which fires
`wake_callback(recipient_id)` after `inbox.deliver()`. The recipient gets
woken even if the sender is still mid-turn — processing is deferred to the
background loop.

### 2. Post-spawn

The parent's deferred spawn work runs in `_drain_deferred()`. After all
`do_spawn` coroutines execute, the orchestrator scans the parent's children:

```python
for child in self._tree.children(agent_id):
    inbox = self._inboxes.get(child.id)
    if inbox and child.state == AgentState.IDLE:
        self._notify_wake(child.id)
```

This handles the common pattern: parent calls `spawn_agent("worker", ...)`
and `send_message("worker", "go", sync=true)` in the same turn. The message
queues before the child session exists. After spawn creates the session, the
post-spawn scan finds the non-empty inbox and triggers a wake.

### 3. Recovery

At the end of `recover()`, all placed agents with non-empty inboxes get wake
notifications:

```python
for nid in placed:
    node = self._tree.get(nid)
    inbox = self._inboxes.get(nid)
    if inbox and node.state == AgentState.IDLE:
        self._notify_wake(nid)
```

Messages recovered from the event log (`message.enqueued` without matching
`message.delivered`) are re-injected into inboxes during recovery. This scan
ensures they don't stay stuck. See [crash_recovery.md](crash_recovery.md).

---

## Concurrency Model

### No mutexes

asyncio is single-threaded. Cooperative multitasking means code between
`await` points runs atomically. The wake loop exploits this:

1. `_process_wake` checks `node.state == AgentState.IDLE` — synchronous.
2. `node.begin_turn()` transitions to BUSY — synchronous.
3. First `await` is `scheduler.send_turn()` — by then, the agent is BUSY.

A concurrent RPC `agent.send` seeing the same agent will hit `begin_turn()`
and get `AgentStateError` because the agent is already BUSY. No lock needed.

### begin_turn as guard

`_process_wake` wraps `begin_turn()` in try/except `AgentStateError`:

```python
try:
    node.begin_turn()
except AgentStateError:
    return  # Someone else got there first.
```

This covers the theoretical race where the state check and the transition
straddle a yield point (they don't today, but the guard is defensive).

### Empty inbox after begin_turn

If `_format_wake_prompt()` returns empty (inbox drained by a concurrent
`check_inbox` tool call between enqueue and processing), the agent is
transitioned back to IDLE without executing a turn:

```python
if not prompt:
    node.end_turn()
    return
```

---

## _execute_turn: Shared Turn Execution

`_execute_turn(node, prompt)` is the common path for both external RPC turns
(`run_turn`) and internal wake turns. It takes an already-BUSY node.

```
run_turn(agent_id, prompt)      _process_wake(agent_id)
  → begin_turn()                  → begin_turn()
  → _execute_turn(node, prompt)   → _execute_turn(node, prompt)
```

The method handles:
1. `scheduler.send_turn()` — actual provider interaction.
2. `node.end_turn()` — BUSY → IDLE.
3. `_drain_deferred()` — deferred spawns, terminations, post-spawn wakes.
4. Error recovery — if send_turn raises, `end_turn()` is called if still BUSY.

---

## Prompt Format

`_format_wake_prompt()` reads the inbox via `peek()` and builds a prompt.
Drain (`collect()`) happens after the turn succeeds. See Wake Failure
Handling below.

**Single message:**
```
Message from alice:
Can you review the analysis?
```

**Multiple messages:**
```
1. From alice: Can you review the analysis?
2. From bob: Here are the updated numbers.
3. From system: Deadline extended to Friday.
```

Sender names resolve through `_sender_display_name()`: sentinel UUIDs
(SYSTEM, USER) get their symbolic names, tree nodes get their agent name,
unknown senders fall back to raw UUID string.

Each delivered message is logged as `message.delivered` with its envelope ID.

---

## Safety Limits

### Batch size cap

The wake loop drains the queue in batches of up to `_WAKE_LIMIT` (100).
If the limit is hit, a warning is logged:

```
wake limit hit (100), possible ping-pong
```

This prevents runaway A↔B reply loops from starving the event loop. Messages
beyond the limit stay in the queue for the next iteration — nothing is lost.

### Deduplication

Multiple wake notifications for the same agent within one batch are collapsed.
Only the first occurrence triggers `_process_wake`. This is cheap — a set
tracks seen agent IDs:

```python
seen: set[UUID] = set()
for aid in batch:
    if aid not in seen:
        seen.add(aid)
        await self._process_wake(aid)
```

Redundant wakes happen naturally: two messages from different senders to the
same recipient produce two `_notify_wake` calls.

### State guards

`_process_wake` skips the agent if any of:
- Agent not in tree (terminated between enqueue and processing).
- Agent not IDLE (already mid-turn from RPC or another wake).
- Inbox empty (pre-`begin_turn` check via `Inbox.__bool__`).
- No handler registered (session not ready — pending spawn).
- `begin_turn()` raises `AgentStateError` (defensive).
- Inbox empty after drain (post-`begin_turn` check via `collect()`).

No wake is "lost". Two mechanisms ensure pending messages get processed:

1. **Post-spawn wake.** `_drain_deferred` scans children for non-empty
   inboxes after creating their sessions. Catches messages queued before
   the child's session existed.
2. **Post-turn re-wake.** `_execute_turn` calls `_rewake_if_pending` after
   the turn ends and deferred work drains. Catches messages delivered
   mid-turn whose original wake was skipped because the agent was BUSY.

---

## Lifecycle

| Phase | Method | Where |
|-------|--------|-------|
| Start | `Orchestrator.start_wake_loop()` | Daemon `start()`, after `recover()`, before UDS server. |
| Run | `_wake_loop()` background task | Runs until cancelled. |
| Stop | `Orchestrator.stop_wake_loop()` | Daemon `stop()`, before closing server. |

`start_wake_loop()` is idempotent — calling it twice does nothing.
`stop_wake_loop()` cancels the task and suppresses `CancelledError`.

The wake loop must start after recovery (so recovered agents get woken)
and before the UDS server (so incoming messages from the first RPC turn
can trigger wakes).

---

## Interaction with complete()

`complete(result)` sends a RESPONSE to the parent and defers self-termination.
The RESPONSE delivery fires `wake_callback` for the parent. After the child's
turn ends, deferred work runs `terminate_agent()`. The parent wakes, reads the
result, and continues.

Timeline:
```
Child turn N:
  complete("done")
    → _deliver(parent_id, RESPONSE envelope)
      → wake_callback(parent_id)       # Enqueued.
    → deferred: terminate_agent(child_id)
  turn ends → end_turn() → _drain_deferred()
    → terminate_agent(child_id)        # Child removed.

Parent wake:
  _process_wake(parent_id)
    → inbox.peek() → RESPONSE envelope
    → prompt: "Message from child:\ndone"
    → _execute_turn()
    → inbox.collect()                  # Drain after success.
```

---

## Wake Failure Handling

What happens when a wake-triggered turn crashes (provider exits non-zero,
network timeout, OOM, etc.).

### Peek-then-drain (implemented)

`_process_wake` wraps `_execute_turn` in try/except. The prompt is
built from `inbox.peek()` (non-destructive). `inbox.collect()` and
`message.delivered` logging happen only after the turn succeeds.

On failure: exception is logged, messages stay in the inbox, agent
goes back to IDLE. The branch is frozen until someone pokes it or
a new message triggers another wake attempt.

```
_process_wake(agent_id)
  → begin_turn()
  → _format_wake_prompt()        # peek(), not collect().
  → try: _execute_turn()
        _drain_inbox()           # collect() + log message.delivered.
    except: log warning, leave inbox intact.
```

`message.delivered` events are logged after the turn, not before.
Recovery replay still works — undelivered messages get re-injected,
and the post-recovery wake scan picks them up.

### Design: parent error notification

On wake-turn failure, the orchestrator delivers an ERROR message to
the parent's inbox. The parent wakes, reads the notification, and
decides what to do.

New enum value: `MessageKind.ERROR`.

Error envelope payload includes:
- Exception type and message.
- Summary of consumed messages (the ones the child failed to process).

The parent does not need to parse this — it's a human-readable
description the LLM can act on. The parent can:

1. **Poke** — retry the child's wake (messages are still in the inbox).
2. **Terminate** — give up on the child.
3. **Send new instructions** — adjust approach, then poke.
4. **Do nothing** — branch stays frozen indefinitely.

If the parent's turn also crashes processing the error notification,
the same mechanism applies: grandparent gets notified, parent's inbox
is preserved. Failures cascade upward, freezing branches, until
someone doesn't crash. If a root agent crashes, the error surfaces
to the daemon → CLI. The human operator retries.

### Design: poke

`poke(agent_name)` is a new tool that re-wakes a child without
sending a message. From the child's perspective, the crash never
happened — it wakes up, processes its original inbox, and continues.

Implementation: resolve child name → `wake_callback(child_id)`.
One line. No inbox mutation, no new message in the child's prompt.

```
Parameters:
  agent_name: str       # Name of a direct child.

Returns:
  {"status": "poked", "agent_id": "uuid"}
```

Behavior by child state:

| Child state              | Result                                    |
|--------------------------|-------------------------------------------|
| IDLE + messages in inbox | Re-wakes, retries the turn.               |
| IDLE + empty inbox       | No-op (wake loop checks, skips).          |
| BUSY                     | No-op (wake loop checks, skips).          |
| Terminated / not found   | Error: child not found or not alive.      |

Poke is distinct from `send_message` because it adds nothing to
the inbox. The child's prompt is identical to the failed attempt.

### Caveat: provider conversation history

Peek-then-drain guarantees the orchestrator re-sends the same prompt
on poke. But the provider's conversation context may contain a ghost
of the failed attempt — e.g. cursor-agent with `--resume` might have
the crashed turn's prompt in its session history even though no
response was recorded.

This is provider-specific and unavoidable without provider-level
rollback (which none of the current providers support). In practice,
the child agent sees its messages and processes them. If the provider
retained the failed prompt, the agent might notice "I was asked this
before" — that's acceptable.

"Crash never happened" is the orchestrator's guarantee, not the
provider's.

---

## Fuzzing Strategy

The stateful fuzzer (`test_orchestrator_fuzz.py`) needs to exercise wake failure
paths under random interleaving. The current binary FakeProvider/FlakyProvider
split can't reach most failure interleavings — a single ChaosProvider replaces
both.

### ChaosProvider

A provider whose failures are controlled by Hypothesis. All randomness lives in
Hypothesis strategies so shrinking, replay, and `derandomize=True` work.

**Failure surface.** Methods that talk to the agent subprocess — the network
boundary:

| Method | Failure mode |
|-----------|----------------------------------------------|
| `create()` | Spawn fails — deferred drain cleanup path. |
| `send()` | Turn fails — IDLE rollback. Can also yield partial chunks before crashing. |
| `suspend()` | Eviction fails — session stays in multiplexer slots (rollback). |
| `restore()` | Session can't resume from suspension. |

`stop()` is best-effort (already wrapped in try/except). `models()` is
metadata, never fails. Neither is in scope.

**Per-provider, not per-session.** All sessions share the same network. A
prolonged outage fails every session, not just one.

**Schedule.** Hypothesis draws a failure schedule upfront as a deque of
outcomes. Each failing method pops the next entry; empty deque means succeed.
Two modes:

- **Intermittent.** `st.lists(st.booleans())` — True = fail. Hypothesis
  controls the exact sequence, can shrink to the minimal failing case.
- **Prolonged.** `st.integers(min_value=1, max_value=N)` — fail the next K
  consecutive calls, then recover. Models a network outage window.

**Partial send.** A schedule entry can be `(fail_after_chunks=N)` instead of a
plain bool. The generator yields N chunks, then raises. Exercises the non-zero
exit after partial output path.

### Fuzzer rules

The current `run_turn` / `run_turn_flaky` split generalizes into a single rule
that handles both outcomes via try/except and updates shadow state accordingly.

**Root retry.** A new rule simulates the human operator: pick a failed root
agent, call `run_turn(agent_id, "retry")`. This is the top-level recovery path
— no poke tool needed, just another turn.

**Non-root retry.** Blocked on parent error notification and `poke` tool
landing. Once those exist, a rule where the parent calls `poke(child_name)`
after receiving an ERROR message exercises the full cascade.

**Wake loop.** The fuzzer currently doesn't call `start_wake_loop`. Running the
async wake loop inside synchronous Hypothesis rules requires pumping the event
loop between steps (via `nest_asyncio` or manual `_process_wake` calls). Until
the wake loop is integrated, explicit `run_turn` calls are the only way to
exercise failure paths — wake-triggered failures remain untested under random
interleaving.

### Shadow model

With ChaosProvider, the fuzzer can't predict whether a turn succeeds. The
shadow model must handle both outcomes:

```
try:
    _run(self.orch.run_turn(agent, "go"))
    # Success — drain shadow state as usual.
except RuntimeError:
    pass
# Agent must be IDLE either way.
assert node.state == AgentState.IDLE
```

Children of Chaos-backed agents inherit the Chaos provider. The shadow model
must track this (the flaky inheritance bug that already bit us once).

---

## Open Questions

- **Cascading wake depth.** If A wakes B wakes C, the chain runs
  sequentially in the wake loop. No depth limit beyond the batch cap.
  May need explicit depth tracking if deep chains become a problem.
- **Priority.** All wakes are equal. No mechanism to prioritize responses
  over notifications.
