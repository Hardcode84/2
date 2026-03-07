# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the tool logic layer (ToolHandler)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
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
from substrat.agent.message import USER
from substrat.workspace import (
    Workspace,
    WorkspaceMapping,
    WorkspaceStore,
    WorkspaceToolHandler,
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


def _dummy_spawn_callback(
    node: AgentNode, ws_key: tuple[UUID, str] | None = None
) -> Any:
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


def test_send_no_sync_field(fix: ToolFixture) -> None:
    """Messages have no sync metadata — all delivery is async."""
    result = fix.h_alice.send_message("bob", "fire and forget")
    assert "waiting_for_reply" not in result
    assert result["status"] == "sent"


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


def test_check_inbox_filter_by_sender(fix: ToolFixture) -> None:
    """Filtering by sender returns only that sender's messages."""
    fix.h_bob.send_message("alice", "from bob")
    fix.h_carol.send_message("alice", "from carol")
    result = fix.h_alice.check_inbox(sender="bob")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["from"] == "bob"
    # Carol's message stays in the inbox.
    assert len(fix.inboxes[fix.alice.id]) == 1


def test_check_inbox_filter_by_kind(fix: ToolFixture) -> None:
    """Filtering by kind returns only matching messages."""
    fix.h_bob.send_message("alice", "request msg")
    # Inject a NOTIFICATION directly to have a different kind.
    from substrat.agent.message import MessageEnvelope

    notif = MessageEnvelope(
        sender=fix.bob.id,
        recipient=fix.alice.id,
        kind=MessageKind.NOTIFICATION,
        payload="notif msg",
    )
    fix.inboxes[fix.alice.id].deliver(notif)
    result = fix.h_alice.check_inbox(kind="notification")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "notif msg"
    # The request message stays.
    assert len(fix.inboxes[fix.alice.id]) == 1


def test_check_inbox_filter_combined(fix: ToolFixture) -> None:
    """Combined sender + kind filter narrows to intersection."""
    from substrat.agent.message import MessageEnvelope

    fix.h_bob.send_message("alice", "bob request")
    fix.h_carol.send_message("alice", "carol request")
    # Inject a NOTIFICATION from bob to have a different kind.
    notif = MessageEnvelope(
        sender=fix.bob.id,
        recipient=fix.alice.id,
        kind=MessageKind.NOTIFICATION,
        payload="bob notif",
    )
    fix.inboxes[fix.alice.id].deliver(notif)
    # Filter: sender=bob AND kind=request.
    result = fix.h_alice.check_inbox(sender="bob", kind="request")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "bob request"
    # Two messages left (carol's request + bob's notification).
    assert len(fix.inboxes[fix.alice.id]) == 2


def test_check_inbox_filter_no_match(fix: ToolFixture) -> None:
    """No matching messages — inbox untouched."""
    fix.h_bob.send_message("alice", "from bob")
    result = fix.h_alice.check_inbox(sender="carol")
    assert result["messages"] == []
    assert len(fix.inboxes[fix.alice.id]) == 1


def test_check_inbox_filter_unknown_sender(fix: ToolFixture) -> None:
    """Unknown sender name returns error dict."""
    result = fix.h_alice.check_inbox(sender="nobody")
    assert "error" in result


def test_check_inbox_filter_unknown_kind(fix: ToolFixture) -> None:
    """Unknown kind string returns error dict."""
    result = fix.h_alice.check_inbox(kind="bogus")
    assert "error" in result


def test_check_inbox_filter_logs_delivered(fix: ToolFixture) -> None:
    """Filtered messages still log message.delivered."""
    cap = LogCapture()
    fix.h_bob.send_message("alice", "m1")
    fix.h_carol.send_message("alice", "m2")
    handler = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        log_callback=cap,
    )
    result = handler.check_inbox(sender="bob")
    delivered = [d for _, ev, d in cap.events if ev == "message.delivered"]
    assert len(delivered) == 1
    assert len(result["messages"]) == 1


def test_check_inbox_sentinel_sender_shows_name(fix: ToolFixture) -> None:
    # Manually deliver a SYSTEM message.
    from substrat.agent.message import MessageEnvelope

    envelope = MessageEnvelope(sender=SYSTEM, recipient=fix.alice.id, payload="sys")
    fix.inboxes[fix.alice.id].deliver(envelope)
    result = fix.h_alice.check_inbox()
    assert result["messages"][0]["from"] == "SYSTEM"


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
    fix.dave.begin_turn()
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
    assert envelope.kind == MessageKind.REQUEST


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


# --- complete ---


def test_complete_sends_response_to_parent(fix: ToolFixture) -> None:
    """complete() delivers a RESPONSE to the caller's parent."""
    from substrat.agent.message import MessageKind

    result = fix.h_alice.complete("done")
    assert result["status"] == "completing"
    assert "message_id" in result
    inbox = fix.inboxes[fix.root.id]
    assert len(inbox) == 1
    msg = inbox.peek()[0]
    assert msg.kind == MessageKind.RESPONSE
    assert msg.payload == "done"
    assert msg.sender == fix.alice.id


def test_complete_defers_termination(fix: ToolFixture) -> None:
    """complete() queues deferred self-termination."""
    terminated: list[UUID] = []

    def term_cb(agent_id: UUID) -> Any:
        async def do_term() -> None:
            terminated.append(agent_id)

        return do_term

    handler = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        terminate_callback=term_cb,
    )
    handler.complete("bye")
    deferred = handler.drain_deferred()
    assert len(deferred) == 1


