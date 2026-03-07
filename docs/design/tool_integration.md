# Tool Integration

How Substrat exposes custom tools (messaging, spawning, etc.) to agents.

## Mechanism

Substrat runs an MCP server per agent. The provider spawns it alongside the
agent process. The server exposes Substrat tools (messaging, spawning, inbox)
and connects back to the daemon over a Unix socket to fulfill requests.

Each agent gets its own server instance. Agent identity is baked into the
server's launch args at config generation time — no runtime context passing
needed.

Provider-specific details on how the MCP server is registered and discovered
live in `providers/`.

## Execution Model: Non-blocking Tools Only

All MCP tools return immediately. No tool call ever blocks the agent process.
This is a hard rule — it prevents deadlocks and eliminates the need for
special slot accounting.

### Why not block?

If a tool call blocks, the agent process stays alive, holding a multiplexer
slot. If the recipient also needs a slot and none are free → deadlock.
Blocking also complicates lifecycle management (what if we need to suspend a
blocked agent?).

### Message delivery is always async

All messages are delivered to the recipient's inbox. The recipient is woken
automatically (auto-wake). When the recipient replies, the sender is woken
too. No polling, no blocking, no special slot accounting.

The agent's turn ends after `send_message` returns. The slot is freed. The
reply arrives as a new turn triggered by auto-wake.

### Deferred execution

Some tools record intent and defer heavy work until the agent's turn ends:

- **`spawn_agent`**: registers the child in the agent tree immediately (so the
  parent can reference it by name), but defers `provider.create()` +
  `mux.put()` until the parent's slot is released. The daemon maintains a
  pending-spawn queue drained between turns.

This keeps the parent's turn fast and avoids holding two slots simultaneously
(parent + child). With `max_slots=4`, a parent can spawn up to 3 children in
one turn without contention — the daemon creates them sequentially after the
parent releases.

### System prompt guidance

Agents are told in their system prompt:
- Tool calls return immediately with a status.
- Spawned agents start working after your current turn ends.
- Messages from other agents wake you automatically — no need to poll.
- Call `complete(result)` when your work is done.

## Tool Catalog

All tools return JSON. All tools are non-blocking.

### `send_message`

Send a message to another agent (parent, child, or sibling). Root agents
can also send to `"USER"` to notify the human operator. All messages are
async — the recipient is woken automatically, and the sender is woken when
a reply arrives.

```
Parameters:
  recipient: str       # Agent name, or "USER" for root agents.
  text: str            # Message body.

Returns:
  {"status": "sent", "message_id": "uuid"}
```

### `broadcast`

Send a message to all siblings in the team.

```
Parameters:
  text: str

Returns:
  {"status": "sent", "message_id": "uuid", "recipient_count": int}
```

Replies arrive as separate messages, one per respondent.

### `check_inbox`

Retrieve pending messages (notifications, replies, etc.). Mostly useful for
inspecting what arrived without waiting for auto-wake. Optional filters narrow
which messages are collected; unmatched messages remain in the inbox.

```
Parameters:
  sender: str | null    # Only return messages from this agent name.
  kind: str | null      # Only return messages of this kind
                        # (request, response, notification, error).

Returns:
  {"messages": [{"from": "name", "text": "...", "message_id": "uuid"}, ...]}
```

### `spawn_agent`

Request creation of a subagent. The tool returns immediately with the child's
UUID — actual provider session creation is **deferred** until the parent's turn
ends and its multiplexer slot is released. This avoids slot pressure during the
parent's turn and lets the daemon schedule child creation when capacity is
available.

Messages sent to the child before it is live are queued and delivered once the
provider session is ready.

```
Parameters:
  name: str
  instructions: str
  workspace: str | WorkspaceSpec | null   # Name or inline spec. See workspace.md.
  metadata: dict[str, str] | null         # Key-value metadata attached to child.

Returns:
  {"status": "accepted", "agent_id": "uuid", "name": "str"}
```

