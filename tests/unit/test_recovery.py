# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent tree persistence and recovery."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from substrat.logging import EventLog, read_log
from substrat.orchestrator import Orchestrator
from substrat.scheduler import TurnScheduler
from substrat.session import SessionStore
from substrat.session.model import SessionState
from substrat.session.multiplexer import SessionMultiplexer

# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Minimal provider session for testing."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self._chunks = chunks if chunks is not None else ["ok"]
        self.stopped = False

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        for chunk in self._chunks:
            yield chunk

    async def suspend(self) -> bytes:
        return b"fake-state"

    async def stop(self) -> None:
        self.stopped = True


class FakeProvider:
    """Tracks create calls for assertions."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self._chunks = chunks
        self.created: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "fake"

    async def create(
        self,
        model: str,
        system_prompt: str,
        log: EventLog | None = None,
    ) -> FakeProviderSession:
        self.created.append((model, system_prompt))
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
    tmp_path: Path,
) -> TurnScheduler:
    return TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
        log_root=tmp_path / "sessions",
    )


@pytest.fixture()
def orch(scheduler: TurnScheduler) -> Orchestrator:
    return Orchestrator(
        scheduler,
        default_provider="fake",
        default_model="test-model",
    )


def _fresh_orch(tmp_path: Path, provider: FakeProvider) -> Orchestrator:
    """Build a brand-new orchestrator against the same store directory."""
    store = SessionStore(tmp_path / "sessions")
    mux = SessionMultiplexer(store, max_slots=4)
    sched = TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
        log_root=tmp_path / "sessions",
    )
    return Orchestrator(sched, default_provider="fake", default_model="test-model")


# -- Event logging ----------------------------------------------------------


async def test_create_root_logs_agent_created(
    orch: Orchestrator,
    store: SessionStore,
) -> None:
    """create_root_agent logs agent.created with correct fields."""
    node = await orch.create_root_agent("alpha", "do things")
    log_path = store.agent_dir(node.session_id) / "events.jsonl"
    entries = read_log(log_path)
    created = [e for e in entries if e["event"] == "agent.created"]
    assert len(created) == 1
    data = created[0]["data"]
    assert data["agent_id"] == node.id.hex
    assert data["name"] == "alpha"
    assert data["parent_session_id"] is None
    assert data["instructions"] == "do things"


async def test_spawn_logs_child_agent_created(
    orch: Orchestrator,
    store: SessionStore,
) -> None:
    """Spawn + drain logs child agent.created with parent_session_id."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    result = handler.spawn_agent("child", "be helpful")
    child_id = UUID(result["agent_id"])

    # Drain deferred — creates child session and logs event.
    await orch.run_turn(parent.id, "go")

    child_node = orch.tree.get(child_id)
    log_path = store.agent_dir(child_node.session_id) / "events.jsonl"
    entries = read_log(log_path)
    created = [e for e in entries if e["event"] == "agent.created"]
    assert len(created) == 1
    data = created[0]["data"]
    assert data["agent_id"] == child_id.hex
    assert data["name"] == "child"
    assert data["parent_session_id"] == parent.session_id.hex


async def test_terminate_logs_agent_terminated(
    orch: Orchestrator,
    store: SessionStore,
) -> None:
    """terminate_agent logs agent.terminated before closing the log."""
    node = await orch.create_root_agent("doomed", "p")
    sid = node.session_id
    log_path = store.agent_dir(sid) / "events.jsonl"

    await orch.terminate_agent(node.id)

    # Log file should still exist; read it directly.
    entries = read_log(log_path)
    terminated = [e for e in entries if e["event"] == "agent.terminated"]
    assert len(terminated) == 1
    assert terminated[0]["data"]["agent_id"] == node.id.hex


async def test_grandchild_parent_session_chain(
    orch: Orchestrator,
    store: SessionStore,
) -> None:
    """Grandchild logs correct parent_session_id at each level."""
    root = await orch.create_root_agent("root", "r")
    h_root = orch.get_handler(root.id)
    r_child = h_root.spawn_agent("child", "ci")
    child_id = UUID(r_child["agent_id"])
    await orch.run_turn(root.id, "go")

    child_node = orch.tree.get(child_id)
    h_child = orch.get_handler(child_id)
    r_grand = h_child.spawn_agent("grandchild", "gi")
    grand_id = UUID(r_grand["agent_id"])
    await orch.run_turn(child_id, "go")

    grand_node = orch.tree.get(grand_id)

    # Child's event log: parent_session_id points to root's session.
    child_entries = read_log(store.agent_dir(child_node.session_id) / "events.jsonl")
    child_created = [e for e in child_entries if e["event"] == "agent.created"]
    assert child_created[0]["data"]["parent_session_id"] == root.session_id.hex

    # Grandchild's event log: parent_session_id points to child's session.
    grand_entries = read_log(store.agent_dir(grand_node.session_id) / "events.jsonl")
    grand_created = [e for e in grand_entries if e["event"] == "agent.created"]
    assert grand_created[0]["data"]["parent_session_id"] == child_node.session_id.hex