def test_complete_root_agent_error(fix: ToolFixture) -> None:
    """Root agent cannot complete — no parent."""
    result = fix.h_root.complete("i am root")
    assert "error" in result
    assert "root" in result["error"]


def test_complete_with_children_error(fix: ToolFixture) -> None:
    """Agent with children cannot complete."""
    result = fix.h_carol.complete("still have dave")
    assert "error" in result
    assert "children" in result["error"]


def test_complete_fires_wake_on_parent(fix: ToolFixture) -> None:
    """complete() fires wake callback for parent."""
    woken: list[UUID] = []
    handler = ToolHandler(
        fix.tree, fix.inboxes, fix.alice.id, wake_callback=woken.append
    )
    handler.complete("result")
    assert fix.root.id in woken


# --- poke ---


def test_poke_alive_child(fix: ToolFixture) -> None:
    """Poke enqueues wake for a direct child."""
    woken: list[UUID] = []
    handler = ToolHandler(
        fix.tree, fix.inboxes, fix.root.id, wake_callback=woken.append
    )
    result = handler.poke("alice")
    assert result == {"status": "poked", "agent_id": str(fix.alice.id)}
    assert woken == [fix.alice.id]


def test_poke_nonexistent_child(fix: ToolFixture) -> None:
    """Poke with unknown child name returns error."""
    result = fix.h_root.poke("nobody")
    assert "error" in result
    assert "no child" in result["error"]


def test_poke_non_child(fix: ToolFixture) -> None:
    """Poke only works on direct children, not siblings or parent."""
    result = fix.h_alice.poke("bob")
    assert "error" in result
    assert "no child" in result["error"]


def test_poke_without_wake_callback(fix: ToolFixture) -> None:
    """Poke without wake callback still succeeds (no-op wake)."""
    handler = ToolHandler(fix.tree, fix.inboxes, fix.root.id)
    result = handler.poke("alice")
    assert result["status"] == "poked"


# --- wake callback ---


def test_send_message_fires_wake(fix: ToolFixture) -> None:
    """send_message fires wake callback for recipient."""
    woken: list[UUID] = []
    handler = ToolHandler(
        fix.tree, fix.inboxes, fix.alice.id, wake_callback=woken.append
    )
    handler.send_message("bob", "hi")
    assert woken == [fix.bob.id]


def test_broadcast_fires_wake_per_sibling(fix: ToolFixture) -> None:
    """broadcast fires wake callback once per sibling."""
    woken: list[UUID] = []
    handler = ToolHandler(
        fix.tree, fix.inboxes, fix.alice.id, wake_callback=woken.append
    )
    handler.broadcast("hello team")
    assert set(woken) == {fix.bob.id, fix.carol.id}


def test_no_wake_callback_no_crash(fix: ToolFixture) -> None:
    """Delivery without wake callback doesn't crash."""
    handler = ToolHandler(fix.tree, fix.inboxes, fix.alice.id)
    result = handler.send_message("bob", "quiet")
    assert result["status"] == "sent"


def test_wake_fires_after_inbox_delivery(fix: ToolFixture) -> None:
    """At wake time the inbox already contains the message."""
    inbox_lengths: list[int] = []

    def spy(agent_id: UUID) -> None:
        inbox_lengths.append(len(fix.inboxes[agent_id]))

    handler = ToolHandler(fix.tree, fix.inboxes, fix.alice.id, wake_callback=spy)
    handler.send_message("bob", "check timing")
    assert inbox_lengths == [1]


# === Workspace tool tests ===


class WsFixture(NamedTuple):
    tree: AgentTree
    inboxes: InboxRegistry
    ws_store: WorkspaceStore
    ws_mapping: WorkspaceMapping
    root: AgentNode
    alice: AgentNode
    bob: AgentNode
    h_root: ToolHandler
    h_alice: ToolHandler
    h_bob: ToolHandler
    wh_root: WorkspaceToolHandler
    wh_alice: WorkspaceToolHandler
    wh_bob: WorkspaceToolHandler


@pytest.fixture()
def ws_fix(tmp_path: Path) -> WsFixture:
    """Build tree: root -> {alice, bob}. Each handler has workspace deps."""
    tree = AgentTree()
    inboxes: InboxRegistry = {}
    store = WorkspaceStore(tmp_path / "workspaces")
    mapping = WorkspaceMapping()

    root = AgentNode(session_id=uuid4(), name="root")
    alice = AgentNode(session_id=uuid4(), name="alice", parent_id=root.id)
    bob = AgentNode(session_id=uuid4(), name="bob", parent_id=root.id)
    for n in (root, alice, bob):
        tree.add(n)
        inboxes[n.id] = Inbox()

    def make_ws_handler(agent_id: UUID) -> WorkspaceToolHandler:
        def resolve_ctx() -> tuple[UUID | None, list[UUID], Callable[[str], UUID]]:
            parent = tree.parent(agent_id)
            parent_id = parent.id if parent else None
            caller = tree.get(agent_id)

            def child_lookup(name: str) -> UUID:
                return tree.child_by_name(agent_id, name).id

            return parent_id, caller.children, child_lookup

        def scope_namer(scope: UUID) -> str:
            if scope == agent_id:
                return "self"
            caller = tree.get(agent_id)
            for cid in caller.children:
                child = tree.get(cid)
                if child.id == scope:
                    return child.name
            return "parent"

        return WorkspaceToolHandler(
            store=store,
            mapping=mapping,
            caller_id=agent_id,
            resolve_ctx=resolve_ctx,
            scope_namer=scope_namer,
        )

    wh_root = make_ws_handler(root.id)
    wh_alice = make_ws_handler(alice.id)
    wh_bob = make_ws_handler(bob.id)

    def handler(agent_id: UUID, wh: WorkspaceToolHandler) -> ToolHandler:
        return ToolHandler(
            tree,
            inboxes,
            agent_id,
            validate_ws_ref=wh.validate_ref,
        )

    return WsFixture(
        tree,
        inboxes,
        store,
        mapping,
        root,
        alice,
        bob,
        handler(root.id, wh_root),
        handler(alice.id, wh_alice),
        handler(bob.id, wh_bob),
        wh_root,
        wh_alice,
        wh_bob,
    )


