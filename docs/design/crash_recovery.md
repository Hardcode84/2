# Crash Recovery

How Substrat recovers after an unclean daemon shutdown (kill -9, power loss,
OOM, etc.).

## Core idea: the event log is the source of truth

Every session operation (create, send, response, state transition) is logged
to an append-only JSONL file before the operation is acknowledged. On crash
recovery, the log is replayed to reconstruct the conversation state.

This eliminates the "stale provider blob" problem: a session that was never
evicted from the multiplexer still has a complete log. The `provider_state`
blob from `suspend()` is a fast-path optimization, not a correctness
requirement.

## Event log format

Context fields are set by the caller when creating the `EventLog` and
injected into every entry. Currently the scheduler sets
`{"session_id": "<hex>"}`. Event-specific payload lives under `data`.

Implemented events (logged by `TurnScheduler`):

```jsonl
{"session_id":"...","ts":"...","event":"turn.start","data":{"prompt":"..."}}
{"session_id":"...","ts":"...","event":"turn.complete","data":{"response":"..."}}
{"session_id":"...","ts":"...","event":"suspend.result","data":{"state_size":N}}
{"session_id":"...","ts":"...","event":"session.restored","data":{"provider":"...","model":"..."}}
```

`suspend.result` is logged when the multiplexer evicts a session (LRU).
`state_size` is the byte length of the serialized provider blob — the blob
itself lives in `session.json`, not duplicated in the log. `session.restored`
is logged when `send_turn` re-acquires a previously evicted session.

Implemented events (logged by `Orchestrator`):

```jsonl
{"session_id":"...","ts":"...","event":"agent.created","data":{"agent_id":"<hex>","name":"...","parent_session_id":null,"instructions":"..."}}
{"session_id":"...","ts":"...","event":"agent.terminated","data":{"agent_id":"<hex>"}}
{"session_id":"...","ts":"...","event":"message.enqueued","data":{"message_id":"<hex>","sender":"<hex>","recipient":"<hex>","kind":"...","payload":"...","timestamp":"...","reply_to":null,"metadata":{...}}}
{"session_id":"...","ts":"...","event":"message.delivered","data":{"message_id":"<hex>"}}
```

`message.enqueued` is logged to the **recipient's** session log before
`inbox.deliver()`. `message.delivered` is logged after `inbox.collect()`
drains the inbox. Both events are fired by `ToolHandler` via a
`LogCallback` that the orchestrator resolves to the correct session log.

Planned events (not yet implemented):

```jsonl
{"session_id":"...","ts":"...","event":"session.created","data":{"provider":"...","model":"...","system_prompt":"..."}}
```

Location: `~/.substrat/agents/<uuid>/events.jsonl`.

Fsynced after every `log()` call. Cost is negligible — fsync takes
milliseconds, inference takes seconds.

### Serialization contract

All log entries must be JSON-serializable from plain types only: strings,
numbers, bools, lists, dicts. No opaque Python objects. UUIDs as hex strings,
timestamps as ISO 8601 strings. This is a stability contract — the log format
must be readable across versions and by external tools.

## Agent lifecycle events

The orchestrator logs agent lifecycle events to the session's own event log.
This is the only persistence for tree structure — no `tree.json`, no agent
metadata in `session.json`. The event log is already crash-safe (WAL, fsync),
so agent metadata inherits those guarantees for free.

### Events logged by the orchestrator

```jsonl
{"ts":"...","event":"agent.created","data":{"agent_id":"<hex>","name":"alpha","parent_session_id":null,"instructions":"..."}}
{"ts":"...","event":"agent.created","data":{"agent_id":"<hex>","name":"worker","parent_session_id":"<parent-hex>","instructions":"..."}}
{"ts":"...","event":"agent.terminated","data":{"agent_id":"<hex>"}}
```

`parent_session_id` (not `parent_id`) links to the parent's session UUID —
the same UUID used as the directory name under `~/.substrat/agents/`. This
avoids a dependency on agent UUIDs for the tree link; session UUIDs are the
stable on-disk identifiers.

The timing of `agent.created` differs between root and child agents:

**Root agents.** Session is created first, then `tree.add()`, then
`agent.created` is logged. If the daemon crashes between session creation
and logging, recovery sees the session but no `agent.created` event — the
session is orphaned and gets cleaned up.

