# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the session multiplexer."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from substrat.provider.base import ProviderSession
from substrat.session import Session, SessionState, SessionStore
from substrat.session.multiplexer import SessionMultiplexer

# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Tracks suspend/stop calls for assertion."""

    def __init__(self, state: bytes = b"blob") -> None:
        self._state = state
        self.suspended = False
        self.stopped = False

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        yield f"echo: {message}"

    async def suspend(self) -> bytes:
        self.suspended = True
        return self._state

    async def stop(self) -> None:
        self.stopped = True


class FakeProvider:
    """Records restore calls so tests can inspect them."""

    def __init__(self) -> None:
        self.restore_calls: list[bytes] = []

    @property
    def name(self) -> str:
        return "fake"

    async def create(self, model: str, system_prompt: str) -> FakeProviderSession:
        return FakeProviderSession()

    async def restore(self, state: bytes) -> FakeProviderSession:
        self.restore_calls.append(state)
        return FakeProviderSession(state)


# -- Helpers ----------------------------------------------------------------


def _make_session(**kwargs: object) -> Session:
    defaults: dict[str, object] = {
        "provider_name": "fake",
        "model": "test",
    }
    defaults.update(kwargs)
    return Session(**defaults)  # type: ignore[arg-type]


def _suspended_session(store: SessionStore) -> Session:
    """Create, persist, activate, suspend, and persist a session."""
    s = _make_session()
    store.save(s)
    s.activate()
    s.suspend(b"saved-state")
    store.save(s)
    return s


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path)


@pytest.fixture()
def mux(store: SessionStore) -> SessionMultiplexer:
    return SessionMultiplexer(store, max_slots=2)


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider()


# -- put --------------------------------------------------------------------


async def test_put_adds_session_to_slot(
    mux: SessionMultiplexer,
) -> None:
    ps = FakeProviderSession()
    sid = _make_session().id
    await mux.put(sid, ps)
    assert mux.contains(sid)
    assert mux.active_count == 1


async def test_put_evicts_lru_when_full(
    mux: SessionMultiplexer, store: SessionStore
) -> None:
    # Fill both slots, release them so they're evictable.
    s1, s2 = _make_session(), _make_session()
    store.save(s1)
    s1.activate()
    store.save(s1)
    store.save(s2)
    s2.activate()
    store.save(s2)
    ps1, ps2 = FakeProviderSession(), FakeProviderSession()
    await mux.put(s1.id, ps1)
    await mux.put(s2.id, ps2)
    await mux.release(s1.id)
    await mux.release(s2.id)
    # Third put evicts s1 (first released = LRU head).
    ps3 = FakeProviderSession()
    s3 = _make_session()
    await mux.put(s3.id, ps3)
    assert not mux.contains(s1.id)
    assert mux.contains(s2.id)
    assert mux.contains(s3.id)
    assert ps1.suspended
    assert ps1.stopped


# -- acquire ----------------------------------------------------------------


async def test_acquire_returns_cached_slot(
    mux: SessionMultiplexer, provider: FakeProvider
) -> None:
    s = _make_session()
    ps = FakeProviderSession()
    await mux.put(s.id, ps)
    await mux.release(s.id)
    got = await mux.acquire(s, provider)
    assert got is ps
    assert provider.restore_calls == []


async def test_acquire_restores_suspended_session(
    mux: SessionMultiplexer, store: SessionStore, provider: FakeProvider
) -> None:
    s = _suspended_session(store)
    ps = await mux.acquire(s, provider)
    assert isinstance(ps, ProviderSession)
    assert provider.restore_calls == [b"saved-state"]
    assert mux.contains(s.id)


async def test_acquire_activates_and_persists(
    mux: SessionMultiplexer, store: SessionStore, provider: FakeProvider
) -> None:
    s = _suspended_session(store)
    await mux.acquire(s, provider)
    assert s.state == SessionState.ACTIVE
    loaded = store.load(s.id)
    assert loaded.state == SessionState.ACTIVE


async def test_acquire_evicts_lru_then_restores(
    mux: SessionMultiplexer, store: SessionStore, provider: FakeProvider
) -> None:
    # Fill slots with two sessions.
    a, b = _make_session(), _make_session()
    store.save(a)
    a.activate()
    store.save(a)
    store.save(b)
    b.activate()
    store.save(b)
    await mux.put(a.id, FakeProviderSession())
    await mux.put(b.id, FakeProviderSession())
    await mux.release(a.id)
    await mux.release(b.id)
    # Acquire a suspended session — should evict a (LRU head).
    target = _suspended_session(store)
    await mux.acquire(target, provider)
    assert not mux.contains(a.id)
    assert mux.contains(b.id)
    assert mux.contains(target.id)


