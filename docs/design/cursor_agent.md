# cursor-agent CLI Protocol

Based on `cursor-agent` version `2026.02.13-41ac335`.

## Invocation

Headless mode with JSON streaming:

```
cursor-agent --print --output-format stream-json --trust \
    --model <model> --workspace <path> "<prompt>"
```

Key flags:
- `--print` — non-interactive, stdout-only. Has access to all tools.
- `--output-format stream-json` — newline-delimited JSON events on stdout.
- `--stream-partial-output` — emit text deltas as they arrive (optional).
- `--trust` — skip workspace trust prompt (required for headless).
- `--model <id>` — model selection (e.g. `sonnet-4.6`, `opus-4.6`).
- `--workspace <path>` — working directory for the agent.
- `--resume <session-id>` — resume an existing chat session.
- `--force` / `--yolo` — auto-approve tool calls.

## Session Management

- `cursor-agent create-chat` — pre-create a chat, returns UUID on stdout.
- `cursor-agent --resume <id>` — resume a previously created/used chat.
- `cursor-agent ls` — interactive session picker.
- `cursor-agent resume` — resume latest session.

Sessions are server-side. The session ID is the only state needed to resume.

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

cursor-agent sessions are server-side. To suspend, just store the `session_id`
string. To restore, pass `--resume <session_id>` on the next invocation. No
local state management needed.

## Open Questions

- How to inject custom tools (needed for agent messaging).
- MCP server integration — `cursor-agent mcp` subcommand exists, investigate.
- Behavior inside bwrap with restricted/no network.
