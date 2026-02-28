# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Pure-function message routing on top of the agent tree.

No mutable state, no I/O. Validates one-hop reachability and resolves
broadcast targets.
"""

from __future__ import annotations

from uuid import UUID

from substrat.agent.message import is_sentinel
from substrat.agent.tree import AgentTree


class RoutingError(Exception):
    """Raised when a message cannot be routed."""


def reachable_set(tree: AgentTree, agent_id: UUID) -> set[UUID]:
    """Return the set of agent ids reachable in one hop (parent + children + siblings).

    Does not include *agent_id* itself.
    """
    result: set[UUID] = set()
    node = tree.get(agent_id)
    if node.parent_id is not None:
        result.add(node.parent_id)
    result.update(node.children)
    for sibling in tree.team(agent_id):
        result.add(sibling.id)
    return result


def validate_route(tree: AgentTree, sender: UUID, recipient: UUID) -> None:
    """Raise :class:`RoutingError` if *sender* cannot reach *recipient*.

    Sentinels (SYSTEM, USER) bypass the one-hop constraint but the
    recipient must still exist in the tree.
    """
    if recipient not in tree:
        raise RoutingError(f"recipient {recipient} not in tree")
    if is_sentinel(sender):
        return
    if sender not in tree:
        raise RoutingError(f"sender {sender} not in tree")
    if recipient not in reachable_set(tree, sender):
        raise RoutingError(f"{sender} cannot reach {recipient}")


def resolve_broadcast(tree: AgentTree, sender: UUID) -> list[UUID]:
    """Return the list of sibling ids for a broadcast from *sender*.

    Sentinels cannot broadcast (they have no position in the tree).
    Raises :class:`RoutingError` if the sender has no siblings.
    """
    if is_sentinel(sender):
        raise RoutingError("sentinels cannot broadcast")
    if sender not in tree:
        raise RoutingError(f"sender {sender} not in tree")
    siblings = tree.team(sender)
    if not siblings:
        raise RoutingError(f"{sender} has no siblings")
    return [s.id for s in siblings]
