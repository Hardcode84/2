# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Turn scheduler — orchestrates session lifecycle and turn execution."""

from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from uuid import UUID

from substrat.logging import EventLog
from substrat.provider.base import AgentProvider
from substrat.session.model import Session
from substrat.session.multiplexer import SessionMultiplexer
from substrat.session.store import SessionStore

DeferredCallback = Callable[[], Coroutine[Any, Any, None]]


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
    ) -> None:
        self._providers = providers
        self._mux = mux
        self._store = store
        self._log_root = log_root
        self._sessions: dict[UUID, Session] = {}
        self._logs: dict[UUID, EventLog] = {}
        self._deferred: deque[DeferredCallback] = deque()
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

    def restore_session(self, session: Session) -> None:
        """Load an existing session into the scheduler's cache and open its log.

        Used during recovery. No provider session created — that happens
        on next send_turn via mux acquire.
        """
        self._sessions[session.id] = session
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
        model: str,
        system_prompt: str,
    ) -> Session:
        """Create a provider session, slot it, persist, and release."""
        if provider_name not in self._providers:
            raise ValueError(f"unknown provider: {provider_name}")
        provider = self._providers[provider_name]

        session = Session(provider_name=provider_name, model=model)

        log: EventLog | None = None
        if self._log_root is not None:
            log = EventLog(
                self._log_root / session.id.hex / "events.jsonl",
                context={"session_id": session.id.hex},
            )
            log.open()

        ps = await provider.create(model, system_prompt, log=log)
        await self._mux.put(session.id, ps)
        session.activate()
        self._store.save(session)
        await self._mux.release(session.id)

        self._sessions[session.id] = session
        if log is not None:
            self._logs[session.id] = log
        return session

    async def send_turn(self, session_id: UUID, prompt: str) -> str:
        """Acquire slot, send prompt, release, drain deferred, return response."""
        session = self._sessions[session_id]
        provider = self._providers[session.provider_name]
        log = self._logs.get(session_id)

        if log is not None:
            log.log("turn.start", {"prompt": prompt})

        # Resync with store if the mux evicted this session behind our back.
        was_suspended = not self._mux.contains(session_id)
        if was_suspended:
            session = self._store.load(session_id)
            self._sessions[session_id] = session

        ps = await self._mux.acquire(session, provider, log=log)

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

        while self._deferred:
            await self._deferred.popleft()()

        return response

    async def terminate_session(self, session_id: UUID) -> None:
        """Remove from mux, terminate state, persist, cleanup."""
        session = self._sessions[session_id]
        await self._mux.remove(session_id)
        session.terminate()
        self._store.save(session)

        log = self._logs.pop(session_id, None)
        if log is not None:
            log.close()
        del self._sessions[session_id]

    def defer(self, callback: DeferredCallback) -> None:
        """Enqueue work to run after the current turn releases its slot."""
        self._deferred.append(callback)
