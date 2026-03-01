# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the tool logic layer (ToolHandler)."""

from __future__ import annotations

from typing import Any, NamedTuple
from uuid import UUID, uuid4

import pytest

from substrat.agent import (
    SYSTEM,
    AgentNode,
    AgentTree,
    Inbox,
    InboxRegistry,
    MessageKind,
    ToolHandler,
)


class ToolFixture(NamedTuple):
    tree: AgentTree
    inboxes: InboxRegistry
    root: AgentNode
    alice: AgentNode
    bob: AgentNode
    carol: AgentNode
    dave: AgentNode
    h_root: ToolHandler
    h_alice: ToolHandler
    h_bob: ToolHandler
    h_carol: ToolHandler
    h_dave: ToolHandler


def _dummy_spawn_callback(node: AgentNode) -> Any:
    """Return a no-op coroutine factory for testing deferred work."""

    async def _noop() -> None:
        pass

    return _noop


@pytest.fixture()
def fix() -> ToolFixture:
    """Build tree: root -> {alice, bob, carol}, carol -> dave.

    Each agent gets an inbox and a ToolHandler with spawn_callback.
    """
    tree = AgentTree()
    inboxes: InboxRegistry = {}

    root = AgentNode(session_id=uuid4(), name="root")
    alice = AgentNode(session_id=uuid4(), name="alice", parent_id=root.id)
    bob = AgentNode(session_id=uuid4(), name="bob", parent_id=root.id)
    carol = AgentNode(session_id=uuid4(), name="carol", parent_id=root.id)
    dave = AgentNode(session_id=uuid4(), name="dave", parent_id=carol.id)
    for n in (root, alice, bob, carol, dave):
        tree.add(n)
        inboxes[n.id] = Inbox()

    def handler(agent_id: UUID) -> ToolHandler:
        return ToolHandler(tree, inboxes, agent_id, _dummy_spawn_callback)

    return ToolFixture(
        tree,
        inboxes,
        root,
        alice,
        bob,
        carol,
        dave,
        handler(root.id),
        handler(alice.id),
        handler(bob.id),
        handler(carol.id),
        handler(dave.id),
    )


# --- send_message ---


def test_send_sibling_to_sibling(fix: ToolFixture) -> None:
    result = fix.h_alice.send_message("bob", "hello")
    assert result["status"] == "sent"
    assert "message_id" in result
    assert result["waiting_for_reply"] is True
    assert len(fix.inboxes[fix.bob.id]) == 1


def test_send_parent_to_child(fix: ToolFixture) -> None:
    result = fix.h_root.send_message("alice", "do work")
    assert result["status"] == "sent"
    assert len(fix.inboxes[fix.alice.id]) == 1


def test_send_child_to_parent(fix: ToolFixture) -> None:
    result = fix.h_alice.send_message("root", "done")
    assert result["status"] == "sent"
    assert len(fix.inboxes[fix.root.id]) == 1


def test_send_skip_level_error(fix: ToolFixture) -> None:
    # root -> dave is two hops.
    result = fix.h_root.send_message("dave", "hey")
    assert "error" in result


def test_send_self_error(fix: ToolFixture) -> None:
    result = fix.h_alice.send_message("alice", "echo")
    assert "error" in result


def test_send_nonexistent_error(fix: ToolFixture) -> None:
    result = fix.h_alice.send_message("nobody", "hello?")
    assert "error" in result


def test_send_envelope_fields(fix: ToolFixture) -> None:
    fix.h_alice.send_message("bob", "check this")
    envelope = fix.inboxes[fix.bob.id].peek()[0]
    assert envelope.sender == fix.alice.id
    assert envelope.recipient == fix.bob.id
    assert envelope.kind == MessageKind.REQUEST
    assert envelope.payload == "check this"


def test_send_async_metadata(fix: ToolFixture) -> None:
    result = fix.h_alice.send_message("bob", "fire and forget", sync=False)
    assert result["waiting_for_reply"] is False
    envelope = fix.inboxes[fix.bob.id].peek()[0]
    assert envelope.metadata["sync"] == "False"


# --- broadcast ---


def test_broadcast_siblings_receive(fix: ToolFixture) -> None:
    result = fix.h_alice.broadcast("all hands")
    assert result["status"] == "sent"
    # Bob and carol should get envelopes.
    assert len(fix.inboxes[fix.bob.id]) == 1
    assert len(fix.inboxes[fix.carol.id]) == 1
    # Alice should not get her own broadcast.
    assert len(fix.inboxes[fix.alice.id]) == 0


def test_broadcast_no_siblings_error(fix: ToolFixture) -> None:
    # Dave has no siblings.
    result = fix.h_dave.broadcast("anyone?")
    assert "error" in result


