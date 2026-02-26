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

### System prompt guidance

Agents are told in their system prompt:
- Tool calls return immediately with a status.
- Replies to sync messages arrive as your next message.
- Do not loop/poll waiting for replies.

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

```
Parameters: (none)

Returns:
  {"messages": [{"from": "name", "text": "...", "message_id": "uuid"}, ...]}
```

### `spawn_agent`

Create a subagent.

```
Parameters:
  name: str
  instructions: str
  role: str = "worker"             # "worker" | "reviewer".
  workspace_subdir: str | null     # Subdirectory of parent workspace.

Returns:
  {"status": "created", "agent_id": "uuid", "name": "str"}
```

### `inspect_agent`

View a subordinate's recent activity.

```
Parameters:
  name: str

Returns:
  {"state": "idle|busy|waiting", "recent_messages": [...]}
```

TODO: define what "recent activity" actually means.

### `read_file` / `write_file`

These are provider-native tools — no need to reimplement. They operate within
the agent's workspace naturally.

## Open Questions

- How replies are injected — provider-specific mechanism (e.g. new subprocess
  turn, API message append, etc.).
- Rate limiting / abuse prevention for tool calls.
