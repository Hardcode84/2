# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Session multiplexer — pooled LRU scheduler for provider sessions."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from substrat.logging.event_log import EventLog
from substrat.model import CommandWrapper
from substrat.provider.base import AgentProvider, ProviderSession
from substrat.session.model import Session, SessionState
from substrat.session.store import SessionStore

_log = logging.getLogger(__name__)

EvictCallback = Callable[[UUID, int], None]

DEFAULT_POOL = "default"


@dataclass
class _Pool:
    """Per-pool state: slot limit, live sessions, LRU queue, held set."""

    max_slots: int
    slots: dict[UUID, ProviderSession] = field(default_factory=dict)
    lru: list[UUID] = field(default_factory=list)
    held: set[UUID] = field(default_factory=set)


class SessionMultiplexer:
    """Manages concurrent ProviderSession slots across named pools.

    Each pool has its own slot limit and LRU eviction queue. Sessions
    mid-send are held (non-evictable). Idle sessions sit in their pool's
    LRU and get suspended when that pool runs out of slots.
    """

    def __init__(
        self,
        store: SessionStore,
        pools: dict[str, int],
    ) -> None:
        self._store = store
        self._pools: dict[str, _Pool] = {
            name: _Pool(max_slots=limit) for name, limit in pools.items()
        }
        self._session_pool: dict[UUID, str] = {}
        self.on_evict: EvictCallback | None = None

    def _get_pool(self, session_id: UUID) -> _Pool:
        """Look up pool for a known session."""
        name = self._session_pool[session_id]
        return self._pools[name]

    async def put(
        self,
        session_id: UUID,
        ps: ProviderSession,
        pool: str = DEFAULT_POOL,
    ) -> None:
        """Slot a freshly-created ProviderSession. Evicts LRU if full."""
        p = self._pools[pool]
        await self._ensure_slot(p)
        p.slots[session_id] = ps
        p.held.add(session_id)
        self._session_pool[session_id] = pool

    async def acquire(
        self,
        session: Session,
        provider: AgentProvider,
        log: EventLog | None = None,
        *,
        wrap_command: CommandWrapper | None = None,
        pool: str = DEFAULT_POOL,
    ) -> ProviderSession:
        """Get a live ProviderSession. Restores from suspension if needed.

        If already slotted: touch LRU, mark held, return.
        If SUSPENDED: evict LRU if full, restore via provider, activate, persist.
        Otherwise: raise ValueError (use put() for new sessions).
        """
        sid = session.id
        # Already slotted — find its pool via the lookup.
        if sid in self._session_pool:
            p = self._get_pool(sid)
            if sid in p.slots:
                self._touch(p, sid)
                p.held.add(sid)
                return p.slots[sid]
        if session.state != SessionState.SUSPENDED:
            raise ValueError(
                f"session {sid} is {session.state.value}, not suspended"
                " — use put() for new sessions"
            )
        p = self._pools[pool]
        await self._ensure_slot(p)
        ps = await provider.restore(
            session.provider_state, log=log, wrap_command=wrap_command
        )
        p.slots[sid] = ps
        p.held.add(sid)
        self._session_pool[sid] = pool
        session.activate()
        self._store.save(session)
        return ps

    async def release(self, session_id: UUID) -> None:
        """Mark session as evictable. Appends to pool's LRU tail."""
        if session_id not in self._session_pool:
            return
        p = self._get_pool(session_id)
        p.held.discard(session_id)
        if session_id in p.slots and session_id not in p.lru:
            p.lru.append(session_id)

    async def remove(self, session_id: UUID) -> None:
        """Remove from slots, call ps.stop(). No-op if not slotted."""
        if session_id not in self._session_pool:
            return
        p = self._get_pool(session_id)
        if session_id not in p.slots:
            return
        ps = p.slots.pop(session_id)
        p.held.discard(session_id)
        if session_id in p.lru:
            p.lru.remove(session_id)
        del self._session_pool[session_id]
        await ps.stop()

    async def _ensure_slot(self, pool: _Pool) -> None:
        """Evict LRU from pool if at capacity. Raises if all held."""
        if len(pool.slots) < pool.max_slots:
            return
        if not pool.lru:
            raise RuntimeError(f"all {pool.max_slots} slots held, cannot evict")
        victim = pool.lru[0]
        await self._evict(pool, victim)

    async def _evict(self, pool: _Pool, session_id: UUID) -> None:
        """Suspend provider, persist state via store, stop provider."""
        ps = pool.slots[session_id]
        state_blob = await ps.suspend()
        del pool.slots[session_id]
        pool.lru.remove(session_id)
        pool.held.discard(session_id)
        del self._session_pool[session_id]
        session = self._store.load(session_id)
        session.suspend(state_blob)
        self._store.save(session)
        if self.on_evict is not None:
            self.on_evict(session_id, len(state_blob))
        try:
            await ps.stop()
        except Exception:
            _log.warning(
                "stop() failed during eviction of %s", session_id, exc_info=True
            )

    def _touch(self, pool: _Pool, session_id: UUID) -> None:
        """Remove from LRU (session is being held)."""
        if session_id in pool.lru:
            pool.lru.remove(session_id)

    @property
    def active_count(self) -> int:
        """Total sessions currently in slots across all pools."""
        return sum(len(p.slots) for p in self._pools.values())

    def contains(self, session_id: UUID) -> bool:
        """Whether a session is currently slotted in any pool."""
        return session_id in self._session_pool