The workspace must exist (or be created inline). See
[workspace.md](workspace.md) for the full workspace tool catalog and the
inline convenience form.

Typical spawn-then-send flow:

1. Parent calls `spawn_agent("analyst", ...)` + `send_message("analyst", "go")`
   in the same turn. Both return immediately.
2. Parent's turn ends → slot released.
3. Daemon creates the analyst's provider session, delivers the queued message.
4. Analyst works, replies → auto-wake delivers reply to parent as a new turn.

### `inspect_agent`

View a subordinate's state, metadata, and recent activity.

```
Parameters:
  name: str

Returns:
  {"state": "idle|busy|waiting", "metadata": {...}, "recent_messages": [...]}
```

### `complete`

Send a result to the calling agent's parent and self-terminate. Sugar for
the two-step "send RESPONSE + terminate" pattern. Only valid for leaf agents
(no active children) that have a parent.

```
Parameters:
  result: str           # Final result to deliver to parent.

Returns:
  {"status": "completing", "message_id": "uuid"}
```

The RESPONSE message fires auto-wake on the parent. Self-termination is
deferred until the agent's current turn ends, following the same pattern as
`spawn_agent`.

### `poke`

Re-wake a child agent without sending a message. Used after a child's
wake-triggered turn crashes — the child's inbox still has its original
messages (preserved by peek-then-drain), so poke retries the turn with
the same prompt. From the child's perspective, the crash never happened.

```
Parameters:
  agent_name: str       # Name of a direct child.

Returns:
  {"status": "poked", "agent_id": "uuid"}
```

Poke enqueues a wake notification. If the child is IDLE with pending
messages, the wake loop picks it up and runs a turn. If the child is
BUSY or has an empty inbox, the wake is silently skipped.

Distinct from `send_message` because it adds nothing to the inbox.
The child's prompt is identical to the failed attempt.

