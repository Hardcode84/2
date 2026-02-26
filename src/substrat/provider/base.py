"""Agent provider protocol — the interface all LLM/agent backends implement."""

from collections.abc import AsyncGenerator
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProviderSession(Protocol):
    """A live conversation handle returned by a provider."""

    def send(self, message: str) -> AsyncGenerator[str, None]:
        """Send a message and yield streamed response chunks."""
        ...

    async def suspend(self) -> bytes:
        """Serialize session state. Returns opaque blob for later restore."""
        ...

    async def stop(self) -> None:
        """Terminate the session and release resources."""
        ...


@runtime_checkable
class AgentProvider(Protocol):
    """Factory for provider sessions.

    Each provider type (cursor-agent, claude-cli, etc.) implements this once.
    Sessions are the per-agent conversation handles it produces.
    """

    @property
    def name(self) -> str:
        """Provider type identifier (e.g. "cursor-agent", "claude-cli")."""
        ...

    async def create(self, model: str, system_prompt: str) -> ProviderSession:
        """Start a new conversation with the given model and instructions."""
        ...

    async def restore(self, state: bytes) -> ProviderSession:
        """Recreate a session from a previously suspended state blob."""
        ...
