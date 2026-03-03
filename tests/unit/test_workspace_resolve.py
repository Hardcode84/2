# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for workspace name resolution and access predicates."""

from uuid import UUID, uuid4

import pytest

from substrat.agent import AgentNode, AgentTree
from substrat.model import USER
from substrat.workspace.resolve import mutable_scopes, resolve, visible_scopes


@pytest.fixture()
def tree() -> AgentTree:
    """Tree: root -> {alice, bob}, alice -> dave."""
    t = AgentTree()
    root = AgentNode(session_id=uuid4(), name="root")
    alice = AgentNode(session_id=uuid4(), name="alice", parent_id=root.id)
    bob = AgentNode(session_id=uuid4(), name="bob", parent_id=root.id)
    dave = AgentNode(session_id=uuid4(), name="dave", parent_id=alice.id)
    for n in (root, alice, bob, dave):
        t.add(n)
    return t


def _get(tree: AgentTree, name: str) -> AgentNode:
    """Find a node by name. Test helper, not production code."""
    for node in tree.roots():
        if node.name == name:
            return node
        for desc in tree.subtree(node.id):
            if desc.name == name:
                return desc
    raise KeyError(name)


def _parent_id(tree: AgentTree, node: AgentNode) -> UUID | None:
    """Extract parent UUID from the tree."""
    parent = tree.parent(node.id)
    return parent.id if parent else None


def _child_lookup(tree: AgentTree, node: AgentNode):
    """Build a child-name-to-UUID lookup for a node."""
    return lambda name: tree.child_by_name(node.id, name).id


# --- resolve ---


def test_resolve_own_scope(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    scope, name = resolve(
        alice.id,
        "my-ws",
        parent_id=_parent_id(tree, alice),
        child_lookup=_child_lookup(tree, alice),
    )
    assert scope == alice.id
    assert name == "my-ws"


def test_resolve_child_scope(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    dave = _get(tree, "dave")
    scope, name = resolve(
        alice.id,
        "dave/output",
        parent_id=_parent_id(tree, alice),
        child_lookup=_child_lookup(tree, alice),
    )
    assert scope == dave.id
    assert name == "output"


def test_resolve_parent_scope(tree: AgentTree) -> None:
    root = _get(tree, "root")
    alice = _get(tree, "alice")
    scope, name = resolve(
        alice.id,
        "../shared",
        parent_id=_parent_id(tree, alice),
        child_lookup=_child_lookup(tree, alice),
    )
    assert scope == root.id
    assert name == "shared"


def test_resolve_parent_scope_from_root(tree: AgentTree) -> None:
    root = _get(tree, "root")
    scope, name = resolve(
        root.id,
        "../shared",
        parent_id=_parent_id(tree, root),
        child_lookup=_child_lookup(tree, root),
    )
    assert scope == USER
    assert name == "shared"


def test_resolve_bad_child_name(tree: AgentTree) -> None:
    root = _get(tree, "root")
    with pytest.raises(KeyError):
        resolve(
            root.id,
            "ghost/ws",
            parent_id=_parent_id(tree, root),
            child_lookup=_child_lookup(tree, root),
        )


def test_resolve_too_many_hops(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    with pytest.raises(ValueError, match="malformed"):
        resolve(
            alice.id,
            "../../ws",
            parent_id=_parent_id(tree, alice),
            child_lookup=_child_lookup(tree, alice),
        )


def test_resolve_empty_ref(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    with pytest.raises(ValueError, match="empty"):
        resolve(
            alice.id,
            "",
            parent_id=_parent_id(tree, alice),
            child_lookup=_child_lookup(tree, alice),
        )


def test_resolve_trailing_slash(tree: AgentTree) -> None:
    root = _get(tree, "root")
    with pytest.raises(ValueError, match="missing workspace name"):
        resolve(
            root.id,
            "alice/",
            parent_id=_parent_id(tree, root),
            child_lookup=_child_lookup(tree, root),
        )


def test_resolve_dot_segment(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    with pytest.raises(ValueError, match="invalid path segment"):
        resolve(
            alice.id,
            ".",
            parent_id=_parent_id(tree, alice),
            child_lookup=_child_lookup(tree, alice),
        )


def test_resolve_bare_dotdot(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    with pytest.raises(ValueError, match="malformed"):
        resolve(
            alice.id,
            "..",
            parent_id=_parent_id(tree, alice),
            child_lookup=_child_lookup(tree, alice),
        )


def test_resolve_dot_as_name(tree: AgentTree) -> None:
    root = _get(tree, "root")
    with pytest.raises(ValueError, match="invalid path segment"):
        resolve(
            root.id,
            "alice/.",
            parent_id=_parent_id(tree, root),
            child_lookup=_child_lookup(tree, root),
        )


def test_resolve_dotdot_as_name(tree: AgentTree) -> None:
    root = _get(tree, "root")
    with pytest.raises(ValueError, match="invalid path segment"):
        resolve(
            root.id,
            "alice/..",
            parent_id=_parent_id(tree, root),
            child_lookup=_child_lookup(tree, root),
        )


def test_resolve_dot_slash_child(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    with pytest.raises(ValueError, match="invalid path segment"):
        resolve(
            alice.id,
            "./foo",
            parent_id=_parent_id(tree, alice),
            child_lookup=_child_lookup(tree, alice),
        )


# --- visible_scopes ---


def test_visible_from_root(tree: AgentTree) -> None:
    root = _get(tree, "root")
    alice = _get(tree, "alice")
    bob = _get(tree, "bob")
    vis = visible_scopes(root.id, root.children, _parent_id(tree, root))
    assert root.id in vis
    assert alice.id in vis
    assert bob.id in vis
    assert USER in vis


def test_visible_from_child(tree: AgentTree) -> None:
    root = _get(tree, "root")
    alice = _get(tree, "alice")
    dave = _get(tree, "dave")
    vis = visible_scopes(alice.id, alice.children, _parent_id(tree, alice))
    assert alice.id in vis
    assert dave.id in vis
    assert root.id in vis


def test_visible_from_leaf(tree: AgentTree) -> None:
    alice = _get(tree, "alice")
    dave = _get(tree, "dave")
    vis = visible_scopes(dave.id, dave.children, _parent_id(tree, dave))
    assert dave.id in vis
    assert alice.id in vis
    assert len(vis) == 2  # Own + parent, no children.


# --- mutable_scopes ---


def test_mutable_from_root(tree: AgentTree) -> None:
    root = _get(tree, "root")
    alice = _get(tree, "alice")
    bob = _get(tree, "bob")
    mut = mutable_scopes(root.id, root.children)
    assert root.id in mut
    assert alice.id in mut
    assert bob.id in mut
    assert USER not in mut


def test_mutable_from_child(tree: AgentTree) -> None:
    root = _get(tree, "root")
    alice = _get(tree, "alice")
    dave = _get(tree, "dave")
    mut = mutable_scopes(alice.id, alice.children)
    assert alice.id in mut
    assert dave.id in mut
    assert root.id not in mut