See [wake.md — Wake Failure Handling](wake.md#wake-failure-handling).

### `list_children`

Enumerate all direct children with state, metadata, and pending message count.
One-call inventory for project agents tracking multiple workers — survives
context compaction because the LLM can re-discover child state without
inspecting each one individually.

```
Parameters: (none)

Returns:
  {"children": [{"name": "str", "agent_id": "uuid", "state": "idle|busy|waiting|terminated", "metadata": {...}, "pending_messages": int}, ...]}
```

### `set_agent_metadata`

Set or delete a metadata key on a direct child. Metadata is daemon-tracked
(`dict[str, str]` on `AgentNode`), persisted via event log, and survives
recovery. Use null value to delete a key.

```
Parameters:
  agent_name: str        # Name of a direct child.
  key: str               # Metadata key.
  value: str | null      # Value to set. Null to delete.

Returns:
  {"status": "updated", "agent_name": "str", "key": "str", "value": "str|null"}
```

### `remind_me`

Schedule a delayed self-notification. Delivered as a SYSTEM NOTIFICATION
to the caller's inbox, triggering auto-wake. Timer task is created after
the current turn ends (deferred work). Ephemeral — lost on daemon crash.

```
Parameters:
  reason: str            # Reminder payload.
  timeout: number        # Seconds until first delivery (must be > 0).
  every: number | null   # Repeat interval in seconds. Omit for one-shot.

Returns:
  {"status": "scheduled", "reminder_id": "uuid"}
```

Cancelled by `cancel_reminder`. All pending reminders are cancelled when
the agent is terminated or the daemon shuts down.

**Implementation notes.** Timers are `asyncio.Task`s created as deferred work
(same pattern as spawn). Delivery reuses the existing inbox + wake path —
no new state transitions. Ephemeral: lost on daemon crash (no persistence).
Not in the orchestrator fuzzer: the code path under test (`inbox.deliver` +
`_notify_wake`) is already stress-tested by messaging rules, and real
`asyncio.sleep` timers would require mocked time for deterministic fuzzing.

### `cancel_reminder`

Cancel a previously scheduled reminder. Returns error if the reminder
has already fired (one-shot) or is unknown.

```
Parameters:
  reminder_id: str       # UUID returned by remind_me.

Returns:
  {"status": "cancelled", "reminder_id": "uuid"}
```

### `list_workspaces`

List visible workspaces (own, children's, parent's scopes).

```
Parameters: (none)

Returns:
  {"workspaces": [{"name": "str", "scope": "self" | "parent" | "<child-name>", "mutable": bool}, ...]}
```

### `create_workspace`

Create a workspace in the calling agent's scope. Supports live views via
`view_of`.

```
Parameters:
  name: str
  network_access: bool = false
  view_of: str | null           # Source workspace ref for live view.
  subdir: str = "."             # Subfolder within source (view_of only).
  mode: "ro" | "rw" = "ro"     # View mode (view_of only).

Returns:
  {"status": "created", "name": "str"}
```

### `delete_workspace`

Delete a workspace. Must be in a mutable scope.

```
Parameters:
  name: str                     # Workspace ref (scoped).

Returns:
  {"status": "deleted"}
```

### `link_dir`

Link a directory from the calling agent's workspace into a target workspace.

```
Parameters:
  workspace: str                # Target workspace ref (scoped).
  source: str                   # Path inside caller's own workspace.
  target: str                   # Mount path inside target workspace.
  mode: "ro" | "rw" = "ro"

Returns:
  {"status": "linked"}
```

### `link_from`

Mount a directory from any visible workspace into a mutable workspace.
Unlike `link_dir` (which sources from the caller's own workspace), this
tool can pull content from any workspace the caller can see — own,
children's, or parent's. The target must be in a mutable scope.

```
Parameters:
  source_workspace: str         # Source workspace ref (scoped). Must be visible.
  source: str                   # Path inside the source workspace.
  target: str                   # Mount path inside the target workspace.
  target_workspace: str | null  # Target workspace ref. Defaults to caller's own.
  mode: "ro" | "rw" = "ro"

Returns:
  {"status": "linked"}
```

Primary use case: integration. A project agent needs to access a child
worker's files for git merge. `link_from` avoids spawning a throwaway
integrator agent — the parent can mount the child's workspace directly.

### `unlink_dir`

Remove a linked directory from a workspace.

```
Parameters:
  workspace: str                # Workspace ref (scoped).
  target: str                   # Mount path to remove.

Returns:
  {"status": "unlinked"}
```

### `read_file` / `write_file`

These are provider-native tools — no need to reimplement. They operate within
the agent's workspace naturally.

## Auto-Wake Mechanism

When a message arrives in an IDLE agent's inbox, the daemon automatically
starts a new turn on that agent. This replaces the need for external polling
or explicit `agent.send` RPC calls to drive message flow.

Full design: [wake.md](wake.md). Key points:

- **Triggers**: message delivery, post-spawn inbox scan, crash recovery.
- **Processing**: background `asyncio.Task` drains a queue, deduplicates,
  guards on state, formats inbox contents as prompt, runs `_execute_turn`.
- **Safety**: 100 wakes/batch cap, `begin_turn` as concurrency guard.

## MCP Server Implementation

The MCP server lives in `src/substrat/provider/mcp_server.py`. It speaks
JSON-RPC 2.0 over stdio — one request per line, one response per line.

### Dispatch architecture

`McpServer` takes a `Sequence[ToolDef]` (from `substrat.model`) and a
`ToolDispatch` callable (`(str, dict) -> dict`). Tool schemas are structured
dataclasses (`ToolDef`, `ToolParam` in `model.py`); the server serializes
them to MCP JSON internally via `_tool_to_schema()`.

Two dispatch factories exist:

- **`direct_dispatch(methods)`** — takes a `Mapping[str, Callable]`
  (name → callable). Used by tests.
- **`daemon_dispatch(socket, agent_id)`** — UDS client that routes tool
  calls through `sync_call("tool.call", ...)` to the daemon.

### Error surfacing

| Condition | Response | Code |
|-----------|----------|------|
| Malformed JSON | Silently skipped | — |
| Notification (no `id`) | No response | — |
| Unknown method | JSON-RPC error | -32601 |
| Unknown tool name | JSON-RPC error | -32602 |
| Bad argument types | JSON-RPC error | -32602 |
| Unexpected dispatch exception | JSON-RPC error | -32603 |
| ToolHandler returns `{"error": ...}` | MCP tool result (success envelope) | — |

The last row matters: tool-level errors are application semantics, not protocol
failures. The LLM sees them as tool results and can adjust.

### Entry point

```
python -m substrat.provider.mcp_server --agent-id <uuid>
```

Requires `SUBSTRAT_SOCKET` in the environment. Without it, exits immediately —
no silent fallback.

### Data model

Tool definitions are layer-neutral frozen dataclasses in `src/substrat/model.py`:

- **`ToolParam`** — name, JSON type, description, required flag, optional
  default. Uses a `_MISSING` sentinel internally; `has_default` property
  hides it from callers.
- **`ToolDef`** — name, description, tuple of `ToolParam`. No serialization
  logic — that lives in the MCP server.

The agent tool catalog (`AGENT_TOOLS`) and workspace tool catalog
(`WORKSPACE_TOOLS`) both live in `src/substrat/agent/tools.py` as tuples of
`ToolDef` objects. `ALL_TOOLS = AGENT_TOOLS + WORKSPACE_TOOLS` is the unified
set used by the daemon and MCP server. Providers accept tools at construction
via `tools: Sequence[ToolDef]`.

## Alternative: CLI-based tool exposure

Instead of MCP or provider-specific function calling, expose tools as
executables in the workspace. Every provider can run bash — so a single
`.substrat/bin/substrat` binary with subcommands gives tool access to any
provider without per-provider integration work.

```
.substrat/bin/substrat send_message --to parent --text "done"
.substrat/bin/substrat spawn_agent --name worker --instructions "do X"
.substrat/bin/substrat check_inbox --sender worker
```

The binary talks to the daemon over the existing UDS socket
(`.substrat_socket`). From the daemon's perspective, the protocol is
identical to MCP dispatch — same `tool.call` RPC, same JSON payloads.

### Why this works

- **Provider-agnostic.** New provider = implement `start`/`send`/`stop`.
  No MCP server, no function-call schema registration, no output parsing.
- **Discoverable.** `ls .substrat/bin/`, `substrat --help`. Self-documenting.
- **Composable.** Agents can chain, pipe, script with standard shell idioms.
- **Messaging is fine.** All messages are async (tool returns immediately,
  reply arrives via auto-wake as a new turn). The CLI call never blocks long
  enough to hit provider bash timeouts.

### Trade-offs

- **No structured tool results.** MCP/function calling returns typed JSON.
  CLI returns text on stdout. The agent parses it, which is less reliable.
- **Weaker interception.** The daemon sees the call when the script hits
  the socket, but can't preview it at the provider level before execution.
  Logging still works; pre-flight approval does not.
- **No schema validation at the provider level.** CLI args are strings.
  Validation moves to the binary, errors surface as stderr text.
- **Binary distribution.** A compiled binary (Go/Rust) is dependency-free
  inside bwrap but adds a build/cross-compile step to a Python project.
  A Python script avoids this but requires Python in the sandbox.
- **Prompt cost unchanged.** Agents still need tool descriptions in the
  system prompt — they won't reliably `--help` before first use.

### Recommendation

Keep MCP/function calling as the primary path for providers that support
structured tool protocols. Build the CLI binary as a **fallback** for
providers that only have shell access. The daemon socket protocol supports
both — only the client differs.

## Open Questions

- How replies are injected — provider-specific mechanism (e.g. new subprocess
  turn, API message append, etc.).
- Rate limiting / abuse prevention for tool calls.