# --- list_workspaces ---


def test_list_workspaces_own_scope(ws_fix: WsFixture) -> None:
    """Agent sees workspaces in its own scope."""
    ws = Workspace(
        name="mine",
        scope=ws_fix.alice.id,
        root_path=ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "mine") / "root",
    )
    ws_fix.ws_store.save(ws)
    result = ws_fix.wh_alice.list_workspaces()
    names = [w["name"] for w in result["workspaces"]]
    assert "mine" in names
    entry = next(w for w in result["workspaces"] if w["name"] == "mine")
    assert entry["scope"] == "self"
    assert entry["mutable"] is True


def test_list_workspaces_child_scope(ws_fix: WsFixture) -> None:
    """Parent sees workspaces in children's scopes."""
    ws = Workspace(
        name="child-ws",
        scope=ws_fix.alice.id,
        root_path=ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "child-ws") / "root",
    )
    ws_fix.ws_store.save(ws)
    result = ws_fix.wh_root.list_workspaces()
    entry = next(w for w in result["workspaces"] if w["name"] == "child-ws")
    assert entry["scope"] == "alice"
    assert entry["mutable"] is True


def test_list_workspaces_parent_scope_not_mutable(ws_fix: WsFixture) -> None:
    """Child sees parent's workspaces as read-only."""
    ws = Workspace(
        name="parent-ws",
        scope=ws_fix.root.id,
        root_path=ws_fix.ws_store.workspace_dir(ws_fix.root.id, "parent-ws") / "root",
    )
    ws_fix.ws_store.save(ws)
    result = ws_fix.wh_alice.list_workspaces()
    entry = next(w for w in result["workspaces"] if w["name"] == "parent-ws")
    assert entry["scope"] == "parent"
    assert entry["mutable"] is False


def test_list_workspaces_empty(ws_fix: WsFixture) -> None:
    """No workspaces exist — returns empty list."""
    result = ws_fix.wh_alice.list_workspaces()
    assert result == {"workspaces": []}


def test_list_workspaces_invisible_scopes_filtered(ws_fix: WsFixture) -> None:
    """Workspaces in sibling scope are not visible."""
    # Create workspace in bob's scope — alice shouldn't see it.
    ws = Workspace(
        name="bob-ws",
        scope=ws_fix.bob.id,
        root_path=ws_fix.ws_store.workspace_dir(ws_fix.bob.id, "bob-ws") / "root",
    )
    ws_fix.ws_store.save(ws)
    result = ws_fix.wh_alice.list_workspaces()
    names = [w["name"] for w in result["workspaces"]]
    assert "bob-ws" not in names


def test_list_workspaces_root_sees_user_scope(ws_fix: WsFixture) -> None:
    """Root agent sees USER-scoped workspaces as parent scope."""
    ws = Workspace(
        name="user-ws",
        scope=USER,
        root_path=ws_fix.ws_store.workspace_dir(USER, "user-ws") / "root",
    )
    ws_fix.ws_store.save(ws)
    result = ws_fix.wh_root.list_workspaces()
    entry = next(w for w in result["workspaces"] if w["name"] == "user-ws")
    assert entry["scope"] == "parent"
    assert entry["mutable"] is False


def test_spawn_without_validate_ws_ref_errors() -> None:
    """Handler without validate_ws_ref errors on workspace spawn."""
    tree = AgentTree()
    inboxes: InboxRegistry = {}
    node = AgentNode(session_id=uuid4(), name="lonely")
    tree.add(node)
    inboxes[node.id] = Inbox()
    handler = ToolHandler(tree, inboxes, node.id)
    result = handler.spawn_agent("child", "go", workspace="some-ws")
    assert "error" in result


# --- create_workspace ---


def test_create_workspace_basic(ws_fix: WsFixture) -> None:
    """Basic workspace creation in own scope."""
    result = ws_fix.wh_alice.create_workspace("my-env")
    assert result == {"status": "created", "name": "my-env"}
    assert ws_fix.ws_store.exists(ws_fix.alice.id, "my-env")


def test_create_workspace_duplicate_error(ws_fix: WsFixture) -> None:
    """Duplicate name in own scope fails."""
    ws_fix.wh_alice.create_workspace("dup")
    result = ws_fix.wh_alice.create_workspace("dup")
    assert "error" in result
    assert "already exists" in result["error"]


def test_create_workspace_invalid_name(ws_fix: WsFixture) -> None:
    """Invalid workspace name is rejected."""
    result = ws_fix.wh_alice.create_workspace("../evil")
    assert "error" in result


def test_create_workspace_view_of_own(ws_fix: WsFixture) -> None:
    """Create a view of own workspace."""
    ws_fix.wh_alice.create_workspace("source")
    result = ws_fix.wh_alice.create_workspace("view", view_of="source")
    assert result["status"] == "created"
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "view")
    assert len(ws.links) == 1


