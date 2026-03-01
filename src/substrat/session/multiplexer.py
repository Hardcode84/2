# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Session multiplexer — fixed-slot LRU scheduler for provider sessions."""

from collections.abc import Callable
from uuid import UUID

from substrat.logging.event_log import EventLog
from substrat.provider.base import AgentProvider, ProviderSession
from substrat.session.model import Session, SessionState
from substrat.session.store import SessionStore

EvictCallback = Callable[[UUID, int], None]


class SessionMultiplexer:
    """Manages a fixed number of concurrent ProviderSession slots.

    Sessions mid-send are held (non-evictable). Idle sessions sit in an LRU
    queue and get suspended when slots run out.
    """

    def __init__(self, store: SessionStore, max_slots: int = 4) -> None:
        self._store = store
        self._max_slots = max_slots
        self._slots: dict[UUID, ProviderSession] = {}
        self._lru: list[UUID] = []  # Released sessions, head = next victim.
        self._held: set[UUID] = set()  # Acquired, not evictable.
        self.on_evict: EvictCallback | None = None

    async def put(self, session_id: UUID, ps: ProviderSession) -> None:
        """Slot a freshly-created ProviderSession. Evicts LRU if full."""
        await self._ensure_slot()
        self._slots[session_id] = ps
        self._held.add(session_id)

    async def acquire(
        self,
        session: Session,
        provider: AgentProvider,
        log: EventLog | None = None,
    ) -> ProviderSession:
        """Get a live ProviderSession. Restores from suspension if needed.

        If already slotted: touch LRU, mark held, return.
        If SUSPENDED: evict LRU if full, restore via provider, activate, persist.
        Otherwise: raise ValueError (use put() for new sessions).
        """
        sid = session.id
        if sid in self._slots:
            self._touch(sid)
            self._held.add(sid)
            return self._slots[sid]
        if session.state != SessionState.SUSPENDED:
            raise ValueError(
                f"session {sid} is {session.state.value}, not suspended"
                " — use put() for new sessions"
            )
        await self._ensure_slot()
        ps = await provider.restore(session.provider_state, log=log)
        self._slots[sid] = ps
        self._held.add(sid)
        session.activate()
        self._store.save(session)
        return ps

    async def release(self, session_id: UUID) -> None:
        """Mark session as evictable. Appends to LRU tail."""
        self._held.discard(session_id)
        if session_id in self._slots and session_id not in self._lru:
            self._lru.append(session_id)

    async def remove(self, session_id: UUID) -> None:
        """Remove from slots, call ps.stop(). No-op if not slotted."""
        if session_id not in self._slots:
            return
        ps = self._slots.pop(session_id)
        self._held.discard(session_id)
        if session_id in self._lru:
            self._lru.remove(session_id)
        await ps.stop()

    async def _ensure_slot(self) -> None:
        """Evict LRU if at capacity. Raises if all held."""
        if len(self._slots) < self._max_slots:
            return
        if not self._lru:
            raise RuntimeError(f"all {self._max_slots} slots held, cannot evict")
        victim = self._lru[0]
        await self._evict(victim)

    async def _evict(self, session_id: UUID) -> None:
        """Suspend provider, persist state via store, stop provider."""
        ps = self._slots.pop(session_id)
        self._lru.remove(session_id)
        self._held.discard(session_id)
        state_blob = await ps.suspend()
        session = self._store.load(session_id)
        session.suspend(state_blob)
        self._store.save(session)
        if self.on_evict is not None:
            self.on_evict(session_id, len(state_blob))
        await ps.stop()

    def _touch(self, session_id: UUID) -> None:
        """Remove from LRU (session is being held)."""
        if session_id in self._lru:
            self._lru.remove(session_id)

    @property
    def active_count(self) -> int:
        """Number of sessions currently in slots."""
        return len(self._slots)

    def contains(self, session_id: UUID) -> bool:
        """Whether a session is currently slotted."""
        return session_id in self._slots
