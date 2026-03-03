# Daemon Design

The daemon is the composition root — it creates and owns every component,
serves UDS requests, and manages process lifecycle.

---

## Composition

`Daemon.__init__()` builds the full stack:

```
SessionStore(root / "agents")
    → SessionMultiplexer(store, max_slots)
        → TurnScheduler(providers, mux, store, log_root)
            → Orchestrator(scheduler, default_provider, default_model)
```

Providers are injected. Production default: `{"cursor-agent": CursorAgentProvider()}`.
Tests inject `FakeProvider`.

---

## UDS Server

`asyncio.start_unix_server` on `root/daemon.sock`. One request per connection,
one asyncio task per connection. No persistent connections (that's `agent attach`,
deferred).

### Wire Protocol

Newline-delimited JSON, shared module `rpc.py`:

```
→ {"id": "req-N", "method": "...", "params": {...}}\n
← {"id": "req-N", "result": {...}}\n
← {"id": "req-N", "error": {"code": N, "message": "..."}}\n
```

`sync_call()` for blocking callers (CLI, MCP server).
`async_call()` for async callers (integration tests).

---

## Request Dispatch

Handler methods registered in `self._handlers: dict[str, Callable]`.

| Method | Handler | Delegates to |
|--------|---------|-------------|
| `agent.create` | `_handle_agent_create` | `orch.create_root_agent()` |
| `agent.list` | `_handle_agent_list` | Walk tree from roots |
| `agent.send` | `_handle_agent_send` | `orch.run_turn()` |
| `agent.inspect` | `_handle_agent_inspect` | Tree + inbox queries |
| `agent.terminate` | `_handle_agent_terminate` | `orch.terminate_agent()` |
| `tool.call` | `_handle_tool_call` | `orch.get_handler().method()` |

### Error Codes

```
ERR_NOT_FOUND = 1   # Agent/session not found.
ERR_INVALID   = 2   # Bad params, state error, name collision.
ERR_INTERNAL  = 3   # Unexpected exception.
ERR_METHOD    = 4   # Unknown RPC method.
```

Handler exceptions caught at `_handle_connection()`, mapped to error envelopes
by exception type: `KeyError → NOT_FOUND`, `ValueError/TypeError → INVALID`,
`Exception → INTERNAL`.

---

## tool.call Concurrency

When an agent is mid-turn (`orch.run_turn` awaits `ps.send()`), cursor-agent
may call Substrat tools via MCP → daemon UDS. The `tool.call` RPC arrives on a
separate connection, handled by a separate asyncio task. Works because the
`run_turn` task is suspended at `await`. ToolHandler methods are synchronous.
Deferred spawn work is drained by `run_turn` after the provider finishes.

---

## Lifecycle

1. **start()** — `mkdir`, `_cleanup_stale()`, `orch.recover()`,
   `start_unix_server()`, write PID file.
2. **serve** — asyncio event loop handles connections until signal.
3. **stop()** — close server, remove socket + PID file.

### Stale Socket Cleanup

On start, `_cleanup_stale()` checks the PID file:
- No PID file → remove orphaned socket if present.
- PID dead → remove socket + PID file.
- PID alive → raise `RuntimeError("already running")`.

### Signal Handling

`SIGTERM` and `SIGINT` trigger `stop()` via `asyncio.Event`.

---

## daemon_dispatch

`mcp_server.daemon_dispatch(socket_path, agent_id)` returns a `ToolDispatch`
callable that routes tool calls through `sync_call("tool.call", ...)`. Used by
the MCP server subprocess to call back into the daemon.
