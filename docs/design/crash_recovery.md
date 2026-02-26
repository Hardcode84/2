# Crash Recovery

How Substrat recovers after an unclean daemon shutdown (kill -9, power loss,
OOM, etc.).

## What survives without us

cursor-agent conversation history is stored in local SQLite databases
(`~/.cursor/chats/<workspace-hash>/<session-uuid>/store.db`). SQLite handles
crash recovery internally (WAL/journal mode). After a power loss, `--resume`
with the session UUID will work — cursor-agent picks up from the last
completed turn.

## What we lose

- Daemon's in-memory state: session registry, agent tree, pending messages.
- Any cursor-agent subprocess that was mid-turn is dead. The local SQLite DB
  is intact, but the in-flight API response is lost. Resuming starts a fresh
  turn.
- Undelivered messages: if agent A sent a sync message to agent B and B
  replied, but the daemon died before delivering the reply to A — that reply
  is lost. B's session has it in history, A's does not.
- Partial agent spawns: if the daemon was mid-way through creating a child
  agent (e.g. session created but tree not updated), the state is inconsistent.

## What Substrat must persist

Three things, written to disk before acknowledging the operation:

### 1. Session records

Minimum: `{agent_uuid, cursor_session_id, provider_name, model, workspace_path,
state}`. Written immediately after `create-chat`, before the first `send()`.

Location: `~/.substrat/sessions/<agent-uuid>.json` (or a single SQLite DB).

### 2. Agent tree

Parent-child links. Written on every `spawn_agent` call, before returning
success to the parent. A flat file per agent or a single tree file — either
works at our scale.

Location: `~/.substrat/tree.json` (or rows in the sessions DB).

### 3. Message log

Pending messages (sent but not yet delivered). Written when a message is
enqueued, removed when delivered. This is the only state that's hard to
recover from cursor-agent's local storage alone.

Location: `~/.substrat/messages/` or a table in the sessions DB.

## Recovery procedure

On daemon startup:

1. **Read session records.** For each agent, we know the cursor session ID,
   model, and workspace.

2. **Reconstruct agent tree.** Parent-child links are on disk. Rebuild the
   in-memory tree.

3. **Mark all agents as suspended.** No subprocesses are running after a
   crash. Every agent is effectively suspended regardless of what state it
   was in before.

4. **Scan for undelivered messages.** Check the message log for anything
   that was enqueued but not marked as delivered. These need to be re-sent
   or flagged for manual review.

5. **Resume root agents.** The daemon kicks off the root agent(s) with a
   recovery prompt (e.g. "You are resuming after a system restart. Check
   your team's status."). Sub-agents are resumed on demand by their parents.

## Persistence strategy

State mutations in Substrat are infrequent — create agent, send message, state
transitions. These are not hot-path operations. Write-on-mutate with fsync is
simple and sufficient:

- **Create session**: write session record to disk, fsync, then proceed.
- **Spawn agent**: write tree link to disk, fsync, return success to caller.
- **Send message**: write to message log, fsync, then enqueue for delivery.
- **Deliver message**: mark as delivered in log, fsync.

No WAL, no periodic checkpoints, no batching. If performance ever matters
here, something has gone very wrong architecturally.

## What we explicitly do not recover

- **In-flight inference responses.** If cursor-agent was mid-turn when the
  daemon died, that turn's API cost is wasted. The next `--resume` starts a
  new turn. This is acceptable — inference is idempotent (mostly).
- **Exact message ordering guarantees.** After recovery, messages may be
  re-delivered or delivered out of order. Agents should be resilient to this
  (and they are, because they're LLMs, not state machines).
- **Ephemeral state.** MCP server processes, subprocess PIDs, multiplexer
  slot assignments — all rebuilt from scratch on restart.

## Open questions

- What recovery prompt to give root agents after a crash. Too little context
  and they're confused; too much and we're burning tokens on exposition.
- Whether to auto-resume all agents or only roots (letting parents decide
  which children to restart).
- How to handle the "B replied but A never got it" scenario — replay from
  B's session history? Silently drop? Ask B to repeat?
