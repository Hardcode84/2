# Tool Integration

How Substrat exposes custom tools (messaging, spawning, etc.) to agents
running inside cursor-agent.

## Mechanism: MCP

cursor-agent discovers MCP servers from `.cursor/mcp.json` in the workspace
root. We run a Substrat MCP server (stdio transport) that exposes our tools.
cursor-agent spawns it as a child process and communicates over stdin/stdout
using the Model Context Protocol.

### Registration

The daemon writes `.cursor/mcp.json` into each agent's workspace before
spawning cursor-agent:

```json
{
  "mcpServers": {
    "substrat": {
      "command": "/path/to/substrat-mcp-server",
      "args": ["--agent-id", "<uuid>"],
      "env": {
        "SUBSTRAT_SOCKET": "/path/to/daemon.sock"
      }
    }
  }
}
```

cursor-agent reads this on startup, spawns our server as a child process, and
makes its tools available to the agent natively.

### Headless invocation

For `--print` mode (our use case), two flags are required:

- `--trust` — skip interactive workspace trust prompt.
- `--approve-mcps` — auto-approve all configured MCP servers.

Without `--approve-mcps`, cursor-agent will refuse to connect to unapproved
servers in headless mode. The flag only takes effect during agent runs (not
with `mcp list` or other management subcommands).

### Agent identity

Each agent gets its own MCP server instance (spawned by cursor-agent as a
child). The agent's identity is baked into the server's args at config
generation time — no runtime context passing needed. The server connects back
to the daemon over `SUBSTRAT_SOCKET` and identifies itself with the agent UUID.

### MCP management CLI

cursor-agent exposes `agent mcp {list,list-tools,enable,disable,login}` for
managing servers. We don't use these — Substrat owns the config file and
passes `--approve-mcps` to bypass the approval flow.

### Limits

cursor-agent caps tools at 40 across all MCP servers combined. Our tool
catalog is small (5–6 tools), so this is not a concern unless agents also use
third-party MCP servers.

## Workspace and bwrap Integration

Each agent's workspace is a bwrap sandbox. The daemon generates
`.cursor/mcp.json` with the correct paths and places it in the workspace.

For bwrap sandboxes, the MCP config can be a read-only bind mount (or
symlink) from a daemon-managed location:

```
~/.substrat/agents/<uuid>/mcp.json  →  <workspace>/.cursor/mcp.json (RO)
```

This avoids writing into the sandbox and lets the daemon update the config
without touching the workspace filesystem. The MCP server binary itself must
be accessible inside the sandbox — either bind-mounted or on a shared
read-only path.

The MCP server process runs inside the sandbox (spawned by cursor-agent as a
child), but connects back to the daemon socket which is bind-mounted into the
sandbox. This is the only network-like hole in the sandbox wall.

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
original agent (spawning a fresh subprocess via `--resume`):
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

- How replies are injected — as a new `send()` call via `--resume`, or is
  there a way to append to the conversation without a fresh subprocess?
- Rate limiting / abuse prevention for tool calls.
- Whether the MCP server process inherits bwrap's seccomp filters or needs
  special handling for the daemon socket connection.
- Graceful behavior when cursor-agent hits the 40-tool limit (unlikely with
  our catalog alone, but possible if combined with user MCP servers).
