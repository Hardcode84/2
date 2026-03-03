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
              → inbox.collect() (drain all)
              → log message.delivered per envelope
              → format prompt string
           → _execute_turn(node, prompt)
              → scheduler.send_turn()
              → end_turn() (BUSY → IDLE)
              → _drain_deferred()
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

`_format_wake_prompt()` drains the inbox via `collect()` and builds a prompt.

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
(system, daemon) get their symbolic names, tree nodes get their agent name,
unknown senders fall back to raw UUID hex.

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
- Inbox empty (drained by concurrent `check_inbox`).
- `begin_turn()` raises `AgentStateError` (defensive).

No wake is "lost" — if the agent has messages when it returns to IDLE, the
next delivery or the post-turn `_drain_deferred` will re-trigger.

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
    → inbox.collect() → RESPONSE envelope
    → prompt: "Message from child:\ndone"
    → _execute_turn()
```

---

## Open Questions

- **Cascading wake depth.** If A wakes B wakes C, the chain runs
  sequentially in the wake loop. No depth limit beyond the batch cap.
  May need explicit depth tracking if deep chains become a problem.
- **Priority.** All wakes are equal. No mechanism to prioritize responses
  over notifications.