def test_create_workspace_view_of_parent(ws_fix: WsFixture) -> None:
    """Child creates a view of parent's workspace."""
    ws_fix.wh_root.create_workspace("shared")
    result = ws_fix.wh_alice.create_workspace("my-view", view_of="../shared")
    assert result["status"] == "created"
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "my-view")
    assert len(ws.links) == 1
    # Link points at parent workspace's root_path.
    parent_ws = ws_fix.ws_store.load(ws_fix.root.id, "shared")
    assert ws.links[0].host_path == parent_ws.root_path / "."


def test_create_workspace_view_of_with_subdir(ws_fix: WsFixture) -> None:
    """View with subdir restricts to a subfolder."""
    ws_fix.wh_alice.create_workspace("big-ws")
    result = ws_fix.wh_alice.create_workspace(
        "sub-view", view_of="big-ws", subdir="src"
    )
    assert result["status"] == "created"
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "sub-view")
    source_ws = ws_fix.ws_store.load(ws_fix.alice.id, "big-ws")
    assert ws.links[0].host_path == source_ws.root_path / "src"


def test_create_workspace_view_of_nonexistent(ws_fix: WsFixture) -> None:
    """View of nonexistent workspace fails."""
    result = ws_fix.wh_alice.create_workspace("view", view_of="ghost")
    assert "error" in result
    assert "not found" in result["error"]


def test_create_workspace_view_of_invisible(ws_fix: WsFixture) -> None:
    """View of workspace in invisible scope fails."""
    # Bob's workspace is invisible to alice.
    ws = Workspace(
        name="secret",
        scope=ws_fix.bob.id,
        root_path=ws_fix.ws_store.workspace_dir(ws_fix.bob.id, "secret") / "root",
    )
    ws_fix.ws_store.save(ws)
    result = ws_fix.wh_alice.create_workspace("view", view_of="bob/secret")
    assert "error" in result


def test_create_workspace_persists(ws_fix: WsFixture) -> None:
    """Created workspace can be loaded back from disk."""
    ws_fix.wh_alice.create_workspace("persistent", network_access=True)
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "persistent")
    assert ws.name == "persistent"
    assert ws.network_access is True


# --- delete_workspace ---


def test_delete_workspace_basic(ws_fix: WsFixture) -> None:
    """Delete own workspace."""
    ws_fix.wh_alice.create_workspace("doomed")
    result = ws_fix.wh_alice.delete_workspace("doomed")
    assert result == {"status": "deleted"}
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "doomed")


def test_delete_workspace_has_agents(ws_fix: WsFixture) -> None:
    """Cannot delete workspace with assigned agents."""
    ws_fix.wh_alice.create_workspace("busy")
    ws_fix.ws_mapping.assign(uuid4(), ws_fix.alice.id, "busy")
    result = ws_fix.wh_alice.delete_workspace("busy")
    assert "error" in result
    assert "assigned agent" in result["error"]


def test_delete_workspace_not_mutable(ws_fix: WsFixture) -> None:
    """Cannot delete workspace in parent's (read-only) scope."""
    ws_fix.wh_root.create_workspace("protected")
    result = ws_fix.wh_alice.delete_workspace("../protected")
    assert "error" in result
    assert "mutable" in result["error"]


def test_delete_workspace_not_found(ws_fix: WsFixture) -> None:
    """Delete nonexistent workspace fails."""
    result = ws_fix.wh_alice.delete_workspace("ghost")
    assert "error" in result
    assert "not found" in result["error"]


def test_delete_workspace_removed_from_disk(ws_fix: WsFixture) -> None:
    """Deleted workspace directory is removed."""
    ws_fix.wh_alice.create_workspace("ephemeral")
    ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "ephemeral")
    assert ws_dir.exists()
    ws_fix.wh_alice.delete_workspace("ephemeral")
    assert not ws_dir.exists()


def test_delete_workspace_child_scope(ws_fix: WsFixture) -> None:
    """Parent can delete workspace in child's scope."""
    ws_fix.wh_alice.create_workspace("child-ws")
    result = ws_fix.wh_root.delete_workspace("alice/child-ws")
    assert result == {"status": "deleted"}


def test_delete_cascades_to_views(ws_fix: WsFixture) -> None:
    """Deleting a source workspace also deletes its views."""
    ws_fix.wh_alice.create_workspace("source")
    ws_fix.wh_alice.create_workspace("view1", view_of="source")
    ws_fix.wh_alice.create_workspace("view2", view_of="source")
    assert ws_fix.ws_store.exists(ws_fix.alice.id, "view1")
    assert ws_fix.ws_store.exists(ws_fix.alice.id, "view2")
    result = ws_fix.wh_alice.delete_workspace("source")
    assert result == {"status": "deleted"}
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "source")
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "view1")
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "view2")


def test_delete_cascades_transitive(ws_fix: WsFixture) -> None:
    """Transitive views (view of a view) are also deleted."""
    ws_fix.wh_alice.create_workspace("base")
    ws_fix.wh_alice.create_workspace("mid", view_of="base")
    ws_fix.wh_alice.create_workspace("leaf", view_of="mid")
    result = ws_fix.wh_alice.delete_workspace("base")
    assert result == {"status": "deleted"}
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "base")
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "mid")
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "leaf")


def test_delete_blocked_by_view_agents(ws_fix: WsFixture) -> None:
    """Cannot delete source if a view has assigned agents."""
    ws_fix.wh_alice.create_workspace("source")
    ws_fix.wh_alice.create_workspace("view", view_of="source")
    ws_fix.ws_mapping.assign(uuid4(), ws_fix.alice.id, "view")
    result = ws_fix.wh_alice.delete_workspace("source")
    assert "error" in result
    assert "assigned agent" in result["error"]
    # Source still exists — nothing was deleted.
    assert ws_fix.ws_store.exists(ws_fix.alice.id, "source")
    assert ws_fix.ws_store.exists(ws_fix.alice.id, "view")


