# Provider Abstraction

The provider layer abstracts LLM/agent backends behind two protocols:
`AgentProvider` (factory) and `ProviderSession` (conversation handle).

## Protocols

```python
class ProviderSession(Protocol):
    def send(self, message: str) -> AsyncGenerator[str, None]: ...
    async def suspend(self) -> bytes: ...
    async def stop(self) -> None: ...

class AgentProvider(Protocol):
    name: str
    async def create(self, model: str, system_prompt: str,
                     log: EventLog | None = None) -> ProviderSession: ...
    async def restore(self, state: bytes,
                      log: EventLog | None = None) -> ProviderSession: ...
```

`AgentProvider` is a factory. It knows how to create new sessions and restore
suspended ones from opaque state blobs. `ProviderSession` is the per-agent
conversation handle — send messages, suspend, stop.

The split keeps session identity (UUIDs, state machines) out of the provider.
Providers don't know about Substrat's session model.

## Command wrapping

Subprocess-based providers (`CursorAgentProvider`, future Claude CLI) accept an
optional `wrap_command` callback on `__init__`. The callback receives the raw
argv plus provider-declared bind mounts and env vars, and returns the final
argv to exec. The daemon builds a closure from `build_command` + workspace
config; the provider passes its own needs (session storage dirs, etc.) at
call time. No `bind_mounts` attribute on the protocol — the provider tells
the wrapper what it needs per invocation.

```python
CommandWrapper = Callable[
    [Sequence[str], Sequence[LinkSpec], Mapping[str, str]],
    Sequence[str],
]
```

One wrapper per provider instance, applied to both `create-chat` and `send`
subprocesses.

## Tool injection

Subprocess-based providers accept `tools: Sequence[ToolDef]` at construction.
Tools are forwarded to every session the provider creates. The MCP server
uses these definitions to build its tool catalog; `model.py` owns the
`ToolDef`/`ToolParam` types so neither the provider nor the MCP server
depends on the agent layer.

## Providers

Provider-specific protocol details live in `providers/`. Current:

- **cursor-agent** (`providers/cursor_agent.md`) — CLI subprocess, local
  session storage, MCP tool integration.

Planned:

- **Claude CLI** — subprocess pattern, `--resume` for native persistence.
- **OpenRouter API** — HTTP streaming, client-side conversation history.
