# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the messaging layer: envelope, routing, inbox."""

from uuid import uuid4

import pytest

from substrat.agent import (
    SYSTEM,
    USER,
    AgentNode,
    AgentTree,
    Inbox,
    MessageEnvelope,
    MessageKind,
    RoutingError,
    is_sentinel,
    reachable_set,
    resolve_broadcast,
    validate_route,
)


@pytest.fixture()
def populated_tree() -> tuple[
    AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
]:
    """Build a tree: root -> {alice, bob, carol}, carol -> dave."""
    tree = AgentTree()
    root = AgentNode(session_id=uuid4(), name="root")
    alice = AgentNode(session_id=uuid4(), name="alice", parent_id=root.id)
    bob = AgentNode(session_id=uuid4(), name="bob", parent_id=root.id)
    carol = AgentNode(session_id=uuid4(), name="carol", parent_id=root.id)
    dave = AgentNode(session_id=uuid4(), name="dave", parent_id=carol.id)
    tree.add(root)
    tree.add(alice)
    tree.add(bob)
    tree.add(carol)
    tree.add(dave)
    return tree, root, alice, bob, carol, dave


# --- MessageEnvelope + sentinels ---


class TestMessageEnvelope:
    def test_defaults(self) -> None:
        sender_id = uuid4()
        msg = MessageEnvelope(sender=sender_id)
        assert msg.sender == sender_id
        assert msg.kind == MessageKind.REQUEST
        assert msg.recipient is None
        assert msg.reply_to is None
        assert msg.payload == ""
        assert msg.metadata == {}
        # Auto-generated fields.
        assert msg.id is not None
        assert msg.timestamp != ""

    def test_explicit_fields(self) -> None:
        sid = uuid4()
        mid = uuid4()
        rid = uuid4()
        ref = uuid4()
        msg = MessageEnvelope(
            sender=sid,
            id=mid,
            timestamp="2026-01-01T00:00:00+00:00",
            recipient=rid,
            reply_to=ref,
            kind=MessageKind.RESPONSE,
            payload="hello",
            metadata={"tag": "test"},
        )
        assert msg.sender == sid
        assert msg.id == mid
        assert msg.timestamp == "2026-01-01T00:00:00+00:00"
        assert msg.recipient == rid
        assert msg.reply_to == ref
        assert msg.kind == MessageKind.RESPONSE
        assert msg.payload == "hello"
        assert msg.metadata == {"tag": "test"}

    def test_sentinel_system(self) -> None:
        assert is_sentinel(SYSTEM) is True

    def test_sentinel_user(self) -> None:
        assert is_sentinel(USER) is True

    def test_non_sentinel(self) -> None:
        assert is_sentinel(uuid4()) is False


# --- reachable_set ---


class TestReachableSet:
    def test_from_root(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, alice, bob, carol, _dave = populated_tree
        assert reachable_set(tree, root.id) == {alice.id, bob.id, carol.id}

    def test_from_alice(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, alice, bob, carol, _dave = populated_tree
        assert reachable_set(tree, alice.id) == {root.id, bob.id, carol.id}

    def test_from_carol(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, alice, bob, carol, dave = populated_tree
        # Parent + siblings + child.
        assert reachable_set(tree, carol.id) == {root.id, alice.id, bob.id, dave.id}

    def test_from_dave(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, _root, _alice, _bob, carol, dave = populated_tree
        assert reachable_set(tree, dave.id) == {carol.id}


# --- validate_route ---


class TestValidateRoute:
    def test_parent_to_child(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, alice, _bob, _carol, _dave = populated_tree
        validate_route(tree, root.id, alice.id)  # Should not raise.

    def test_sibling_to_sibling(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, _root, alice, bob, _carol, _dave = populated_tree
        validate_route(tree, alice.id, bob.id)  # Should not raise.

    def test_skip_level_raises(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, _alice, _bob, _carol, dave = populated_tree
        with pytest.raises(RoutingError, match="cannot reach"):
            validate_route(tree, root.id, dave.id)

    def test_system_bypasses_one_hop(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, _root, _alice, _bob, _carol, dave = populated_tree
        validate_route(tree, SYSTEM, dave.id)  # Should not raise.

    def test_child_to_parent(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, alice, _bob, _carol, _dave = populated_tree
        validate_route(tree, alice.id, root.id)  # Should not raise.

    def test_missing_recipient_raises(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, _alice, _bob, _carol, _dave = populated_tree
        with pytest.raises(RoutingError, match="not in tree"):
            validate_route(tree, root.id, uuid4())

    def test_missing_sender_raises(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, _root, alice, _bob, _carol, _dave = populated_tree
        with pytest.raises(RoutingError, match="not in tree"):
            validate_route(tree, uuid4(), alice.id)


# --- resolve_broadcast ---


class TestResolveBroadcast:
    def test_from_alice(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, _root, alice, bob, carol, _dave = populated_tree
        targets = resolve_broadcast(tree, alice.id)
        assert set(targets) == {bob.id, carol.id}

    def test_from_root_raises(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, root, _alice, _bob, _carol, _dave = populated_tree
        with pytest.raises(RoutingError, match="no siblings"):
            resolve_broadcast(tree, root.id)

    def test_sentinel_raises(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, _root, _alice, _bob, _carol, _dave = populated_tree
        with pytest.raises(RoutingError, match="sentinels cannot broadcast"):
            resolve_broadcast(tree, SYSTEM)

    def test_missing_sender_raises(
        self,
        populated_tree: tuple[
            AgentTree, AgentNode, AgentNode, AgentNode, AgentNode, AgentNode
        ],
    ) -> None:
        tree, _root, _alice, _bob, _carol, _dave = populated_tree
        with pytest.raises(RoutingError, match="not in tree"):
            resolve_broadcast(tree, uuid4())


# --- Inbox ---


class TestInbox:
    def test_deliver_and_collect(self) -> None:
        inbox = Inbox()
        m1 = MessageEnvelope(sender=uuid4(), payload="first")
        m2 = MessageEnvelope(sender=uuid4(), payload="second")
        inbox.deliver(m1)
        inbox.deliver(m2)
        assert len(inbox) == 2
        collected = inbox.collect()
        assert collected == [m1, m2]
        assert len(inbox) == 0

    def test_peek_is_non_destructive(self) -> None:
        inbox = Inbox()
        msg = MessageEnvelope(sender=uuid4(), payload="peek")
        inbox.deliver(msg)
        assert inbox.peek() == [msg]
        assert len(inbox) == 1

    def test_empty_inbox(self) -> None:
        inbox = Inbox()
        assert len(inbox) == 0
        assert not inbox
        assert inbox.collect() == []