def test_delete_leaf_view_keeps_source(ws_fix: WsFixture) -> None:
    """Deleting a leaf view does not affect the source workspace."""
    ws_fix.wh_alice.create_workspace("source")
    ws_fix.wh_alice.create_workspace("view", view_of="source")
    result = ws_fix.wh_alice.delete_workspace("view")
    assert result == {"status": "deleted"}
    assert ws_fix.ws_store.exists(ws_fix.alice.id, "source")
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "view")


# --- link_dir ---


def test_link_dir_basic(ws_fix: WsFixture) -> None:
    """Link a directory from caller's workspace into target workspace."""
    ws_fix.wh_alice.create_workspace("src-ws")
    ws_fix.wh_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    # Create a directory inside the source workspace to link.
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    result = ws_fix.wh_alice.link_dir("dst-ws", "data", "/mnt/data")
    assert result == {"status": "linked"}
    dst_ws = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert len(dst_ws.links) == 1
    assert dst_ws.links[0].mount_path.as_posix() == "/mnt/data"


def test_link_dir_caller_no_workspace(ws_fix: WsFixture) -> None:
    """Caller without workspace cannot link."""
    ws_fix.wh_alice.create_workspace("target")
    result = ws_fix.wh_alice.link_dir("target", "data", "/mnt/data")
    assert "error" in result
    assert "no workspace" in result["error"]


def test_link_dir_source_not_found(ws_fix: WsFixture) -> None:
    """Source path must exist in caller's workspace."""
    ws_fix.wh_alice.create_workspace("src-ws")
    ws_fix.wh_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    result = ws_fix.wh_alice.link_dir("dst-ws", "nonexistent", "/mnt/x")
    assert "error" in result
    assert "does not exist" in result["error"]


def test_link_dir_target_not_mutable(ws_fix: WsFixture) -> None:
    """Cannot link into parent's workspace (read-only scope)."""
    ws_fix.wh_alice.create_workspace("src-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    ws_fix.wh_root.create_workspace("protected")
    result = ws_fix.wh_alice.link_dir("../protected", "data", "/mnt/data")
    assert "error" in result
    assert "mutable" in result["error"]


def test_link_dir_target_not_found(ws_fix: WsFixture) -> None:
    """Link into nonexistent target workspace fails."""
    ws_fix.wh_alice.create_workspace("src-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    result = ws_fix.wh_alice.link_dir("ghost", "data", "/mnt/data")
    assert "error" in result
    assert "not found" in result["error"]


def test_link_dir_mode_default(ws_fix: WsFixture) -> None:
    """Default link mode is ro."""
    ws_fix.wh_alice.create_workspace("src-ws")
    ws_fix.wh_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "stuff").mkdir(parents=True)
    ws_fix.wh_alice.link_dir("dst-ws", "stuff", "/mnt/stuff")
    dst_ws = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert dst_ws.links[0].mode == "ro"


def test_link_dir_persists(ws_fix: WsFixture) -> None:
    """Link survives a reload from disk."""
    ws_fix.wh_alice.create_workspace("src-ws")
    ws_fix.wh_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "code").mkdir(parents=True)
    ws_fix.wh_alice.link_dir("dst-ws", "code", "/src", mode="rw")
    reloaded = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert len(reloaded.links) == 1
    assert reloaded.links[0].mode == "rw"


# --- unlink_dir ---


def test_unlink_dir_basic(ws_fix: WsFixture) -> None:
    """Unlink removes the link entry."""
    ws_fix.wh_alice.create_workspace("src-ws")
    ws_fix.wh_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    ws_fix.wh_alice.link_dir("dst-ws", "data", "/mnt/data")
    result = ws_fix.wh_alice.unlink_dir("dst-ws", "/mnt/data")
    assert result == {"status": "unlinked"}
    dst_ws = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert len(dst_ws.links) == 0


def test_unlink_dir_not_mutable(ws_fix: WsFixture) -> None:
    """Cannot unlink from parent's workspace."""
    ws_fix.wh_root.create_workspace("parent-ws")
    result = ws_fix.wh_alice.unlink_dir("../parent-ws", "/some/path")
    assert "error" in result
    assert "mutable" in result["error"]


def test_unlink_dir_not_found(ws_fix: WsFixture) -> None:
    """Unlink from nonexistent workspace fails."""
    result = ws_fix.wh_alice.unlink_dir("ghost", "/path")
    assert "error" in result
    assert "not found" in result["error"]


def test_unlink_dir_no_link_at_path(ws_fix: WsFixture) -> None:
    """Unlink with no matching mount_path fails."""
    ws_fix.wh_alice.create_workspace("ws")
    result = ws_fix.wh_alice.unlink_dir("ws", "/nonexistent")
    assert "error" in result
    assert "no link" in result["error"]


# --- spawn_agent with workspace ---


