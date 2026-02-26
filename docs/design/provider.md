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
    async def create(self, model: str, system_prompt: str) -> ProviderSession: ...
    async def restore(self, state: bytes) -> ProviderSession: ...
```

`AgentProvider` is a factory. It knows how to create new sessions and restore
suspended ones from opaque state blobs. `ProviderSession` is the per-agent
conversation handle — send messages, suspend, stop.

The split keeps session identity (UUIDs, state machines) out of the provider.
Providers don't know about Substrat's session model.

## cursor-agent

CLI binary, each session spawns one subprocess. Communication via stdin/stdout
JSON lines. Stderr captured to log.

- `create()` — launch `cursor-agent` subprocess with model and system prompt flags.
- `send()` — write to stdin, yield streamed response lines from stdout.
- `suspend()` — cursor-agent manages its own state on disk. Return the session
  dir path as the opaque state blob.
- `restore()` — relaunch subprocess pointing at the saved session dir.
- `stop()` — send EOF to stdin, wait for exit, kill on timeout.

## Future providers

- **Claude CLI**: subprocess pattern. Uses `--resume <session-id>` for native
  persistence. `suspend()` serializes the Claude session ID. `restore()` passes
  it back via `--resume`. Server retains context.
- **OpenRouter API**: HTTP via `aiohttp`/`httpx`. `send()` streams from chat
  completions endpoint. `suspend()` serializes full conversation history (no
  server-side session). `restore()` reconstructs from that history.