# -- read_log ---------------------------------------------------------------


def test_read_log_normal(tmp_path: Path) -> None:
    """Normal JSONL file returns list of dicts."""
    log_path = tmp_path / "events.jsonl"
    entries = [
        {"event": "a", "data": {"x": 1}},
        {"event": "b", "data": {"y": 2}},
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    result = read_log(log_path)
    assert result == entries


def test_read_log_missing_file(tmp_path: Path) -> None:
    """Missing file returns empty list."""
    result = read_log(tmp_path / "nonexistent.jsonl")
    assert result == []


def test_read_log_pending_recovered(tmp_path: Path) -> None:
    """Pending file entry is included if not already tail of main log."""
    log_path = tmp_path / "events.jsonl"
    pending_path = tmp_path / "events.pending"
    main_entry = {"event": "a", "data": {"x": 1}}
    pending_entry = {"event": "b", "data": {"y": 2}}
    log_path.write_text(json.dumps(main_entry) + "\n")
    pending_path.write_text(json.dumps(pending_entry) + "\n")
    result = read_log(log_path)
    assert len(result) == 2
    assert result[0] == main_entry
    assert result[1] == pending_entry


def test_read_log_empty_file(tmp_path: Path) -> None:
    """Empty file returns empty list."""
    log_path = tmp_path / "events.jsonl"
    log_path.write_bytes(b"")
    assert read_log(log_path) == []


def test_read_log_partial_trailing_line(tmp_path: Path) -> None:
    """Truncated trailing line is skipped."""
    log_path = tmp_path / "events.jsonl"
    valid = {"event": "a", "data": {"x": 1}}
    log_path.write_text(json.dumps(valid) + "\n" + '{"event":"b')
    result = read_log(log_path)
    assert result == [valid]


def test_read_log_corrupt_middle_line(tmp_path: Path) -> None:
    """Corrupt line in the middle is skipped; valid lines returned."""
    log_path = tmp_path / "events.jsonl"
    v1 = {"event": "a"}
    v2 = {"event": "c"}
    log_path.write_text(json.dumps(v1) + "\nGARBAGE\n" + json.dumps(v2) + "\n")
    assert read_log(log_path) == [v1, v2]


def test_read_log_pending_already_tail(tmp_path: Path) -> None:
    """Pending entry that matches main log tail is not duplicated."""
    log_path = tmp_path / "events.jsonl"
    pending_path = tmp_path / "events.pending"
    entry = {"event": "x", "data": {"k": 1}}
    log_path.write_text(json.dumps(entry) + "\n")
    pending_path.write_text(json.dumps(entry) + "\n")
    result = read_log(log_path)
    assert result == [entry]


def test_read_log_pending_corrupt(tmp_path: Path) -> None:
    """Corrupt pending file is ignored."""
    log_path = tmp_path / "events.jsonl"
    pending_path = tmp_path / "events.pending"
    valid = {"event": "a"}
    log_path.write_text(json.dumps(valid) + "\n")
    pending_path.write_bytes(b"NOT JSON{{{")
    assert read_log(log_path) == [valid]


def test_read_log_pending_empty(tmp_path: Path) -> None:
    """Empty pending file is ignored."""
    log_path = tmp_path / "events.jsonl"
    pending_path = tmp_path / "events.pending"
    valid = {"event": "a"}
    log_path.write_text(json.dumps(valid) + "\n")
    pending_path.write_bytes(b"")
    assert read_log(log_path) == [valid]


# -- Recovery round-trip ----------------------------------------------------


async def test_recover_single_root(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Single root agent: tree, handler, inbox rebuilt after recovery."""
    node = await orch.create_root_agent("alpha", "do things")
    original_id = node.id
    original_sid = node.session_id

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert original_id in orch2.tree
    recovered = orch2.tree.get(original_id)
    assert recovered.name == "alpha"
    assert recovered.session_id == original_sid
    assert original_id in orch2.inboxes
    assert orch2.get_handler(original_id) is not None


async def test_recover_parent_child(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Parent + child recovered with correct links."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    result = handler.spawn_agent("child", "ci")
    child_id = UUID(result["agent_id"])
    await orch.run_turn(parent.id, "go")

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert parent.id in orch2.tree
    assert child_id in orch2.tree
    recovered_child = orch2.tree.get(child_id)
    assert recovered_child.parent_id == parent.id
    assert child_id in orch2.tree.get(parent.id).children


async def test_recover_multiple_roots(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Multiple root agents all recovered."""
    r1 = await orch.create_root_agent("alpha", "a")
    r2 = await orch.create_root_agent("beta", "b")
    r3 = await orch.create_root_agent("gamma", "c")

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert len(orch2.tree) == 3
    for node in (r1, r2, r3):
        assert node.id in orch2.tree


async def test_recover_terminated_skipped(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Terminated agent is not recovered."""
    node = await orch.create_root_agent("doomed", "d")
    nid = node.id
    await orch.terminate_agent(nid)

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert nid not in orch2.tree
    assert len(orch2.tree) == 0


async def test_recover_orphan_cleaned(
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Session with no agent.created event is terminated as orphan."""
    store = SessionStore(tmp_path / "sessions")
    mux = SessionMultiplexer(store, max_slots=4)
    sched = TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
        # No log_root — sessions won't get event logs.
    )
    Orchestrator(sched, default_provider="fake", default_model="test-model")

    # Create a session directly through the scheduler (no agent.created event).
    session = await sched.create_session("fake", "test-model", "orphan prompt")
    await mux.release(session.id)

    # Now recover with a fresh orch that does have log_root.
    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    # No agents in tree — the orphan session was cleaned up.
    assert len(orch2.tree) == 0
    # Session was terminated on disk.
    reloaded = store.load(session.id)
    assert reloaded.state == SessionState.TERMINATED


async def test_recover_three_level_tree(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Three-level tree (root → child → grandchild) fully recovered."""
    root = await orch.create_root_agent("root", "r")
    h_root = orch.get_handler(root.id)
    r_child = h_root.spawn_agent("child", "ci")
    child_id = UUID(r_child["agent_id"])
    await orch.run_turn(root.id, "go")

    h_child = orch.get_handler(child_id)
    r_grand = h_child.spawn_agent("grandchild", "gi")
    grand_id = UUID(r_grand["agent_id"])
    await orch.run_turn(child_id, "go")

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert len(orch2.tree) == 3
    assert root.id in orch2.tree
    assert child_id in orch2.tree
    assert grand_id in orch2.tree

    recovered_child = orch2.tree.get(child_id)
    recovered_grand = orch2.tree.get(grand_id)
    assert recovered_child.parent_id == root.id
    assert recovered_grand.parent_id == child_id
    assert orch2.get_handler(grand_id) is not None


async def test_recover_event_log_terminated_skipped(
    orch: Orchestrator,
    store: SessionStore,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Event-log terminated flag causes skip even if session is SUSPENDED."""
    node = await orch.create_root_agent("ghost", "g")
    sid = node.session_id
    log_path = store.agent_dir(sid) / "events.jsonl"

    # Manually append an agent.terminated event without going through terminate_agent.
    with log_path.open("a") as f:
        f.write(
            json.dumps({"event": "agent.terminated", "data": {"agent_id": node.id.hex}})
            + "\n"
        )

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    # Session is SUSPENDED on disk but log says terminated — should be skipped.
    assert node.id not in orch2.tree


async def test_recover_empty_store(
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Empty store — recover is a no-op."""
    orch = _fresh_orch(tmp_path, provider)
    await orch.recover()
    assert len(orch.tree) == 0


# -- Scheduler helpers -----------------------------------------------------


def test_log_event_missing_session(tmp_path: Path) -> None:
    """log_event raises KeyError for unknown session_id."""
    store = SessionStore(tmp_path / "sessions")
    mux = SessionMultiplexer(store, max_slots=4)
    sched = TurnScheduler(
        providers={},
        mux=mux,
        store=store,
        log_root=tmp_path / "sessions",
    )
    with pytest.raises(KeyError):
        sched.log_event(uuid4(), "test.event")


# -- Message recovery -------------------------------------------------------


async def test_recover_pending_message(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Sent but unchecked message is re-injected on recovery."""
    root = await orch.create_root_agent("root", "r")
    h = orch.get_handler(root.id)
    r = h.spawn_agent("child", "ci")
    child_id = UUID(r["agent_id"])
    await orch.run_turn(root.id, "go")

    # Root sends a message to child. No check_inbox on child.
    h = orch.get_handler(root.id)
    h.send_message("child", "hello from root")

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    inbox = orch2.inboxes[child_id]
    assert len(inbox) == 1
    msg = inbox.peek()[0]
    assert msg.payload == "hello from root"


async def test_recover_delivered_not_reinjected(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Message that was sent AND checked is not re-injected."""
    root = await orch.create_root_agent("root", "r")
    h_root = orch.get_handler(root.id)
    r = h_root.spawn_agent("child", "ci")
    child_id = UUID(r["agent_id"])
    await orch.run_turn(root.id, "go")

    h_root = orch.get_handler(root.id)
    h_root.send_message("child", "delivered msg")

    h_child = orch.get_handler(child_id)
    h_child.check_inbox()

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert len(orch2.inboxes[child_id]) == 0


async def test_recover_multiple_pending(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Send 3 messages, deliver 1. Recovery yields 2 pending."""
    root = await orch.create_root_agent("root", "r")
    h_root = orch.get_handler(root.id)
    r = h_root.spawn_agent("child", "ci")
    child_id = UUID(r["agent_id"])
    await orch.run_turn(root.id, "go")

    h_root = orch.get_handler(root.id)
    h_root.send_message("child", "m1")
    h_root.send_message("child", "m2")
    h_root.send_message("child", "m3")

    # Deliver one message (drains all 3, but only first check_inbox matters).
    h_child = orch.get_handler(child_id)
    result = h_child.check_inbox()
    assert len(result["messages"]) == 3

    # Send 2 more after drain — these are pending.
    h_root = orch.get_handler(root.id)
    h_root.send_message("child", "m4")
    h_root.send_message("child", "m5")

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert len(orch2.inboxes[child_id]) == 2
    payloads = {m.payload for m in orch2.inboxes[child_id].peek()}
    assert payloads == {"m4", "m5"}


async def test_recover_message_envelope_fields(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Reconstructed envelope preserves all fields."""
    root = await orch.create_root_agent("root", "r")
    h_root = orch.get_handler(root.id)
    r = h_root.spawn_agent("child", "ci")
    child_id = UUID(r["agent_id"])
    await orch.run_turn(root.id, "go")

    h_root = orch.get_handler(root.id)
    result = h_root.send_message("child", "payload check", sync=False)
    original_mid = result["message_id"]

    # Grab original envelope for comparison.
    original = orch.inboxes[child_id].peek()[0]

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    recovered = orch2.inboxes[child_id].peek()[0]
    assert str(recovered.id) == original_mid
    assert recovered.sender == root.id
    assert recovered.recipient == child_id
    assert recovered.payload == "payload check"
    assert recovered.kind == original.kind
    assert recovered.metadata == original.metadata
    assert recovered.timestamp == original.timestamp


async def test_recover_broadcast_pending(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Broadcast to siblings. Recovery puts message in each sibling's inbox."""
    root = await orch.create_root_agent("root", "r")
    h_root = orch.get_handler(root.id)
    h_root.spawn_agent("a", "ai")
    h_root.spawn_agent("b", "bi")
    h_root.spawn_agent("c", "ci")
    await orch.run_turn(root.id, "go")

    a_id = orch.tree.get(
        next(n.id for n in orch.tree.children(root.id) if n.name == "a")
    ).id
    b_id = next(n.id for n in orch.tree.children(root.id) if n.name == "b")
    c_id = next(n.id for n in orch.tree.children(root.id) if n.name == "c")

    h_a = orch.get_handler(a_id)
    h_a.broadcast("team update")

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert len(orch2.inboxes[b_id]) == 1
    assert len(orch2.inboxes[c_id]) == 1
    assert orch2.inboxes[b_id].peek()[0].payload == "team update"
    assert orch2.inboxes[c_id].peek()[0].payload == "team update"
    # Sender should not get own broadcast.
    assert len(orch2.inboxes[a_id]) == 0


async def test_recover_sender_terminated(
    orch: Orchestrator,
    tmp_path: Path,
    provider: FakeProvider,
) -> None:
    """Sender terminated after sending. Message still in recipient inbox on recovery."""
    root = await orch.create_root_agent("root", "r")
    h_root = orch.get_handler(root.id)
    r_a = h_root.spawn_agent("sender", "si")
    r_b = h_root.spawn_agent("receiver", "ri")
    sender_id = UUID(r_a["agent_id"])
    receiver_id = UUID(r_b["agent_id"])
    await orch.run_turn(root.id, "go")

    h_sender = orch.get_handler(sender_id)
    h_sender.send_message("receiver", "farewell")

    await orch.terminate_agent(sender_id)

    orch2 = _fresh_orch(tmp_path, provider)
    await orch2.recover()

    assert len(orch2.inboxes[receiver_id]) == 1
    assert orch2.inboxes[receiver_id].peek()[0].payload == "farewell"