def test_spawn_with_workspace_assigns(ws_fix: WsFixture) -> None:
    """Spawn with workspace passes ws_key through callback."""
    ws_fix.wh_root.create_workspace("child-env")
    # Replace spawn callback with one that captures ws_key.
    captured: list[tuple[UUID, str] | None] = []

    def spy_cb(node: AgentNode, ws_key: tuple[UUID, str] | None) -> Any:
        captured.append(ws_key)

        async def _noop() -> None:
            pass

        return _noop

    handler = ToolHandler(
        ws_fix.tree,
        ws_fix.inboxes,
        ws_fix.root.id,
        spawn_callback=spy_cb,
        validate_ws_ref=ws_fix.wh_root.validate_ref,
    )
    result = handler.spawn_agent("eve", "work", workspace="child-env")
    assert result["status"] == "accepted"
    assert result["workspace"] == "child-env"
    assert len(captured) == 1
    assert captured[0] == (ws_fix.root.id, "child-env")


def test_spawn_with_workspace_mapping_entry(ws_fix: WsFixture) -> None:
    """Spawn callback receives ws_key that can be used for mapping assignment."""
    ws_fix.wh_root.create_workspace("env")

    # Build a handler with a spawn callback that assigns the mapping.
    def assigning_cb(node: AgentNode, ws_key: tuple[UUID, str] | None) -> Any:
        if ws_key is not None:
            ws_fix.ws_mapping.assign(node.id, ws_key[0], ws_key[1])

        async def _noop() -> None:
            pass

        return _noop

    handler = ToolHandler(
        ws_fix.tree,
        ws_fix.inboxes,
        ws_fix.root.id,
        spawn_callback=assigning_cb,
        validate_ws_ref=ws_fix.wh_root.validate_ref,
    )
    result = handler.spawn_agent("fred", "go", workspace="env")
    child_id = UUID(result["agent_id"])
    agents = ws_fix.ws_mapping.agents_in(ws_fix.root.id, "env")
    assert child_id in agents


def test_spawn_with_nonexistent_workspace(ws_fix: WsFixture) -> None:
    """Spawn with nonexistent workspace fails before tree mutation."""
    result = ws_fix.h_root.spawn_agent("ghost-child", "go", workspace="nope")
    assert "error" in result
    assert "not found" in result["error"]
    # No child added to tree.
    children = ws_fix.tree.children(ws_fix.root.id)
    child_names = [c.name for c in children]
    assert "ghost-child" not in child_names


def test_spawn_with_invisible_workspace(ws_fix: WsFixture) -> None:
    """Spawn with workspace in invisible scope fails."""
    # Create workspace in bob's scope — root can see it (child scope).
    ws_fix.wh_bob.create_workspace("secret")
    # Alice can't see bob's scope.
    result = ws_fix.h_alice.spawn_agent("spy", "go", workspace="bob/secret")
    assert "error" in result


def test_spawn_without_workspace_no_key(ws_fix: WsFixture) -> None:
    """Spawn without workspace doesn't set mapping or return key."""
    result = ws_fix.h_root.spawn_agent("plain", "go")
    assert result["status"] == "accepted"
    assert "workspace" not in result
    child_id = UUID(result["agent_id"])
    assert ws_fix.ws_mapping.get(child_id) is None


# --- remind_me / cancel_reminder ---


def _dummy_remind_callback(
    reason: str, timeout: float, every: float | None
) -> tuple[UUID, Any]:
    """Return a (reminder_id, noop_deferred) pair for testing."""
    rid = uuid4()

    async def _noop() -> None:
        pass

    return rid, _noop


def test_remind_me_returns_scheduled(fix: ToolFixture) -> None:
    """remind_me with callback returns scheduled + reminder_id, appends deferred."""
    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        remind_callback=_dummy_remind_callback,
    )
    result = h.remind_me("check status", 30)
    assert result["status"] == "scheduled"
    assert "reminder_id" in result
    # Deferred work appended.
    assert len(h.drain_deferred()) == 1


def test_remind_me_with_every(fix: ToolFixture) -> None:
    """remind_me with every parameter succeeds."""
    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        remind_callback=_dummy_remind_callback,
    )
    result = h.remind_me("poll", 10, every=5)
    assert result["status"] == "scheduled"


def test_remind_me_timeout_zero(fix: ToolFixture) -> None:
    """timeout <= 0 returns error."""
    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        remind_callback=_dummy_remind_callback,
    )
    result = h.remind_me("bad", 0)
    assert "error" in result
    assert "positive" in result["error"]


def test_remind_me_timeout_negative(fix: ToolFixture) -> None:
    """Negative timeout returns error."""
    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        remind_callback=_dummy_remind_callback,
    )
    result = h.remind_me("bad", -5)
    assert "error" in result


def test_remind_me_every_zero(fix: ToolFixture) -> None:
    """every <= 0 returns error."""
    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        remind_callback=_dummy_remind_callback,
    )
    result = h.remind_me("bad", 10, every=0)
    assert "error" in result
    assert "positive" in result["error"]


def test_remind_me_no_callback(fix: ToolFixture) -> None:
    """Without callback, remind_me returns error."""
    h = ToolHandler(fix.tree, fix.inboxes, fix.alice.id)
    result = h.remind_me("lonely", 10)
    assert "error" in result
    assert "not available" in result["error"]


def test_cancel_reminder_valid(fix: ToolFixture) -> None:
    """cancel_reminder with known ID returns cancelled."""
    known_id = uuid4()

    def cancel_cb(rid: UUID) -> bool:
        return rid == known_id

    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        cancel_reminder_callback=cancel_cb,
    )
    result = h.cancel_reminder(str(known_id))
    assert result["status"] == "cancelled"


def test_cancel_reminder_unknown(fix: ToolFixture) -> None:
    """cancel_reminder with unknown ID returns error."""
    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        cancel_reminder_callback=lambda rid: False,
    )
    result = h.cancel_reminder(str(uuid4()))
    assert "error" in result
    assert "unknown" in result["error"]


