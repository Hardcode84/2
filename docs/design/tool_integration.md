# Tool Integration

How Substrat exposes custom tools (messaging, spawning, etc.) to agents
running inside cursor-agent.

## Mechanism: MCP

cursor-agent supports MCP servers (`cursor-agent mcp` subcommand). We run a
Substrat MCP server that exposes our tools. cursor-agent discovers and calls
them natively during execution.

Each cursor-agent subprocess connects to the MCP server on startup. The server
is per-daemon (not per-agent) — all agents talk to the same server, identified
by their session ID passed via tool call context.

TODO: investigate `cursor-agent mcp` subcommand, figure out how to register a
local MCP server for a session.

## Execution Model: Non-blocking Tools Only

All MCP tools return immediately. No tool call ever blocks the cursor-agent
subprocess. This is a hard rule — it prevents deadlocks and eliminates the
need for special slot accounting.

### Why not block?

If a tool call blocks (e.g. sync message waiting for reply), the cursor-agent
subprocess stays alive, holding a multiplexer slot. If the recipient also needs
a slot and none are free → deadlock. Blocking also complicates lifecycle
management (what if we need to suspend a blocked agent?).

### How "synchronous" messaging works without blocking

From the agent's perspective, a sync message is a two-turn pattern:

**Turn 1**: agent calls `send_message(recipient, text, sync=true)`.
MCP tool returns immediately:
```json
{"status": "sent", "message_id": "uuid", "waiting_for_reply": true}
```
The cursor-agent subprocess finishes its turn and exits. Slot is freed.

**Turn 2**: when the recipient replies, the daemon sends a new message to the
original agent (spawning a fresh subprocess):
```
Reply from <recipient> (to message <uuid>):
<reply text>
```
The agent continues from there.

The agent does not need to explicitly poll or manage this — the daemon
orchestrates the two-turn flow. From the agent's perspective, it "sent a
message and got a reply," just not within a single tool call.

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

These are cursor-agent's native tools — no need to reimplement. They operate
within the bwrap workspace naturally.

## Open Questions

- How exactly to register the MCP server with cursor-agent (per-session config?
  global config? command-line flag?).
- How to pass agent identity context to MCP tool calls (which agent is calling?).
- Whether cursor-agent supports MCP tool call in `--print` mode.
- How replies are injected — as a new `send()` call, or is there a way to
  append to the conversation without a fresh subprocess?
- Rate limiting / abuse prevention for tool calls.
