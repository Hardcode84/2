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
    bind_mounts: Sequence[LinkSpec]    # Host dirs needed inside sandbox.
    async def create(self, model: str, system_prompt: str) -> ProviderSession: ...
    async def restore(self, state: bytes) -> ProviderSession: ...
```

`AgentProvider` is a factory. It knows how to create new sessions and restore
suspended ones from opaque state blobs. `ProviderSession` is the per-agent
conversation handle — send messages, suspend, stop.

`bind_mounts` is a class-level constant — host directories the provider needs
inside the bwrap sandbox (session storage, config dirs, etc.). The daemon
queries this at startup and merges with workspace links when building bwrap
commands. See [workspace.md](workspace.md) for details.

The split keeps session identity (UUIDs, state machines) out of the provider.
Providers don't know about Substrat's session model.

## Providers

Provider-specific protocol details live in `providers/`. Current:

- **cursor-agent** (`providers/cursor_agent.md`) — CLI subprocess, local
  session storage, MCP tool integration.

Planned:

- **Claude CLI** — subprocess pattern, `--resume` for native persistence.
- **OpenRouter API** — HTTP streaming, client-side conversation history.
