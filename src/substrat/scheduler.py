# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Turn scheduler — orchestrates session lifecycle and turn execution."""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import UUID

from substrat.logging import EventLog
from substrat.model import CommandWrapper
from substrat.provider.base import AgentProvider
from substrat.session.model import Session
from substrat.session.multiplexer import SessionMultiplexer
from substrat.session.store import SessionStore


class TurnScheduler:
    """Orchestrates turn execution across multiplexed sessions.

    Owns the in-memory session cache. The multiplexer handles slot
    management, the store handles persistence, providers handle the
    actual LLM communication. This layer ties them together.
    """

    def __init__(
        self,
        providers: dict[str, AgentProvider],
        mux: SessionMultiplexer,
        store: SessionStore,
        log_root: Path | None = None,
        daemon_socket: str | None = None,
    ) -> None:
        self._providers = providers
        self._mux = mux
        self._store = store
        self._log_root = log_root
        self._daemon_socket = daemon_socket
        self._sessions: dict[UUID, Session] = {}
        self._logs: dict[UUID, EventLog] = {}
        self._wrap_commands: dict[UUID, CommandWrapper | None] = {}
        self._mux.on_evict = self._on_session_evicted

    @property
    def store(self) -> SessionStore:
        """Public read-only access to the session store."""
        return self._store

    def log_event(
        self,
        session_id: UUID,
        event: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Log an event to a session's event log. KeyError if no log."""
        self._logs[session_id].log(event, data)

    def _on_session_evicted(self, session_id: UUID, state_size: int) -> None:
        """Evict callback — log suspend.result to the session's event log."""
        log = self._logs.get(session_id)
        if log is not None:
            log.log("suspend.result", {"state_size": state_size})

    def restore_session(
        self,
        session: Session,
        *,
        wrap_command: CommandWrapper | None = None,
    ) -> None:
        """Load an existing session into the scheduler's cache and open its log.

        Used during recovery. No provider session created — that happens
        on next send_turn via mux acquire.
        """
        self._sessions[session.id] = session
        self._wrap_commands[session.id] = wrap_command
        if self._log_root is not None:
            log = EventLog(
                self._log_root / session.id.hex / "events.jsonl",
                context={"session_id": session.id.hex},
            )
            log.open()
            self._logs[session.id] = log

    async def create_session(
        self,
        provider_name: str,
        model: str | None,
        system_prompt: str,
        *,
        workspace: Path | None = None,
        wrap_command: CommandWrapper | None = None,
        agent_id: UUID | None = None,
    ) -> Session:
        """Create a provider session, slot it, persist, and release."""
        if provider_name not in self._providers:
            raise ValueError(f"unknown provider: {provider_name}")
        provider = self._providers[provider_name]

        session = Session(provider_name=provider_name, model=model or "")

        log: EventLog | None = None
        if self._log_root is not None:
            log = EventLog(
                self._log_root / session.id.hex / "events.jsonl",
                context={"session_id": session.id.hex},
            )
            log.open()

        ps = await provider.create(
            model,
            system_prompt,
            log=log,
            workspace=workspace,
            wrap_command=wrap_command,
            agent_id=agent_id,
            daemon_socket=self._daemon_socket,
        )
        await self._mux.put(session.id, ps)
        session.activate()
        self._store.save(session)
        await self._mux.release(session.id)

        self._sessions[session.id] = session
        self._wrap_commands[session.id] = wrap_command
        if log is not None:
            self._logs[session.id] = log
        return session

    def _get_session(self, session_id: UUID) -> "Session":
        """Look up a registered session or raise with a useful message."""
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"unknown session: {session_id.hex}") from None

    def _get_provider(self, name: str) -> "AgentProvider":
        """Look up a registered provider or raise with a useful message."""
        try:
            return self._providers[name]
        except KeyError:
            raise KeyError(f"unknown provider: {name!r}") from None

    async def send_turn(self, session_id: UUID, prompt: str) -> str:
        """Acquire slot, send prompt, release, drain deferred, return response."""
        session = self._get_session(session_id)
        provider = self._get_provider(session.provider_name)
        log = self._logs.get(session_id)

        if log is not None:
            log.log("turn.start", {"prompt": prompt})

        # Resync with store if the mux evicted this session behind our back.
        was_suspended = not self._mux.contains(session_id)
        if was_suspended:
            session = self._store.load(session_id)
            self._sessions[session_id] = session

        wc = self._wrap_commands.get(session_id)
        ps = await self._mux.acquire(session, provider, log=log, wrap_command=wc)

        if was_suspended and log is not None:
            log.log(
                "session.restored",
                {"provider": session.provider_name, "model": session.model},
            )
        try:
            chunks: list[str] = []
            async for chunk in ps.send(prompt):
                chunks.append(chunk)
            response = "".join(chunks)
        finally:
            await self._mux.release(session_id)

        if log is not None:
            log.log("turn.complete", {"response": response})

        return response

    async def stream_turn(
        self, session_id: UUID, prompt: str
    ) -> AsyncGenerator[str, None]:
        """Acquire slot, stream prompt chunks, release. Mirrors send_turn."""
        session = self._get_session(session_id)
        provider = self._get_provider(session.provider_name)
        log = self._logs.get(session_id)

        if log is not None:
            log.log("turn.start", {"prompt": prompt})

        was_suspended = not self._mux.contains(session_id)
        if was_suspended:
            session = self._store.load(session_id)
            self._sessions[session_id] = session

        wc = self._wrap_commands.get(session_id)
        ps = await self._mux.acquire(session, provider, log=log, wrap_command=wc)

        if was_suspended and log is not None:
            log.log(
                "session.restored",
                {"provider": session.provider_name, "model": session.model},
            )

        chunks: list[str] = []
        try:
            async for chunk in ps.send(prompt):
                chunks.append(chunk)
                yield chunk
        finally:
            await self._mux.release(session_id)
            if log is not None:
                log.log("turn.complete", {"response": "".join(chunks)})

    async def terminate_session(self, session_id: UUID) -> None:
        """Remove from mux, terminate state, persist, cleanup."""
        session = self._get_session(session_id)
        await self._mux.remove(session_id)
        session.terminate()
        self._store.save(session)

        log = self._logs.pop(session_id, None)
        if log is not None:
            log.close()
        self._wrap_commands.pop(session_id, None)
        del self._sessions[session_id]
