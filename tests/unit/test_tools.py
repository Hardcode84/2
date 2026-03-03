# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the tool logic layer (ToolHandler)."""

from __future__ import annotations

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
from substrat.workspace import Workspace, WorkspaceMapping, WorkspaceStore


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

    def handler(agent_id: UUID) -> ToolHandler:
        return ToolHandler(
            tree,
            inboxes,
            agent_id,
            ws_store=store,
            ws_mapping=mapping,
        )

    return WsFixture(
        tree,
        inboxes,
        store,
        mapping,
        root,
        alice,
        bob,
        handler(root.id),
        handler(alice.id),
        handler(bob.id),
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
    result = ws_fix.h_alice.list_workspaces()
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
    result = ws_fix.h_root.list_workspaces()
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
    result = ws_fix.h_alice.list_workspaces()
    entry = next(w for w in result["workspaces"] if w["name"] == "parent-ws")
    assert entry["scope"] == "parent"
    assert entry["mutable"] is False


def test_list_workspaces_empty(ws_fix: WsFixture) -> None:
    """No workspaces exist — returns empty list."""
    result = ws_fix.h_alice.list_workspaces()
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
    result = ws_fix.h_alice.list_workspaces()
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
    result = ws_fix.h_root.list_workspaces()
    entry = next(w for w in result["workspaces"] if w["name"] == "user-ws")
    assert entry["scope"] == "parent"
    assert entry["mutable"] is False


def test_list_workspaces_no_ws_deps_raises() -> None:
    """Handler without workspace deps raises ToolError on workspace tools."""
    tree = AgentTree()
    inboxes: InboxRegistry = {}
    node = AgentNode(session_id=uuid4(), name="lonely")
    tree.add(node)
    inboxes[node.id] = Inbox()
    handler = ToolHandler(tree, inboxes, node.id)
    result = handler.list_workspaces()
    assert "error" in result


# --- create_workspace ---


def test_create_workspace_basic(ws_fix: WsFixture) -> None:
    """Basic workspace creation in own scope."""
    result = ws_fix.h_alice.create_workspace("my-env")
    assert result == {"status": "created", "name": "my-env"}
    assert ws_fix.ws_store.exists(ws_fix.alice.id, "my-env")


def test_create_workspace_duplicate_error(ws_fix: WsFixture) -> None:
    """Duplicate name in own scope fails."""
    ws_fix.h_alice.create_workspace("dup")
    result = ws_fix.h_alice.create_workspace("dup")
    assert "error" in result
    assert "already exists" in result["error"]


def test_create_workspace_invalid_name(ws_fix: WsFixture) -> None:
    """Invalid workspace name is rejected."""
    result = ws_fix.h_alice.create_workspace("../evil")
    assert "error" in result


def test_create_workspace_view_of_own(ws_fix: WsFixture) -> None:
    """Create a view of own workspace."""
    ws_fix.h_alice.create_workspace("source")
    result = ws_fix.h_alice.create_workspace("view", view_of="source")
    assert result["status"] == "created"
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "view")
    assert len(ws.links) == 1


def test_create_workspace_view_of_parent(ws_fix: WsFixture) -> None:
    """Child creates a view of parent's workspace."""
    ws_fix.h_root.create_workspace("shared")
    result = ws_fix.h_alice.create_workspace("my-view", view_of="../shared")
    assert result["status"] == "created"
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "my-view")
    assert len(ws.links) == 1
    # Link points at parent workspace's root_path.
    parent_ws = ws_fix.ws_store.load(ws_fix.root.id, "shared")
    assert ws.links[0].host_path == parent_ws.root_path / "."


def test_create_workspace_view_of_with_subdir(ws_fix: WsFixture) -> None:
    """View with subdir restricts to a subfolder."""
    ws_fix.h_alice.create_workspace("big-ws")
    result = ws_fix.h_alice.create_workspace("sub-view", view_of="big-ws", subdir="src")
    assert result["status"] == "created"
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "sub-view")
    source_ws = ws_fix.ws_store.load(ws_fix.alice.id, "big-ws")
    assert ws.links[0].host_path == source_ws.root_path / "src"


def test_create_workspace_view_of_nonexistent(ws_fix: WsFixture) -> None:
    """View of nonexistent workspace fails."""
    result = ws_fix.h_alice.create_workspace("view", view_of="ghost")
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
    result = ws_fix.h_alice.create_workspace("view", view_of="bob/secret")
    assert "error" in result