def test_broadcast_separate_envelopes_shared_broadcast_id(fix: ToolFixture) -> None:
    result = fix.h_alice.broadcast("sync up")
    broadcast_id = result["message_id"]
    bob_env = fix.inboxes[fix.bob.id].peek()[0]
    carol_env = fix.inboxes[fix.carol.id].peek()[0]
    # Separate envelope ids.
    assert bob_env.id != carol_env.id
    # Shared broadcast_id.
    assert bob_env.metadata["broadcast_id"] == broadcast_id
    assert carol_env.metadata["broadcast_id"] == broadcast_id


def test_broadcast_recipient_count(fix: ToolFixture) -> None:
    result = fix.h_alice.broadcast("heads up")
    assert result["recipient_count"] == 2


# --- check_inbox ---


def test_check_inbox_empty(fix: ToolFixture) -> None:
    result = fix.h_alice.check_inbox()
    assert result == {"messages": []}


def test_check_inbox_drains(fix: ToolFixture) -> None:
    fix.h_bob.send_message("alice", "msg1")
    fix.h_carol.send_message("alice", "msg2")
    result = fix.h_alice.check_inbox()
    assert len(result["messages"]) == 2
    # Second call returns empty.
    assert fix.h_alice.check_inbox() == {"messages": []}


def test_check_inbox_from_shows_name(fix: ToolFixture) -> None:
    fix.h_bob.send_message("alice", "hi")
    result = fix.h_alice.check_inbox()
    assert result["messages"][0]["from"] == "bob"


def test_check_inbox_sentinel_sender_shows_uuid(fix: ToolFixture) -> None:
    # Manually deliver a SYSTEM message.
    from substrat.agent.message import MessageEnvelope

    envelope = MessageEnvelope(sender=SYSTEM, recipient=fix.alice.id, payload="sys")
    fix.inboxes[fix.alice.id].deliver(envelope)
    result = fix.h_alice.check_inbox()
    assert result["messages"][0]["from"] == str(SYSTEM)


# --- spawn_agent ---


def test_spawn_creates_child_in_tree(fix: ToolFixture) -> None:
    result = fix.h_root.spawn_agent("eve", "do stuff")
    assert result["status"] == "accepted"
    agent_id = UUID(result["agent_id"])
    child = fix.tree.get(agent_id)
    assert child.parent_id == fix.root.id
    assert child.name == "eve"


def test_spawn_creates_inbox(fix: ToolFixture) -> None:
    result = fix.h_root.spawn_agent("frank", "go")
    agent_id = UUID(result["agent_id"])
    assert agent_id in fix.inboxes


def test_spawn_deferred_accumulated(fix: ToolFixture) -> None:
    fix.h_root.spawn_agent("gina", "work")
    deferred = fix.h_root.drain_deferred()
    assert len(deferred) == 1


def test_spawn_return_shape(fix: ToolFixture) -> None:
    result = fix.h_root.spawn_agent("hank", "instructions")
    assert set(result.keys()) == {"status", "agent_id", "name"}
    assert result["name"] == "hank"


def test_spawn_duplicate_sibling_name_error(fix: ToolFixture) -> None:
    # alice already exists under root.
    result = fix.h_root.spawn_agent("alice", "duplicate")
    assert "error" in result


def test_spawn_no_callback_no_deferred(fix: ToolFixture) -> None:
    handler = ToolHandler(fix.tree, fix.inboxes, fix.carol.id, spawn_callback=None)
    result = handler.spawn_agent("iris", "go")
    assert result["status"] == "accepted"
    assert handler.drain_deferred() == []


# --- inspect_agent ---


def test_inspect_child(fix: ToolFixture) -> None:
    fix.h_carol.send_message("dave", "task for you")
    result = fix.h_carol.inspect_agent("dave")
    assert result["state"] == "idle"
    assert len(result["recent_messages"]) == 1


def test_inspect_non_child_error(fix: ToolFixture) -> None:
    # alice is a sibling, not a child of bob.
    result = fix.h_bob.inspect_agent("alice")
    assert "error" in result


def test_inspect_nonexistent_error(fix: ToolFixture) -> None:
    result = fix.h_root.inspect_agent("ghost")
    assert "error" in result


def test_inspect_state_matches(fix: ToolFixture) -> None:
    fix.dave.activate()
    result = fix.h_carol.inspect_agent("dave")
    assert result["state"] == "busy"


# --- drain_deferred ---


def test_drain_returns_and_clears(fix: ToolFixture) -> None:
    fix.h_root.spawn_agent("j1", "go")
    fix.h_root.spawn_agent("j2", "go")
    deferred = fix.h_root.drain_deferred()
    assert len(deferred) == 2
    assert fix.h_root.drain_deferred() == []


# --- edge cases: fallback paths ---


def test_check_inbox_no_inbox_entry(fix: ToolFixture) -> None:
    # Caller has no inbox in registry — should return empty, not crash.
    handler = ToolHandler(fix.tree, fix.inboxes, fix.alice.id)
    del fix.inboxes[fix.alice.id]
    result = handler.check_inbox()
    assert result == {"messages": []}


