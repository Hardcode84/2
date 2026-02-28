# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the AgentNode state machine and AgentTree hierarchy."""

from uuid import uuid4

import pytest

from substrat.agent import AgentNode, AgentState, AgentStateError, AgentTree

# --- AgentNode state machine ---


def test_valid_transitions() -> None:
    node = AgentNode(session_id=uuid4())
    assert node.state == AgentState.IDLE
    node.transition(AgentState.BUSY)
    assert node.state == AgentState.BUSY
    node.transition(AgentState.IDLE)
    assert node.state == AgentState.IDLE
    node.transition(AgentState.BUSY)
    node.transition(AgentState.WAITING)
    assert node.state == AgentState.WAITING
    node.transition(AgentState.BUSY)
    assert node.state == AgentState.BUSY


def test_invalid_transition() -> None:
    node = AgentNode(session_id=uuid4())
    with pytest.raises(AgentStateError, match="idle → waiting"):
        node.transition(AgentState.WAITING)


def test_terminated_is_absorbing() -> None:
    node = AgentNode(session_id=uuid4())
    node.terminate()
    assert node.state == AgentState.TERMINATED
    with pytest.raises(AgentStateError):
        node.transition(AgentState.IDLE)
    with pytest.raises(AgentStateError):
        node.transition(AgentState.BUSY)


def test_convenience_methods() -> None:
    node = AgentNode(session_id=uuid4())
    node.activate()
    assert node.state == AgentState.BUSY
    node.wait()
    assert node.state == AgentState.WAITING
    node.activate()  # WAITING → BUSY via transition.
    node.finish()
    assert node.state == AgentState.IDLE
    node.terminate()
    assert node.state == AgentState.TERMINATED


# --- AgentTree.add ---


@pytest.fixture()
def tree() -> AgentTree:
    return AgentTree()


def test_add_root(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    tree.add(root)
    assert root in tree.roots()
    assert len(tree) == 1


def test_add_child(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    child = AgentNode(session_id=uuid4(), name="child", parent_id=root.id)
    tree.add(root)
    tree.add(child)
    assert child.id in root.children
    assert tree.parent(child.id) is root


def test_add_duplicate_id(tree: AgentTree) -> None:
    node = AgentNode(session_id=uuid4())
    tree.add(node)
    dupe = AgentNode(session_id=uuid4(), id=node.id)
    with pytest.raises(ValueError, match="duplicate"):
        tree.add(dupe)


def test_add_missing_parent(tree: AgentTree) -> None:
    orphan = AgentNode(session_id=uuid4(), parent_id=uuid4())
    with pytest.raises(ValueError, match="not in tree"):
        tree.add(orphan)


def test_add_sibling_name_collision(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    a = AgentNode(session_id=uuid4(), name="worker", parent_id=root.id)
    b = AgentNode(session_id=uuid4(), name="worker", parent_id=root.id)
    tree.add(root)
    tree.add(a)
    with pytest.raises(ValueError, match="sibling name collision"):
        tree.add(b)


def test_root_name_collision(tree: AgentTree) -> None:
    r1 = AgentNode(session_id=uuid4(), name="boss")
    r2 = AgentNode(session_id=uuid4(), name="boss")
    tree.add(r1)
    with pytest.raises(ValueError, match="sibling name collision"):
        tree.add(r2)


# --- AgentTree.remove ---


def test_remove_leaf(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    child = AgentNode(session_id=uuid4(), name="child", parent_id=root.id)
    tree.add(root)
    tree.add(child)
    removed = tree.remove(child.id)
    assert removed is child
    assert child.id not in root.children
    assert len(tree) == 1


def test_remove_non_leaf_raises(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    child = AgentNode(session_id=uuid4(), name="child", parent_id=root.id)
    tree.add(root)
    tree.add(child)
    with pytest.raises(ValueError, match="has children"):
        tree.remove(root.id)


def test_remove_root_leaf(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    tree.add(root)
    removed = tree.remove(root.id)
    assert removed is root
    assert len(tree) == 0


# --- Structural queries ---


def test_parent_returns_none_for_root(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4())
    tree.add(root)
    assert tree.parent(root.id) is None


def test_children_returns_nodes(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    c1 = AgentNode(session_id=uuid4(), name="a", parent_id=root.id)
    c2 = AgentNode(session_id=uuid4(), name="b", parent_id=root.id)
    tree.add(root)
    tree.add(c1)
    tree.add(c2)
    kids = tree.children(root.id)
    assert len(kids) == 2
    assert c1 in kids
    assert c2 in kids


def test_team_excludes_self(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    a = AgentNode(session_id=uuid4(), name="a", parent_id=root.id)
    b = AgentNode(session_id=uuid4(), name="b", parent_id=root.id)
    c = AgentNode(session_id=uuid4(), name="c", parent_id=root.id)
    tree.add(root)
    tree.add(a)
    tree.add(b)
    tree.add(c)
    team_a = tree.team(a.id)
    assert a not in team_a
    assert b in team_a
    assert c in team_a


def test_team_of_root_is_empty(tree: AgentTree) -> None:
    r1 = AgentNode(session_id=uuid4(), name="r1")
    r2 = AgentNode(session_id=uuid4(), name="r2")
    tree.add(r1)
    tree.add(r2)
    assert tree.team(r1.id) == []
    assert tree.team(r2.id) == []


def test_roots(tree: AgentTree) -> None:
    r1 = AgentNode(session_id=uuid4(), name="r1")
    r2 = AgentNode(session_id=uuid4(), name="r2")
    child = AgentNode(session_id=uuid4(), name="c", parent_id=r1.id)
    tree.add(r1)
    tree.add(r2)
    tree.add(child)
    root_nodes = tree.roots()
    assert len(root_nodes) == 2
    assert r1 in root_nodes
    assert r2 in root_nodes
    assert child not in root_nodes


def test_subtree_dfs(tree: AgentTree) -> None:
    root = AgentNode(session_id=uuid4(), name="root")
    a = AgentNode(session_id=uuid4(), name="a", parent_id=root.id)
    b = AgentNode(session_id=uuid4(), name="b", parent_id=root.id)
    aa = AgentNode(session_id=uuid4(), name="aa", parent_id=a.id)
    ab = AgentNode(session_id=uuid4(), name="ab", parent_id=a.id)
    tree.add(root)
    tree.add(a)
    tree.add(b)
    tree.add(aa)
    tree.add(ab)
    desc = tree.subtree(root.id)
    assert len(desc) == 4
    # DFS: a before b, aa and ab before b.
    assert desc.index(a) < desc.index(b)
    assert desc.index(aa) < desc.index(b)
    assert desc.index(ab) < desc.index(b)
    assert root not in desc


# --- Edge cases ---


def test_get_missing_raises(tree: AgentTree) -> None:
    with pytest.raises(KeyError):
        tree.get(uuid4())


def test_contains_and_len(tree: AgentTree) -> None:
    assert len(tree) == 0
    node = AgentNode(session_id=uuid4())
    assert node.id not in tree
    tree.add(node)
    assert node.id in tree
    assert len(tree) == 1