**Child agents.** `tree.add()` happens synchronously during `spawn_agent`,
but session creation is *deferred* until the parent's turn ends.
`agent.created` is logged during the deferred spawn, after the child's
session is created. If the daemon crashes before the deferred spawn runs,
the child is in the in-memory tree but has no session and no log entry —
it vanishes on recovery. This is safe: the parent's turn was still
in-flight, so the spawn was never acknowledged.

`agent.terminated` is logged before `tree.remove()` and before the session's
event log is closed. On recovery, sessions with a terminated event are
skipped.

### Tree reconstruction

On daemon startup:

1. `SessionStore.recover()` — load all sessions, flip ACTIVE → SUSPENDED,
   persist. Returns the full session list (calls `scan()` internally).
2. For each non-terminated session, read its event log and find the
   `agent.created` event. Extract `agent_id`, `name`, `parent_session_id`,
   `instructions`.
3. Build a `session_id → agent_id` index from step 2.
4. For each agent, resolve `parent_session_id` → `parent_agent_id` using
   the index. `null` parent means root agent.
5. Construct `AgentNode` objects, `tree.add()` in dependency order (roots
   first, then children).
6. Rebuild `InboxRegistry` and `ToolHandler` instances for each agent.

Sessions without an `agent.created` event are orphans from a crash during
creation — terminate and clean up. Sessions with `agent.terminated` are
already dead — skip.

### Why not a separate tree file

- One fewer file to keep in sync. The event log already fsyncs per entry.
- Tree mutations (spawn, terminate) are rare — one log entry each. Zero
  write amplification compared to rewriting a `tree.json` on every spawn.
- The event log is already the crash recovery source of truth for everything
  else. Extending it to cover agent metadata is natural.
- Reconstruction cost is proportional to the number of agents, not the
  number of turns. Even with thousands of log entries per session, finding
  the first `agent.created` is a single scan.

## Recovery by provider type

### Agentic providers (cursor-agent, Claude CLI)

These manage their own conversation state (cursor-agent in local SQLite,
Claude CLI server-side). Recovery:

1. Read the session record for the provider session ID (saved at creation).
2. Resume via `--resume <id>`. The provider picks up from its last completed
   turn.
3. Scan the event log to determine which daemon-level messages were delivered
   and which were not.

The event log is not replayed into the provider — it's used to reconcile
the daemon's messaging state.

### Bare LLM providers (OpenRouter, future API providers)

No server-side or local state. The full conversation must be reconstructed:

1. Read the event log.
2. Replay the chain of `send` / `send.result` pairs to rebuild the
   conversation context.
3. Create a fresh API session with the reconstructed context.

The event log is the only copy of the conversation. If the log is lost, the
session is unrecoverable.

## Guarantees

### What is guaranteed

- **Session records survive crashes.** Written atomically before first
  `send()`.
- **Agent tree survives crashes.** `agent.created` event is logged to the
  session's event log before spawn is acknowledged. Tree is reconstructed
  from these events on recovery.
- **Event log is durable up to the last fsync.** Append-only, fsynced per
  entry. Pending-file WAL ensures no acknowledged entry is lost. Partial
  trailing lines from crash mid-write are truncated on recovery.
- **Pending messages survive crashes.** Enqueue events are logged before
  delivery.

### What is not guaranteed

- **The in-flight turn.** If the provider was mid-inference, that turn is
  lost. Cost is wasted. Resuming starts a fresh turn.
- **Undelivered replies.** If B replied but the daemon died before delivering
  to A, B's reply is in B's log but not in A's. The daemon can detect this
  on recovery by cross-referencing logs.
- **Exact message ordering.** After recovery, messages may be re-delivered.
  Agents must tolerate this.
- **Ephemeral state.** Provider processes, multiplexer slots — rebuilt from
  scratch.

## What Substrat persists

### 1. Session record

`~/.substrat/agents/<uuid>/session.json`. Metadata snapshot: agent_uuid,
provider_name, model, state, provider_state_blob (base64). Written atomically
on creation and state transitions.

### 2. Event log

`~/.substrat/agents/<uuid>/events.jsonl`. Append-only. Fsynced per turn.
Source of truth for conversation reconstruction and message reconciliation.

### 3. Agent tree (derived from event logs)

No separate tree file. The agent tree is reconstructed from lifecycle events
in the per-session event logs. Each session's log contains the agent metadata
needed to rebuild its node and parent-child link. See "Agent lifecycle events"
above.

## Recovery procedure

On daemon startup:

1. **Read session records.** `SessionStore.scan()` loads all sessions.

2. **Mark all ACTIVE sessions as SUSPENDED.** `SessionStore.recover()`.

3. **Reconstruct agent tree.** Scan each session's event log for
   `agent.created` / `agent.terminated` events. Build `AgentNode` objects
   and `tree.add()` in dependency order. Clean up orphaned sessions (no
   `agent.created` event).

4. **Reconcile messages.** For each recovered agent, scan its cached event
   log entries for `message.enqueued` and `message.delivered` events.
   Compute the pending set (enqueued minus delivered by message_id).
   Reconstruct `MessageEnvelope` objects from stored data and inject into
   the agent's inbox. No new events are logged during re-injection —
   duplicate delivery on subsequent recovery is tolerable.

5. **Resume root agents** (not yet implemented). For agentic providers:
   `--resume` with saved session ID. For bare LLM: replay event log to
   reconstruct context. Sub-agents resumed on demand by parents.

## Persistence strategy

Write-on-mutate with fsync. State mutations are infrequent (create, spawn,
state transition). The event log fsyncs on every `log()` call — typically
twice per turn (`turn.start` + `turn.complete`).

- **Create session**: write record atomically, fsync, then proceed.
- **Spawn agent**: append `agent.created` to session event log, fsync,
  return success. No separate tree file.
- **Terminate agent**: append `agent.terminated` to session event log, fsync.
- **Each turn**: append send + response to event log, fsync.
- **Message routing**: append enqueue/deliver events, fsync.

### Atomic writes

Session records use write-to-temp-then-rename:

1. Write to `<path>.tmp` (same directory = same filesystem).
2. `fsync` the file descriptor.
3. `os.replace()` over the target (atomic per POSIX).

A crash mid-write leaves a stale `.tmp` file, never a corrupted target.
Recovery ignores `.tmp` files.

The event log uses a pending-file WAL for crash safety:

1. Write the entry to `events.pending` and fsync.
2. Append the entry to `events.jsonl` and fsync.
3. Unlink `events.pending`.

On recovery, if `.pending` exists, its content is appended to the main log
(with tail-dedup to avoid doubles). A partial trailing line from a crash
mid-append is truncated before the pending entry is replayed.

## Testing

### Fault injection

A mock provider (`FakeSession`) with controllable behavior. The test harness
wraps every persist operation with a hook that can kill the daemon:

- After `create()` but before writing session record.
- After writing session record but before logging `agent.created`.
- After logging `agent.created` but before returning spawn success.
- After `send()` completes but before logging the response.
- After logging the response but before delivering the reply.
- After enqueueing a message but before logging delivery.
- After logging `agent.terminated` but before removing from tree.

For each injection point: crash, restart, run recovery, assert invariants.

### Recovery invariants (the oracle)

After every crash+restart:

- No ACTIVE sessions exist (all moved to SUSPENDED).
- Every session record on disk has a valid state.
- Agent tree is consistent: no orphan children, no dangling parent refs.
- Undelivered messages are either logged as enqueued (recoverable) or were
  never acknowledged to the sender.
- Event logs are valid JSONL prefixes (no corruption beyond the last fsync).
- For bare LLM providers: the conversation reconstructed from the log matches
  the oracle's expected state.

### Fuzzer

Two layers: an in-memory lifecycle fuzzer (exists now) and a crash-recovery
fuzzer (requires tree persistence, future work).

#### Hypothesis stateful testing

Both fuzzers use Hypothesis `RuleBasedStateMachine`. This gives us:

- **Rules** — actions the fuzzer can take (create, spawn, turn, terminate,
  crash). Each rule has preconditions that guard against illegal states.
- **Invariants** — checked after every step (registry sync for handlers
  and inboxes, tree-shadow match, parent-child consistency, no stuck
  states, all-idle-between-steps).
- **Shrinking** — when a failure is found, Hypothesis deterministically
  reduces the sequence to a minimal reproduction. 3-step repros instead
  of 30-step monsters.
- **Database** — `DirectoryBasedExampleDatabase` in `.hypothesis/` saves
  failing examples. Subsequent runs replay saved failures first, then
  explore new sequences. Findings persist across runs.

#### Lifecycle fuzzer (in-memory)

