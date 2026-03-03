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

If a tool call blocks (e.g. sync message waiting for reply), the agent process
stays alive, holding a multiplexer slot. If the recipient also needs a slot
and none are free → deadlock. Blocking also complicates lifecycle management
(what if we need to suspend a blocked agent?).

### How "synchronous" messaging works without blocking

From the agent's perspective, a sync message is a two-turn pattern:

**Turn 1**: agent calls `send_message(recipient, text, sync=true)`.
MCP tool returns immediately:
```json
{"status": "sent", "message_id": "uuid", "waiting_for_reply": true}
```
The agent process finishes its turn and exits. Slot is freed.

**Turn 2**: when the recipient replies, the daemon delivers the reply to the
original agent (via the provider's normal message injection mechanism):
```
Reply from <recipient> (to message <uuid>):
<reply text>
```
The agent continues from there.

The agent does not need to explicitly poll or manage this — the daemon
orchestrates the two-turn flow.

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
- Replies to sync messages arrive as your next message.
- Spawned agents start working after your current turn ends.
- Do not loop/poll waiting for replies.
- Messages from other agents wake you automatically.
- Call `complete(result)` when your work is done.

## Tool Catalog

All tools return JSON. All tools are non-blocking.

### `send_message`

Send a message to another agent (parent, child, or sibling).

```
Parameters:
  recipient: str       # Agent name.
  text: str            # Message body.
  sync: bool = true    # If true, daemon will deliver reply as next message.

Returns:
  {"status": "sent", "message_id": "uuid", "waiting_for_reply": bool}
```

### `broadcast`

Multicast to all siblings in the team.

```
Parameters:
  text: str

Returns:
  {"status": "sent", "message_id": "uuid", "recipient_count": int}
```

Replies arrive as separate messages, one per respondent.

### `check_inbox`

Retrieve pending async messages (notifications, unsolicited messages).
Optional filters narrow which messages are collected; unmatched messages
remain in the inbox for later retrieval.

```
Parameters:
  sender: str | null    # Only return messages from this agent name.
  kind: str | null      # Only return messages of this kind
                        # (request, response, notification, multicast).

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

Returns:
  {"status": "accepted", "agent_id": "uuid", "name": "str"}
```

The workspace must exist (or be created inline). See
[workspace.md](workspace.md) for the full workspace tool catalog and the
inline convenience form.

Typical flow with sync messaging:

1. Parent calls `spawn_agent("analyst", ...)` + `send_message("analyst", "go",
   sync=true)` in the same turn. Both return immediately.
2. Parent's turn ends → slot released.
3. Daemon creates the analyst's provider session, delivers the queued message.
4. Analyst works, replies → daemon delivers reply to parent as Turn 2.

### `inspect_agent`

View a subordinate's recent activity.

```
Parameters:
  name: str

Returns:
  {"state": "idle|busy|waiting", "recent_messages": [...]}
```

TODO: define what "recent activity" actually means.

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

## Open Questions

- How replies are injected — provider-specific mechanism (e.g. new subprocess
  turn, API message append, etc.).
- Rate limiting / abuse prevention for tool calls.