def test_cancel_reminder_invalid_uuid(fix: ToolFixture) -> None:
    """cancel_reminder with garbage string returns error."""
    h = ToolHandler(
        fix.tree,
        fix.inboxes,
        fix.alice.id,
        cancel_reminder_callback=lambda rid: True,
    )
    result = h.cancel_reminder("not-a-uuid")
    assert "error" in result
    assert "invalid" in result["error"]


def test_cancel_reminder_no_callback(fix: ToolFixture) -> None:
    """Without callback, cancel_reminder returns error."""
    h = ToolHandler(fix.tree, fix.inboxes, fix.alice.id)
    result = h.cancel_reminder(str(uuid4()))
    assert "error" in result
    assert "not available" in result["error"]


# --- list_children ---


def test_list_children_returns_all(fix: ToolFixture) -> None:
    """list_children returns all direct children with state and metadata."""
    fix.alice.metadata["role"] = "analyst"
    result = fix.h_root.list_children()
    names = {c["name"] for c in result["children"]}
    assert names == {"alice", "bob", "carol"}
    alice_entry = next(c for c in result["children"] if c["name"] == "alice")
    assert alice_entry["state"] == "idle"
    assert alice_entry["metadata"] == {"role": "analyst"}
    assert alice_entry["agent_id"] == str(fix.alice.id)


def test_list_children_pending_count(fix: ToolFixture) -> None:
    """list_children reports pending message count per child."""
    fix.h_root.send_message("alice", "m1")
    fix.h_root.send_message("alice", "m2")
    result = fix.h_root.list_children()
    alice_entry = next(c for c in result["children"] if c["name"] == "alice")
    assert alice_entry["pending_messages"] == 2
    bob_entry = next(c for c in result["children"] if c["name"] == "bob")
    assert bob_entry["pending_messages"] == 0


def test_list_children_leaf_empty(fix: ToolFixture) -> None:
    """list_children on a leaf agent returns empty list."""
    result = fix.h_alice.list_children()
    assert result["children"] == []


# --- set_agent_metadata ---


def test_set_agent_metadata_sets_key(fix: ToolFixture) -> None:
    """set_agent_metadata sets a key on a child."""
    result = fix.h_root.set_agent_metadata("alice", "task", value="dark-mode")
    assert result["status"] == "updated"
    assert fix.alice.metadata["task"] == "dark-mode"


def test_set_agent_metadata_delete_key(fix: ToolFixture) -> None:
    """set_agent_metadata with null value deletes the key."""
    fix.alice.metadata["task"] = "old-task"
    result = fix.h_root.set_agent_metadata("alice", "task")
    assert result["status"] == "updated"
    assert "task" not in fix.alice.metadata


def test_set_agent_metadata_delete_missing_key(fix: ToolFixture) -> None:
    """Deleting a non-existent key is a no-op, not an error."""
    result = fix.h_root.set_agent_metadata("alice", "nonexistent")
    assert result["status"] == "updated"


def test_set_agent_metadata_non_child_error(fix: ToolFixture) -> None:
    """set_agent_metadata on a non-child returns error."""
    result = fix.h_alice.set_agent_metadata("dave", "k", value="v")
    assert "error" in result


def test_set_agent_metadata_logs_event(fix: ToolFixture) -> None:
    """set_agent_metadata fires metadata.updated log event."""
    cap = LogCapture()
    h = ToolHandler(fix.tree, fix.inboxes, fix.root.id, log_callback=cap)
    h.set_agent_metadata("alice", "role", value="worker")
    events = [(ev, d) for _, ev, d in cap.events if ev == "metadata.updated"]
    assert len(events) == 1
    assert events[0][1] == {"key": "role", "value": "worker"}


# --- spawn_agent with metadata ---


def test_spawn_with_metadata(fix: ToolFixture) -> None:
    """spawn_agent with metadata stores it on the child node."""
    result = fix.h_root.spawn_agent(
        "new-kid", "instructions", metadata={"role": "reviewer"}
    )
    assert result["status"] == "accepted"
    child_id = UUID(result["agent_id"])
    child = fix.tree.get(child_id)
    assert child.metadata == {"role": "reviewer"}


def test_spawn_without_metadata(fix: ToolFixture) -> None:
    """spawn_agent without metadata gets empty dict."""
    result = fix.h_root.spawn_agent("plain-kid", "instructions")
    child_id = UUID(result["agent_id"])
    child = fix.tree.get(child_id)
    assert child.metadata == {}


# --- inspect_agent includes metadata ---


def test_inspect_agent_includes_metadata(fix: ToolFixture) -> None:
    """inspect_agent response includes child metadata."""
    fix.alice.metadata["status"] = "reviewing"
    result = fix.h_root.inspect_agent("alice")
    assert result["metadata"] == {"status": "reviewing"}


# === USER messaging tests ===


def test_send_message_to_user_from_root(fix: ToolFixture) -> None:
    """Root agent can send to USER. Message lands in USER inbox."""
    fix.inboxes[USER] = Inbox()
    result = fix.h_root.send_message("USER", "all done")
    assert result["status"] == "sent"
    msgs = fix.inboxes[USER].collect()
    assert len(msgs) == 1
    assert msgs[0].payload == "all done"
    assert msgs[0].sender == fix.root.id


def test_send_message_to_user_from_child_fails(fix: ToolFixture) -> None:
    """Non-root agents cannot send to USER."""
    fix.inboxes[USER] = Inbox()
    result = fix.h_alice.send_message("USER", "sneaky")
    assert "error" in result
    assert "only root agents" in result["error"]
    assert len(fix.inboxes[USER]) == 0