def test_create_workspace_persists(ws_fix: WsFixture) -> None:
    """Created workspace can be loaded back from disk."""
    ws_fix.h_alice.create_workspace("persistent", network_access=True)
    ws = ws_fix.ws_store.load(ws_fix.alice.id, "persistent")
    assert ws.name == "persistent"
    assert ws.network_access is True


# --- delete_workspace ---


def test_delete_workspace_basic(ws_fix: WsFixture) -> None:
    """Delete own workspace."""
    ws_fix.h_alice.create_workspace("doomed")
    result = ws_fix.h_alice.delete_workspace("doomed")
    assert result == {"status": "deleted"}
    assert not ws_fix.ws_store.exists(ws_fix.alice.id, "doomed")


def test_delete_workspace_has_agents(ws_fix: WsFixture) -> None:
    """Cannot delete workspace with assigned agents."""
    ws_fix.h_alice.create_workspace("busy")
    ws_fix.ws_mapping.assign(uuid4(), ws_fix.alice.id, "busy")
    result = ws_fix.h_alice.delete_workspace("busy")
    assert "error" in result
    assert "assigned agent" in result["error"]


def test_delete_workspace_not_mutable(ws_fix: WsFixture) -> None:
    """Cannot delete workspace in parent's (read-only) scope."""
    ws_fix.h_root.create_workspace("protected")
    result = ws_fix.h_alice.delete_workspace("../protected")
    assert "error" in result
    assert "mutable" in result["error"]


def test_delete_workspace_not_found(ws_fix: WsFixture) -> None:
    """Delete nonexistent workspace fails."""
    result = ws_fix.h_alice.delete_workspace("ghost")
    assert "error" in result
    assert "not found" in result["error"]


def test_delete_workspace_removed_from_disk(ws_fix: WsFixture) -> None:
    """Deleted workspace directory is removed."""
    ws_fix.h_alice.create_workspace("ephemeral")
    ws_dir = ws_fix.ws_store.workspace_dir(ws_fix.alice.id, "ephemeral")
    assert ws_dir.exists()
    ws_fix.h_alice.delete_workspace("ephemeral")
    assert not ws_dir.exists()


def test_delete_workspace_child_scope(ws_fix: WsFixture) -> None:
    """Parent can delete workspace in child's scope."""
    ws_fix.h_alice.create_workspace("child-ws")
    result = ws_fix.h_root.delete_workspace("alice/child-ws")
    assert result == {"status": "deleted"}


# --- link_dir ---


def test_link_dir_basic(ws_fix: WsFixture) -> None:
    """Link a directory from caller's workspace into target workspace."""
    ws_fix.h_alice.create_workspace("src-ws")
    ws_fix.h_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    # Create a directory inside the source workspace to link.
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    result = ws_fix.h_alice.link_dir("dst-ws", "data", "/mnt/data")
    assert result == {"status": "linked"}
    dst_ws = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert len(dst_ws.links) == 1
    assert dst_ws.links[0].mount_path.as_posix() == "/mnt/data"


def test_link_dir_caller_no_workspace(ws_fix: WsFixture) -> None:
    """Caller without workspace cannot link."""
    ws_fix.h_alice.create_workspace("target")
    result = ws_fix.h_alice.link_dir("target", "data", "/mnt/data")
    assert "error" in result
    assert "no workspace" in result["error"]


def test_link_dir_source_not_found(ws_fix: WsFixture) -> None:
    """Source path must exist in caller's workspace."""
    ws_fix.h_alice.create_workspace("src-ws")
    ws_fix.h_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    result = ws_fix.h_alice.link_dir("dst-ws", "nonexistent", "/mnt/x")
    assert "error" in result
    assert "does not exist" in result["error"]