def test_inspect_child_no_inbox(fix: ToolFixture) -> None:
    # Child exists in tree but has no inbox entry.
    del fix.inboxes[fix.dave.id]
    result = fix.h_carol.inspect_agent("dave")
    assert result["recent_messages"] == []


def test_sender_display_name_removed_agent(fix: ToolFixture) -> None:
    # Deliver a message, then remove the sender from the tree before check_inbox.
    fix.h_dave.send_message("carol", "bye")
    fix.tree.remove(fix.dave.id)
    result = fix.h_carol.check_inbox()
    assert result["messages"][0]["from"] == str(fix.dave.id)


def test_broadcast_envelope_kind(fix: ToolFixture) -> None:
    fix.h_alice.broadcast("check kind")
    envelope = fix.inboxes[fix.bob.id].peek()[0]
    assert envelope.kind == MessageKind.MULTICAST


# --- name resolution ---


def test_resolve_parent_by_name(fix: ToolFixture) -> None:
    result = fix.h_alice.send_message("root", "up")
    assert result["status"] == "sent"


def test_resolve_sibling_by_name(fix: ToolFixture) -> None:
    result = fix.h_alice.send_message("carol", "lateral")
    assert result["status"] == "sent"


def test_resolve_child_by_name(fix: ToolFixture) -> None:
    result = fix.h_carol.send_message("dave", "down")
    assert result["status"] == "sent"


# --- log callback ---


class LogCapture:
    """Accumulates (agent_id, event, data) tuples for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[UUID, str, dict[str, Any]]] = []

    def __call__(self, agent_id: UUID, event: str, data: dict[str, Any]) -> None:
        self.events.append((agent_id, event, data))


def test_send_logs_enqueued_to_recipient(fix: ToolFixture) -> None:
    """send_message logs message.enqueued with recipient's agent_id."""
    cap = LogCapture()
    handler = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        log_callback=cap,
    )
    handler.send_message("bob", "hello")
    assert len(cap.events) == 1
    agent_id, event, data = cap.events[0]
    assert agent_id == fix.bob.id
    assert event == "message.enqueued"
    assert data["sender"] == fix.alice.id.hex
    assert data["recipient"] == fix.bob.id.hex
    assert data["payload"] == "hello"
    assert data["kind"] == "request"
    assert "message_id" in data
    assert "timestamp" in data


def test_broadcast_logs_enqueued_per_sibling(fix: ToolFixture) -> None:
    """broadcast logs one enqueued event per sibling."""
    cap = LogCapture()
    handler = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        log_callback=cap,
    )
    handler.broadcast("all hands")
    enqueued = [(aid, d) for aid, ev, d in cap.events if ev == "message.enqueued"]
    assert len(enqueued) == 2
    recipient_ids = {aid for aid, _ in enqueued}
    assert recipient_ids == {fix.bob.id, fix.carol.id}


def test_check_inbox_logs_delivered(fix: ToolFixture) -> None:
    """Draining inbox logs message.delivered per message."""
    cap = LogCapture()
    # Send two messages to alice.
    fix.h_bob.send_message("alice", "m1")
    fix.h_carol.send_message("alice", "m2")
    handler = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        log_callback=cap,
    )
    result = handler.check_inbox()
    delivered = [(aid, d) for aid, ev, d in cap.events if ev == "message.delivered"]
    assert len(delivered) == 2
    logged_ids = {d["message_id"] for _, d in delivered}
    result_ids = {m["message_id"].replace("-", "") for m in result["messages"]}
    assert logged_ids == result_ids
    # All delivered events target the caller.
    assert all(aid == fix.alice.id for aid, _ in delivered)


def test_check_inbox_empty_no_log(fix: ToolFixture) -> None:
    """Empty inbox drain logs nothing."""
    cap = LogCapture()
    handler = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        log_callback=cap,
    )
    handler.check_inbox()
    assert len(cap.events) == 0


def test_no_log_callback_silent(fix: ToolFixture) -> None:
    """Handler with no callback — send_message works without crash."""
    handler = ToolHandler(fix.tree, fix.inboxes, fix.alice.id)
    result = handler.send_message("bob", "quiet")
    assert result["status"] == "sent"


def test_enqueue_logged_before_inbox_delivery(fix: ToolFixture) -> None:
    """At log time the message is NOT yet in the recipient's inbox."""
    inbox_lengths: list[int] = []

    def spy(agent_id: UUID, event: str, data: dict[str, Any]) -> None:
        if event == "message.enqueued":
            inbox_lengths.append(len(fix.inboxes[agent_id]))

    handler = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        log_callback=spy,
    )
    handler.send_message("bob", "check timing")
    # Callback fired before deliver — inbox was still at its pre-delivery length.
    assert inbox_lengths == [0]
