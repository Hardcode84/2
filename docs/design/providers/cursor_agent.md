# cursor-agent CLI Protocol

Based on `cursor-agent` version `2026.02.13-41ac335`.

## Invocation

Headless mode with JSON streaming:

```
cursor-agent --print --output-format stream-json --trust --approve-mcps \
    --model <model> --workspace <path> "<prompt>"
```

Key flags:
- `--print` — non-interactive, stdout-only. Has access to all tools.
- `--output-format stream-json` — newline-delimited JSON events on stdout.
- `--stream-partial-output` — emit text deltas as they arrive (optional).
- `--trust` — skip workspace trust prompt (required for headless).
- `--approve-mcps` — auto-approve all MCP servers (required for headless).
- `--model <id>` — model selection (e.g. `sonnet-4.6`, `opus-4.6`).
- `--workspace <path>` — working directory for the agent.
- `--resume <session-id>` — resume an existing chat session.
- `--force` / `--yolo` — auto-approve tool calls.

## Session Management

- `cursor-agent create-chat` — pre-create a chat, returns UUID on stdout.
- `cursor-agent --resume <id>` — resume a previously created/used chat.
- `cursor-agent ls` — interactive session picker.
- `cursor-agent resume` — resume latest session.

### Local storage

Sessions are stored locally as SQLite databases, not server-side:

```
~/.cursor/chats/<workspace-hash>/<session-uuid>/store.db
```

The `store.db` contains two tables:
- `meta` — hex-encoded JSON with agent metadata (agentId, name, mode,
  latestRootBlobId).
- `blobs` — content-addressed blob store (Merkle-tree-like). Stores user
  messages, assistant responses, and internal state as `{id: TEXT, data: BLOB}`.

The conversation history is a chain of blobs linked by hashes, with the root
pointer in `meta`. cursor-agent sends messages to Cursor's API for inference
but persists all state locally.

Implications:
- `--resume` works after process death — SQLite survives crashes (WAL mode).
- The `~/.cursor/chats/` directory must be accessible to cursor-agent at
  runtime. Inside bwrap, this needs a bind mount.
- The session UUID alone is sufficient to resume, but only on the same machine
  with the same `~/.cursor/chats/` directory.

## MCP Integration

cursor-agent discovers MCP servers from `.cursor/mcp.json` in the workspace.

### Config format

```json
{
  "mcpServers": {
    "server-name": {
      "command": "/path/to/binary",
      "args": ["--flag", "value"],
      "env": {"KEY": "value"}
    }
  }
}
```

Transport is stdio: cursor-agent spawns the command as a child process and
speaks MCP over stdin/stdout. Also supports `"url"` for streamable-http.

### Management CLI

```
agent mcp list                  # Show configured servers + status.
agent mcp list-tools <name>     # List tools exposed by a server.
agent mcp enable <name>         # Pre-approve a server.
agent mcp disable <name>        # Disable a server.
agent mcp login <name>          # OAuth auth for a server.
```

No `add`/`remove` — edit `.cursor/mcp.json` directly.

### Approval

MCP servers require approval before cursor-agent connects. Approval state is
stored per-project at:

```
~/.cursor/projects/<workspace-slug>/mcp-approvals.json   # ["name-<hash>", ...]
~/.cursor/projects/<workspace-slug>/mcp-disabled.json     # [...]
```

The approval hash is derived from command + args. For headless use,
`--approve-mcps` bypasses approval entirely (only during agent runs, not
with `mcp list`).

### Substrat MCP server registration

The daemon generates `.cursor/mcp.json` pointing at the Substrat MCP server:

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

For bwrap workspaces, this config is a read-only bind mount from a
daemon-managed location:

```
~/.substrat/agents/<uuid>/mcp.json  →  <workspace>/.cursor/mcp.json (RO)
```

This avoids writing into the sandbox and lets the daemon regenerate the config
without touching the workspace filesystem. The MCP server binary must be
accessible inside the sandbox (bind-mounted or on a shared read-only path).

The MCP server process runs inside the sandbox (spawned by cursor-agent as a
child) but connects back to the daemon socket, which is bind-mounted into the
sandbox. This is the only network-like hole in the sandbox wall.

### Limits

cursor-agent caps tools at 40 across all MCP servers combined.

## Output Protocol

Each line on stdout is a self-contained JSON object. Event types:

### `system` (init)

Emitted once at the start.

```json
{
  "type": "system",
  "subtype": "init",
  "apiKeySource": "login",
  "cwd": "/path/to/workspace",
  "session_id": "uuid",
  "model": "Claude 4.6 Sonnet",
  "permissionMode": "default"
}
```

### `user` (echo)

Echo of the user message sent.

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [{"type": "text", "text": "the prompt"}]
  },
  "session_id": "uuid"
}
```

### `assistant` (response)

Agent response. Two variants:

**Partial delta** (only with `--stream-partial-output`): has `timestamp_ms`.

```json
{
  "type": "assistant",
  "message": {"role": "assistant", "content": [{"type": "text", "text": "delta"}]},
  "session_id": "uuid",
  "timestamp_ms": 1772104286759
}
```

**Final message**: no `timestamp_ms`. Contains the full response text.

```json
{
  "type": "assistant",
  "message": {"role": "assistant", "content": [{"type": "text", "text": "full response"}]},
  "session_id": "uuid"
}
```

### `result` (completion)

Emitted once at the end.

```json
{
  "type": "result",
  "subtype": "success",
  "duration_ms": 3393,
  "duration_api_ms": 3393,
  "is_error": false,
  "result": "full response text",
  "session_id": "uuid",
  "request_id": "uuid"
}
```

On error: `"is_error": true`, `"result"` contains the error message.

## Suspend / Restore

To suspend, store the session UUID and workspace path. To restore, pass
`--resume <session_id>` on the next invocation. All conversation state is in
the local SQLite database — no server-side state to worry about.

## Open Questions

- Whether `--workspace` controls the chat storage path or only the agent's
  working directory (affects bwrap bind mount strategy).
- Behavior inside bwrap with restricted/no network (cursor-agent needs API
  access for inference — network cannot be fully blocked).
- MCP server process lifecycle inside bwrap — does seccomp affect the daemon
  socket connection?