Lives in `tests/stress/test_orchestrator_fuzz.py`. Exercises the
orchestrator's public API with random sequences of `create_root`,
`spawn_child`, `run_turn`, and `terminate_leaf`. Uses a small name alphabet
(5 names) to force collisions. Shadow state tracks alive agents and
parent-child links; invariants verify the real tree matches. Multiplexer
slots are set low (3) to force the eviction/suspend/restore path. No
persistence, no crash simulation — pure state machine correctness.

Settings: 200 examples, 30 steps per example (`stateful_step_count`),
`deadline=None`. Gated behind `--run-stress`.

#### Crash-recovery fuzzer (future)

Extends the lifecycle fuzzer with a `crash_and_recover` rule:

1. **Generate** a random sequence of actions from a seeded RNG:
   `create_agent`, `send_message`, `broadcast`, `spawn_agent`,
   `deliver_message`, `suspend_agent`, etc.

2. **Execute** the sequence against the real daemon code (with a mock
   provider). Each action advances the state machine — no actual delays,
   all timing is simulated.

3. **Crash** after a random prefix of N steps (chosen from the same seed).
   The "crash" just stops execution and discards in-memory state.

4. **Recover** by running the recovery procedure against the persisted
   state on disk.

5. **Replay** the same N-step prefix against a fresh in-memory model (the
   oracle) that tracks what *should* have been persisted by each step.
   Compare the recovered state with the oracle's expected state.

6. **Assert** that recovered state is a valid subset of the oracle state:
   everything that was fsynced before the crash point is present, nothing
   after it is.

Requires tree persistence via event logs (see "Agent lifecycle events"
above) before this can be built.

### Parallel execution

Hypothesis + pytest-xdist. `DirectoryBasedExampleDatabase` is process-safe
on Linux — entries are written atomically (temp file + rename), so multiple
workers sharing the same `.hypothesis/` directory is fine. No per-worker
isolation or `database=None` hacks needed.

```
pytest tests/stress/ --run-stress -n 64 -q
```

### CI profiles

Two Hypothesis profiles for different contexts:

**PR gate (`ci` profile).** `derandomize=True`, moderate example count
(~50). The seed is derived deterministically from the test name, so the
same inputs run every time. No flakes from unrelated changes — a failure
means the PR broke something. Fast enough to run on every push.

**Nightly (`nightly` profile).** Random seeds, high example count, many
xdist workers. Total runtime capped by `pytest --timeout` at the job
level — burn as many cycles as the schedule allows. `deadline=None`
disables the per-example timing check (default 200ms) so the fuzzer
doesn't false-positive under load. Failures get filed as issues, not
blamed on whoever pushed last. Runs on a cron schedule, not on PRs.

The nightly `.hypothesis/` database is persisted across runs via GitHub
Actions cache (`actions/cache` keyed on branch + date). This means the
nightly fuzzer builds a corpus over time — previously-found edge cases are
replayed first before exploring new sequences. A cache miss (first run,
or eviction) just starts fresh.

Hypothesis has built-in profile support:

```python
settings.register_profile(
    "ci", derandomize=True, max_examples=50,
    stateful_step_count=30, deadline=None,
)
settings.register_profile(
    "nightly", max_examples=5000,
    stateful_step_count=50, deadline=None,
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
```

`stateful_step_count` controls how many rule invocations per example.
The `HYPOTHESIS_PROFILE` env var selects the profile. CI pipelines set it;
local runs use the default (200 examples, 30 steps, `deadline=None`).

### Crash granularity

The interesting crash points are I/O boundaries (write, fsync, rename), not
arbitrary bytecode ops — memory state evaporates on crash regardless. Python
has no clean way to hook individual bytecodes, and it wouldn't help anyway.

The persistence layer (`atomic_write` and friends) is mocked with a
crash-injecting wrapper that can fail at each sub-step:

- After `write()` but before `fsync()` (data in page cache, not on disk).
- After `fsync()` but before `os.replace()` (temp file durable, target stale).
- After `os.replace()` (fully committed).

The mock tracks a "crash counter" — decrement on each I/O op, crash when it
hits zero. This gives deterministic, reproducible sub-step crashes without
touching real I/O. For paranoid validation against real filesystems, LazyFS
(FUSE-based power loss simulator) can discard unfsynced pages.

## Open questions

- What recovery prompt to give root agents after a crash.
- Whether to auto-resume all agents or only roots (letting parents decide
  which children to restart).
- How to handle the "B replied but A never got it" — cross-reference B's
  log to find the reply and inject it into A's next turn? Or ask B to repeat?