# === link_from workspace tests ===


def test_link_from_child_workspace_into_own(ws_fix: WsFixture) -> None:
    """Parent can link a child's workspace content into its own workspace."""
    # Create child workspace with content.
    child_ws_dir = (
        ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "child-output") / "root"
    )
    child_ws_dir.mkdir(parents=True)
    (child_ws_dir / "result.txt").write_text("output")
    child_ws = Workspace(
        name="child-output",
        scope=ws_fix.alice.id,
        root_path=child_ws_dir,
    )
    ws_fix.ws_store.save(child_ws)

    # Create root's own workspace and assign it.
    root_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.root.id, "root-ws") / "root"
    root_ws_dir.mkdir(parents=True)
    root_ws = Workspace(
        name="root-ws",
        scope=ws_fix.root.id,
        root_path=root_ws_dir,
    )
    ws_fix.ws_store.save(root_ws)
    ws_fix.ws_mapping.assign(ws_fix.root.id, ws_fix.root.id, "root-ws")

    # link_from child workspace into root's workspace.
    result = ws_fix.wh_root.link_from(
        source_workspace="alice/child-output",
        source=".",
        target="/child-view",
        target_workspace="root-ws",
    )
    assert result["status"] == "linked"
    # Verify the link was added.
    loaded = ws_fix.ws_store.load(ws_fix.root.id, "root-ws")
    assert any(lk.mount_path == Path("/child-view") for lk in loaded.links)


def test_link_from_invisible_workspace_fails(ws_fix: WsFixture) -> None:
    """Cannot link from a workspace that isn't visible."""
    # Bob cannot see alice's workspaces (they are siblings, not parent/child).
    child_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "secret") / "root"
    child_ws_dir.mkdir(parents=True)
    ws_fix.ws_store.save(
        Workspace(
            name="secret",
            scope=ws_fix.alice.id,
            root_path=child_ws_dir,
        )
    )
    result = ws_fix.wh_bob.link_from(
        source_workspace="alice/secret",
        source=".",
        target="/peek",
    )
    assert "error" in result


def test_link_from_nonexistent_source_path_fails(ws_fix: WsFixture) -> None:
    """link_from fails if the source path doesn't exist."""
    child_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "empty") / "root"
    child_ws_dir.mkdir(parents=True)
    ws_fix.ws_store.save(
        Workspace(
            name="empty",
            scope=ws_fix.alice.id,
            root_path=child_ws_dir,
        )
    )
    root_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.root.id, "root-ws2") / "root"
    root_ws_dir.mkdir(parents=True)
    ws_fix.ws_store.save(
        Workspace(
            name="root-ws2",
            scope=ws_fix.root.id,
            root_path=root_ws_dir,
        )
    )
    ws_fix.ws_mapping.assign(ws_fix.root.id, ws_fix.root.id, "root-ws2")
    result = ws_fix.wh_root.link_from(
        source_workspace="alice/empty",
        source="nonexistent",
        target="/ghost",
        target_workspace="root-ws2",
    )
    assert "error" in result
    assert "does not exist" in result["error"]


def test_link_from_into_immutable_scope_fails(ws_fix: WsFixture) -> None:
    """Cannot link into a workspace that is read-only to the caller."""
    # alice cannot mutate root's workspace (parent scope is read-only).
    root_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.root.id, "parent-ws") / "root"
    root_ws_dir.mkdir(parents=True)
    (root_ws_dir / "file.txt").write_text("hi")
    ws_fix.ws_store.save(
        Workspace(
            name="parent-ws",
            scope=ws_fix.root.id,
            root_path=root_ws_dir,
        )
    )
    # alice's own workspace as source.
    alice_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "mine") / "root"
    alice_ws_dir.mkdir(parents=True)
    (alice_ws_dir / "data.txt").write_text("data")
    ws_fix.ws_store.save(
        Workspace(
            name="mine",
            scope=ws_fix.alice.id,
            root_path=alice_ws_dir,
        )
    )
    result = ws_fix.wh_alice.link_from(
        source_workspace="mine",
        source=".",
        target="/inject",
        target_workspace="../parent-ws",
    )
    assert "error" in result
    assert "not in a mutable scope" in result["error"]


def test_link_from_defaults_to_own_workspace(ws_fix: WsFixture) -> None:
    """When target_workspace is omitted, link goes into caller's assigned workspace."""
    # Create child workspace with content.
    child_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "src") / "root"
    child_ws_dir.mkdir(parents=True)
    (child_ws_dir / "code.py").write_text("pass")
    ws_fix.ws_store.save(
        Workspace(
            name="src",
            scope=ws_fix.alice.id,
            root_path=child_ws_dir,
        )
    )
    # Root's own workspace.
    root_ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.root.id, "my-ws") / "root"
    root_ws_dir.mkdir(parents=True)
    ws_fix.ws_store.save(
        Workspace(
            name="my-ws",
            scope=ws_fix.root.id,
            root_path=root_ws_dir,
        )
    )
    ws_fix.ws_mapping.assign(ws_fix.root.id, ws_fix.root.id, "my-ws")
    result = ws_fix.wh_root.link_from(
        source_workspace="alice/src",
        source=".",
        target="/alice-src",
    )
    assert result["status"] == "linked"
    loaded = ws_fix.ws_store.load(ws_fix.root.id, "my-ws")
    assert any(lk.mount_path == Path("/alice-src") for lk in loaded.links)
