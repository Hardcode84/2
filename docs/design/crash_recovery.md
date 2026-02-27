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

Context fields (`agent_id`, etc.) are set by the caller when creating the
`EventLog` and injected into every entry. Event-specific payload lives under
`data`. Event names derive from provider method names (`send`, `suspend`) with
a `.result` suffix for after-call entries.

```jsonl
{"agent_id":"...","ts":"...","event":"session.created","data":{"provider":"...","model":"...","session_id":"...","system_prompt":"...","workspace":"..."}}
{"agent_id":"...","ts":"...","event":"send","data":{"message":"..."}}
{"agent_id":"...","ts":"...","event":"send.result","data":{"text":"..."}}
{"agent_id":"...","ts":"...","event":"suspend.result","data":{"result":"<base64>"}}
{"agent_id":"...","ts":"...","event":"session.restored","data":{"provider":"...","model":"...","session_id":"...","workspace":"..."}}
{"agent_id":"...","ts":"...","event":"message.enqueued","data":{"message_id":"...","sender":"...","recipient":"..."}}
{"agent_id":"...","ts":"...","event":"message.delivered","data":{"message_id":"..."}}
```

Location: `~/.substrat/agents/<uuid>/events.jsonl`.

Fsynced after every `log()` call. Cost is negligible — fsync takes
milliseconds, inference takes seconds.

### Serialization contract

All log entries must be JSON-serializable from plain types only: strings,
numbers, bools, lists, dicts. No opaque Python objects. UUIDs as hex strings,
timestamps as ISO 8601 strings. This is a stability contract — the log format
must be readable across versions and by external tools.

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
- **Agent tree survives crashes.** Parent-child links written before spawn
  is acknowledged.
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

### 3. Agent tree

Parent-child links. Written on every `spawn_agent`, before returning success.
Location: `~/.substrat/tree.json` or per-agent in the session record.

## Recovery procedure

On daemon startup:

1. **Read session records.** Load provider name, model, state for each agent.

2. **Reconstruct agent tree.** From persisted links.

3. **Mark all ACTIVE sessions as SUSPENDED.**

4. **Reconcile messages.** For each agent, scan its event log for
   `message.enqueued` without a matching `message.delivered`. Cross-reference
   with recipient logs to detect undelivered replies.

5. **Resume root agents.** For agentic providers: `--resume` with saved
   session ID. For bare LLM: replay event log to reconstruct context.
   Sub-agents resumed on demand by parents.

## Persistence strategy

Write-on-mutate with fsync. State mutations are infrequent (create, spawn,
state transition). Event log appends are per-turn (seconds apart).

- **Create session**: write record atomically, fsync, then proceed.
- **Spawn agent**: write tree link, fsync, return success.
- **Each turn**: append send + response to event log, fsync.
- **Message routing**: append enqueue/deliver events, fsync.

### Atomic writes

All persisted files (session records, tree) use write-to-temp-then-rename:

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
- After writing session record but before first `send()`.
- After `send()` completes but before logging the response.
- After logging the response but before delivering the reply.
- After enqueueing a message but before logging delivery.
- After writing tree link but before returning spawn success.

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

Deterministic, simulation-based. No real timing, no threads, no sleeps.

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

Repeat for many seeds. Log the seed and step index on failure for
reproduction. Gate behind `@pytest.mark.stress`.

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
