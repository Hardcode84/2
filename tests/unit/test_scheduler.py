# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the turn scheduler."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest

from substrat.logging import EventLog
from substrat.scheduler import TurnScheduler
from substrat.session import SessionState, SessionStore
from substrat.session.multiplexer import SessionMultiplexer

# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Minimal provider session for testing."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self._chunks = chunks if chunks is not None else ["response"]
        self.stopped = False

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        for chunk in self._chunks:
            yield chunk

    async def suspend(self) -> bytes:
        return b"fake-state"

    async def stop(self) -> None:
        self.stopped = True


class ErrorProviderSession(FakeProviderSession):
    """Provider session whose send() always raises."""

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        raise RuntimeError("send failed")
        yield ""  # noqa: RUF027  # Unreachable, makes it a generator.


class FakeProvider:
    """Tracks create/restore calls."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self._chunks = chunks
        self._error_on_send = False

    @property
    def name(self) -> str:
        return "fake"

    async def create(
        self,
        model: str,
        system_prompt: str,
        log: EventLog | None = None,
    ) -> FakeProviderSession:
        if self._error_on_send:
            return ErrorProviderSession()
        return FakeProviderSession(self._chunks)

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
    ) -> FakeProviderSession:
        return FakeProviderSession(self._chunks)


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@pytest.fixture()
def mux(store: SessionStore) -> SessionMultiplexer:
    return SessionMultiplexer(store, max_slots=4)


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture()
def scheduler(
    provider: FakeProvider,
    mux: SessionMultiplexer,
    store: SessionStore,
) -> TurnScheduler:
    return TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
    )


# -- create_session ---------------------------------------------------------


async def test_create_session(
    scheduler: TurnScheduler,
    mux: SessionMultiplexer,
    store: SessionStore,
) -> None:
    session = await scheduler.create_session("fake", "test-model", "be helpful")
    assert session.state == SessionState.ACTIVE
    assert session.provider_name == "fake"
    assert session.model == "test-model"
    assert mux.contains(session.id)
    # Persisted to store.
    loaded = store.load(session.id)
    assert loaded.state == SessionState.ACTIVE


async def test_create_session_unknown_provider(
    scheduler: TurnScheduler,
) -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        await scheduler.create_session("nonexistent", "m", "p")


# -- send_turn --------------------------------------------------------------


async def test_send_turn(scheduler: TurnScheduler) -> None:
    session = await scheduler.create_session("fake", "m", "p")
    response = await scheduler.send_turn(session.id, "hello")
    assert response == "response"


async def test_send_turn_multi_chunk(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    provider = FakeProvider(chunks=["one", " two", " three"])
    sched = TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
    )
    session = await sched.create_session("fake", "m", "p")
    response = await sched.send_turn(session.id, "hello")
    assert response == "one two three"


async def test_send_turn_drains_deferred(scheduler: TurnScheduler) -> None:
    session = await scheduler.create_session("fake", "m", "p")
    called: list[bool] = []

    async def cb() -> None:
        called.append(True)

    scheduler.defer(cb)
    await scheduler.send_turn(session.id, "hello")
    assert called == [True]


async def test_deferred_runs_after_release(
    scheduler: TurnScheduler,
    mux: SessionMultiplexer,
) -> None:
    session = await scheduler.create_session("fake", "m", "p")
    order: list[str] = []

    original_release = mux.release

    async def tracked_release(sid: UUID) -> None:
        order.append("release")
        await original_release(sid)

    mux.release = tracked_release  # type: ignore[assignment]

    async def cb() -> None:
        order.append("deferred")

    scheduler.defer(cb)
    await scheduler.send_turn(session.id, "hello")
    assert order == ["release", "deferred"]


# -- terminate_session ------------------------------------------------------


async def test_terminate_session(
    scheduler: TurnScheduler,
    mux: SessionMultiplexer,
    store: SessionStore,
) -> None:
    session = await scheduler.create_session("fake", "m", "p")
    await scheduler.terminate_session(session.id)
    assert not mux.contains(session.id)
    loaded = store.load(session.id)
    assert loaded.state == SessionState.TERMINATED


# -- logging ----------------------------------------------------------------


async def test_send_turn_logs_events(
    store: SessionStore,
    mux: SessionMultiplexer,
    tmp_path: Path,
) -> None:
    log_root = tmp_path / "logs"
    provider = FakeProvider()
    sched = TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
        log_root=log_root,
    )
    session = await sched.create_session("fake", "m", "p")
    await sched.send_turn(session.id, "hello")

    log_file = log_root / session.id.hex / "events.jsonl"
    assert log_file.exists()
    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    event_names = [e["event"] for e in events]
    assert "turn.start" in event_names
    assert "turn.complete" in event_names
    start = next(e for e in events if e["event"] == "turn.start")
    assert start["data"]["prompt"] == "hello"
    complete = next(e for e in events if e["event"] == "turn.complete")
    assert complete["data"]["response"] == "response"


# -- suspension/restore ----------------------------------------------------


async def test_send_turn_on_suspended_session(
    store: SessionStore,
) -> None:
    """After mux eviction, send_turn transparently restores the session."""
    mux = SessionMultiplexer(store, max_slots=1)
    provider = FakeProvider()
    sched = TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
    )
    # Second create evicts the first from the single slot.
    s1 = await sched.create_session("fake", "m", "p")
    _s2 = await sched.create_session("fake", "m", "p")
    assert not mux.contains(s1.id)

    # send_turn on the evicted session restores it transparently.
    response = await sched.send_turn(s1.id, "hello")
    assert response == "response"
    assert mux.contains(s1.id)


# -- error handling ---------------------------------------------------------


async def test_send_turn_error_releases_slot(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    provider = FakeProvider()
    provider._error_on_send = True
    sched = TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
    )
    session = await sched.create_session("fake", "m", "p")

    deferred_called: list[bool] = []

    async def cb() -> None:
        deferred_called.append(True)

    sched.defer(cb)

    with pytest.raises(RuntimeError, match="send failed"):
        await sched.send_turn(session.id, "hello")

    # Slot released despite the error (session still in mux, just evictable).
    assert mux.contains(session.id)
    # Deferred was NOT drained.
    assert deferred_called == []
