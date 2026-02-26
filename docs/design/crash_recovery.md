# Crash Recovery

How Substrat recovers after an unclean daemon shutdown (kill -9, power loss,
OOM, etc.).

## Guarantees

### What the session/provider layer guarantees

- **Provider state is opaque.** Each provider persists its own conversation
  state however it wants. Substrat stores the provider's opaque state blob
  (from `ProviderSession.suspend()`) but does not interpret it.
- **`restore()` recreates a session from its state blob.** After a crash, if
  Substrat has the blob on disk, the provider can resume the conversation.
- **Sessions found in ACTIVE state on startup are stale.** The daemon was not
  running, so no provider process is alive. These sessions are moved to
  SUSPENDED during recovery.

### What Substrat guarantees (with write-on-mutate)

- **Session records survive crashes.** Written to disk before the first
  `send()`, fsynced.
- **Agent tree survives crashes.** Parent-child links are written to disk
  before the spawn is acknowledged to the parent.
- **Pending messages survive crashes.** Written to the message log before
  being enqueued for delivery.

### What is not guaranteed

- **In-flight turns.** If a provider was mid-inference when the daemon died,
  that turn is lost. The API cost is wasted. Resuming starts a fresh turn.
  This is acceptable — inference is idempotent (mostly).
- **Undelivered replies.** If agent A sent a sync message to B, B replied,
  but the daemon died before delivering B's reply to A — that reply is lost
  from A's perspective. B's provider has it in history, A's does not.
- **Exact message ordering.** After recovery, messages may be re-delivered
  or arrive out of order. Agents must tolerate this (they're LLMs, not state
  machines).
- **Ephemeral state.** Provider processes, subprocess PIDs, multiplexer slot
  assignments — all rebuilt from scratch on restart.

## What Substrat persists

Three things, written to disk before acknowledging the operation:

### 1. Session records

`{agent_uuid, provider_name, model, state, provider_state_blob}`. Written
immediately after provider `create()`, before the first `send()`.

Location: `~/.substrat/sessions/<agent-uuid>.json` (or a single SQLite DB).

### 2. Agent tree

Parent-child links. Written on every `spawn_agent` call, before returning
success to the parent.

Location: `~/.substrat/tree.json` (or rows in the sessions DB).

### 3. Message log

Pending messages (sent but not yet delivered). Written when a message is
enqueued, removed when delivered. This is the hardest state to recover because
neither side's provider necessarily has a complete picture.

Location: `~/.substrat/messages/` or a table in the sessions DB.

## Recovery procedure

On daemon startup:

1. **Read session records.** For each agent, load the provider state blob,
   provider name, model, and workspace.

2. **Reconstruct agent tree.** Parent-child links are on disk. Rebuild the
   in-memory tree.

3. **Mark all ACTIVE sessions as SUSPENDED.** No provider processes are
   running after a crash.

4. **Scan for undelivered messages.** Check the message log for anything
   enqueued but not marked delivered. These need to be re-sent or flagged.

5. **Resume root agents.** The daemon restores root agent sessions via
   `provider.restore(state_blob)` and sends a recovery prompt. Sub-agents
   are resumed on demand by their parents.

## Persistence strategy

State mutations in Substrat are infrequent — create agent, send message,
state transitions. These are not hot-path operations. Write-on-mutate with
fsync is simple and sufficient:

- **Create session**: write record to disk, fsync, then proceed.
- **Spawn agent**: write tree link to disk, fsync, return success to caller.
- **Send message**: write to message log, fsync, then enqueue for delivery.
- **Deliver message**: mark as delivered in log, fsync.

No WAL, no periodic checkpoints, no batching. If performance ever matters
here, something has gone very wrong architecturally.

## Testing

### Fault injection

A mock provider (`FakeSession`) that returns canned responses with
controllable delays. The test harness wraps every persist operation with a
hook that can kill the daemon at that exact point:

- After `create()` but before writing session record.
- After writing session record but before first `send()`.
- After `send()` completes but before delivering the reply.
- After enqueueing a message but before marking it delivered.
- After writing tree link but before returning spawn success.
- Mid-inference (provider process alive, daemon dies).

For each injection point: crash, restart daemon, run recovery, assert
invariants.

### Recovery invariants (the oracle)

After every crash+restart, these must hold:

- No ACTIVE sessions exist (all moved to SUSPENDED).
- Every session record on disk has a valid state and parseable provider blob.
- Agent tree is consistent: no orphan children, no dangling parent refs.
- Undelivered messages are either in the log or were never acknowledged to
  the sender.
- The JSONL event log is a prefix of the pre-crash log (no corruption, no
  partial writes beyond the last fsynced entry).

### Fuzzer

Randomized stress test that exercises crash recovery at scale:

- Spawn a configurable number of agents in random tree topologies.
- Agents exchange messages at random intervals.
- A chaos thread kills the daemon at random moments (uniform over all
  persist operations, biased toward the interesting ones).
- After each kill: restart, run recovery, check invariants.
- Repeat for N iterations or until a violation is found.

The fuzzer should be deterministic given a seed, so failures are reproducible.
Log the seed, the sequence of operations, and the crash point on failure.

This is not a unit test — it's a standalone harness that runs for minutes or
hours. Gate it behind a separate pytest marker (e.g. `@pytest.mark.stress`).

## Open questions

- What recovery prompt to give root agents after a crash.
- Whether to auto-resume all agents or only roots (letting parents decide
  which children to restart).
- How to handle the "B replied but A never got it" scenario — replay from
  B's provider history? Silently drop? Ask B to repeat?
