# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent provider protocol — the interface all LLM/agent backends implement."""

from collections.abc import AsyncGenerator
from typing import Protocol, runtime_checkable

from substrat.logging.event_log import EventLog


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
    The caller owns the EventLog and passes it in — providers never create logs.
    """

    @property
    def name(self) -> str:
        """Provider type identifier (e.g. "cursor-agent", "claude-cli")."""
        ...

    async def create(
        self,
        model: str,
        system_prompt: str,
        log: EventLog | None = None,
    ) -> ProviderSession:
        """Start a new conversation with the given model and instructions."""
        ...

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
    ) -> ProviderSession:
        """Recreate a session from a previously suspended state blob."""
        ...