def test_link_dir_target_not_mutable(ws_fix: WsFixture) -> None:
    """Cannot link into parent's workspace (read-only scope)."""
    ws_fix.h_alice.create_workspace("src-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    ws_fix.h_root.create_workspace("protected")
    result = ws_fix.h_alice.link_dir("../protected", "data", "/mnt/data")
    assert "error" in result
    assert "mutable" in result["error"]


def test_link_dir_target_not_found(ws_fix: WsFixture) -> None:
    """Link into nonexistent target workspace fails."""
    ws_fix.h_alice.create_workspace("src-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    result = ws_fix.h_alice.link_dir("ghost", "data", "/mnt/data")
    assert "error" in result
    assert "not found" in result["error"]


def test_link_dir_mode_default(ws_fix: WsFixture) -> None:
    """Default link mode is ro."""
    ws_fix.h_alice.create_workspace("src-ws")
    ws_fix.h_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "stuff").mkdir(parents=True)
    ws_fix.h_alice.link_dir("dst-ws", "stuff", "/mnt/stuff")
    dst_ws = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert dst_ws.links[0].mode == "ro"


def test_link_dir_persists(ws_fix: WsFixture) -> None:
    """Link survives a reload from disk."""
    ws_fix.h_alice.create_workspace("src-ws")
    ws_fix.h_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "code").mkdir(parents=True)
    ws_fix.h_alice.link_dir("dst-ws", "code", "/src", mode="rw")
    reloaded = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert len(reloaded.links) == 1
    assert reloaded.links[0].mode == "rw"


# --- unlink_dir ---


def test_unlink_dir_basic(ws_fix: WsFixture) -> None:
    """Unlink removes the link entry."""
    ws_fix.h_alice.create_workspace("src-ws")
    ws_fix.h_alice.create_workspace("dst-ws")
    ws_fix.ws_mapping.assign(ws_fix.alice.id, ws_fix.alice.id, "src-ws")
    src_ws = ws_fix.ws_store.load(ws_fix.alice.id, "src-ws")
    (src_ws.root_path / "data").mkdir(parents=True)
    ws_fix.h_alice.link_dir("dst-ws", "data", "/mnt/data")
    result = ws_fix.h_alice.unlink_dir("dst-ws", "/mnt/data")
    assert result == {"status": "unlinked"}
    dst_ws = ws_fix.ws_store.load(ws_fix.alice.id, "dst-ws")
    assert len(dst_ws.links) == 0


def test_unlink_dir_not_mutable(ws_fix: WsFixture) -> None:
    """Cannot unlink from parent's workspace."""
    ws_fix.h_root.create_workspace("parent-ws")
    result = ws_fix.h_alice.unlink_dir("../parent-ws", "/some/path")
    assert "error" in result
    assert "mutable" in result["error"]


def test_unlink_dir_not_found(ws_fix: WsFixture) -> None:
    """Unlink from nonexistent workspace fails."""
    result = ws_fix.h_alice.unlink_dir("ghost", "/path")
    assert "error" in result
    assert "not found" in result["error"]


def test_unlink_dir_no_link_at_path(ws_fix: WsFixture) -> None:
    """Unlink with no matching mount_path fails."""
    ws_fix.h_alice.create_workspace("ws")
    result = ws_fix.h_alice.unlink_dir("ws", "/nonexistent")
    assert "error" in result
    assert "no link" in result["error"]


# --- spawn_agent with workspace ---


def test_spawn_with_workspace_assigns(ws_fix: WsFixture) -> None:
    """Spawn with workspace assigns the child and sets AgentNode.workspace."""
    ws_fix.h_root.create_workspace("child-env")
    result = ws_fix.h_root.spawn_agent("eve", "work", workspace="child-env")
    assert result["status"] == "accepted"
    assert result["workspace"] == "child-env"
    child_id = UUID(result["agent_id"])
    child = ws_fix.tree.get(child_id)
    assert child.workspace == (ws_fix.root.id, "child-env")
    assert child_id in ws_fix.ws_mapping


def test_spawn_with_workspace_mapping_entry(ws_fix: WsFixture) -> None:
    """Mapping tracks the assignment after spawn."""
    ws_fix.h_root.create_workspace("env")
    result = ws_fix.h_root.spawn_agent("fred", "go", workspace="env")
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
    ws_fix.h_bob.create_workspace("secret")
    # Alice can't see bob's scope.
    result = ws_fix.h_alice.spawn_agent("spy", "go", workspace="bob/secret")
    assert "error" in result


def test_spawn_without_workspace_no_key(ws_fix: WsFixture) -> None:
    """Spawn without workspace doesn't set workspace or return key."""
    result = ws_fix.h_root.spawn_agent("plain", "go")
    assert result["status"] == "accepted"
    assert "workspace" not in result
    child_id = UUID(result["agent_id"])
    child = ws_fix.tree.get(child_id)
    assert child.workspace is None