async def test_acquire_raises_when_all_held(
    mux: SessionMultiplexer, store: SessionStore, provider: FakeProvider
) -> None:
    # Fill both slots, don't release.
    await mux.put(_make_session().id, FakeProviderSession())
    await mux.put(_make_session().id, FakeProviderSession())
    target = _suspended_session(store)
    with pytest.raises(RuntimeError, match="all 2 slots held"):
        await mux.acquire(target, provider)


async def test_acquire_raises_for_created_session(
    mux: SessionMultiplexer, provider: FakeProvider
) -> None:
    s = _make_session()
    assert s.state == SessionState.CREATED
    with pytest.raises(ValueError, match="created.*not suspended"):
        await mux.acquire(s, provider)


async def test_acquire_raises_for_terminated_session(
    mux: SessionMultiplexer, provider: FakeProvider
) -> None:
    s = _make_session()
    s.activate()
    s.terminate()
    with pytest.raises(ValueError, match="terminated.*not suspended"):
        await mux.acquire(s, provider)


# -- release ----------------------------------------------------------------


async def test_release_makes_session_evictable(
    mux: SessionMultiplexer, store: SessionStore
) -> None:
    s = _make_session()
    store.save(s)
    s.activate()
    store.save(s)
    ps = FakeProviderSession()
    await mux.put(s.id, ps)
    await mux.release(s.id)
    # Fill remaining slot and put a third — should evict s.
    s2 = _make_session()
    await mux.put(s2.id, FakeProviderSession())
    s3 = _make_session()
    await mux.put(s3.id, FakeProviderSession())
    assert not mux.contains(s.id)
    assert ps.suspended


async def test_release_idempotent(mux: SessionMultiplexer) -> None:
    s = _make_session()
    await mux.put(s.id, FakeProviderSession())
    await mux.release(s.id)
    await mux.release(s.id)  # No error, no duplicate LRU entry.
    assert mux.active_count == 1


# -- LRU order -------------------------------------------------------------


async def test_lru_evicts_least_recently_released(
    mux: SessionMultiplexer, store: SessionStore
) -> None:
    a, b = _make_session(), _make_session()
    for s in (a, b):
        store.save(s)
        s.activate()
        store.save(s)
    ps_a, ps_b = FakeProviderSession(), FakeProviderSession()
    await mux.put(a.id, ps_a)
    await mux.put(b.id, ps_b)
    # Release a first, then b.
    await mux.release(a.id)
    await mux.release(b.id)
    # Next put evicts a (released first).
    await mux.put(_make_session().id, FakeProviderSession())
    assert not mux.contains(a.id)
    assert mux.contains(b.id)
    assert ps_a.suspended
    assert not ps_b.suspended


# -- remove -----------------------------------------------------------------


async def test_remove_calls_stop(mux: SessionMultiplexer) -> None:
    ps = FakeProviderSession()
    s = _make_session()
    await mux.put(s.id, ps)
    await mux.remove(s.id)
    assert ps.stopped
    assert not mux.contains(s.id)
    assert mux.active_count == 0


async def test_remove_noop_for_unknown(mux: SessionMultiplexer) -> None:
    await mux.remove(_make_session().id)  # No error.


# -- eviction ---------------------------------------------------------------


async def test_eviction_suspends_and_persists(
    mux: SessionMultiplexer, store: SessionStore
) -> None:
    s = _make_session()
    store.save(s)
    s.activate()
    store.save(s)
    ps = FakeProviderSession(state=b"evict-blob")
    await mux.put(s.id, ps)
    await mux.release(s.id)
    # Force eviction by filling the mux.
    await mux.put(_make_session().id, FakeProviderSession())
    await mux.put(_make_session().id, FakeProviderSession())
    assert ps.suspended
    assert ps.stopped
    loaded = store.load(s.id)
    assert loaded.state == SessionState.SUSPENDED
    assert loaded.provider_state == b"evict-blob"


# -- properties -------------------------------------------------------------


async def test_active_count_and_contains(mux: SessionMultiplexer) -> None:
    assert mux.active_count == 0
    s = _make_session()
    assert not mux.contains(s.id)
    await mux.put(s.id, FakeProviderSession())
    assert mux.active_count == 1
    assert mux.contains(s.id)
    await mux.remove(s.id)
    assert mux.active_count == 0
    assert not mux.contains(s.id)
