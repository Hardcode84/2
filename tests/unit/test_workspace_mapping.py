# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the bidirectional agent-workspace index."""

from uuid import uuid4

import pytest

from substrat.workspace import WorkspaceMapping

# --- assign / get ---


def test_assign_and_get() -> None:
    m = WorkspaceMapping()
    aid, scope = uuid4(), uuid4()
    m.assign(aid, scope, "ws")
    assert m.get(aid) == (scope, "ws")


def test_assign_duplicate_raises() -> None:
    m = WorkspaceMapping()
    aid, scope = uuid4(), uuid4()
    m.assign(aid, scope, "ws")
    with pytest.raises(ValueError, match="already assigned"):
        m.assign(aid, scope, "other")


def test_assign_same_workspace_multiple_agents() -> None:
    m = WorkspaceMapping()
    scope = uuid4()
    a1, a2 = uuid4(), uuid4()
    m.assign(a1, scope, "shared")
    m.assign(a2, scope, "shared")
    assert m.get(a1) == m.get(a2) == (scope, "shared")
    assert m.agents_in(scope, "shared") == frozenset({a1, a2})


# --- unassign ---


def test_unassign() -> None:
    m = WorkspaceMapping()
    aid, scope = uuid4(), uuid4()
    m.assign(aid, scope, "ws")
    m.unassign(aid)
    assert m.get(aid) is None


def test_unassign_missing_raises() -> None:
    m = WorkspaceMapping()
    with pytest.raises(KeyError):
        m.unassign(uuid4())


def test_unassign_cleans_reverse_index() -> None:
    m = WorkspaceMapping()
    aid, scope = uuid4(), uuid4()
    m.assign(aid, scope, "ws")
    m.unassign(aid)
    assert m.agents_in(scope, "ws") == frozenset()


# --- agents_in ---


def test_agents_in_empty() -> None:
    m = WorkspaceMapping()
    assert m.agents_in(uuid4(), "nope") == frozenset()


def test_agents_in_multiple() -> None:
    m = WorkspaceMapping()
    scope = uuid4()
    agents = {uuid4() for _ in range(3)}
    for a in agents:
        m.assign(a, scope, "ws")
    assert m.agents_in(scope, "ws") == frozenset(agents)


# --- contains / len ---


def test_contains_and_len() -> None:
    m = WorkspaceMapping()
    aid, scope = uuid4(), uuid4()
    assert aid not in m
    assert len(m) == 0
    m.assign(aid, scope, "ws")
    assert aid in m
    assert len(m) == 1
    m.unassign(aid)
    assert aid not in m
    assert len(m) == 0
