# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent tree — pure in-memory hierarchy with structural queries."""

from __future__ import annotations

from uuid import UUID

from substrat.agent.node import AgentNode


class AgentTree:
    """Maintains parent-child relationships between agents.

    No routing, no persistence, no I/O. Just the tree and queries on it.
    """

    def __init__(self) -> None:
        self._nodes: dict[UUID, AgentNode] = {}

    def add(self, node: AgentNode) -> None:
        """Insert a node into the tree.

        Raises ValueError if the id already exists, the parent is missing,
        or a sibling with the same name already exists.
        """
        if node.id in self._nodes:
            raise ValueError(f"duplicate agent id: {node.id}")
        if node.parent_id is not None:
            parent = self._nodes.get(node.parent_id)
            if parent is None:
                raise ValueError(f"parent {node.parent_id} not in tree")
            # Check sibling name uniqueness.
            if node.name:
                self._check_name_collision(node.name, parent.children)
            parent.children.append(node.id)
        else:
            # Roots are siblings of each other for name-uniqueness purposes.
            if node.name:
                root_ids = [
                    nid for nid, n in self._nodes.items() if n.parent_id is None
                ]
                self._check_name_collision(node.name, root_ids)
        self._nodes[node.id] = node

    def _check_name_collision(self, name: str, sibling_ids: list[UUID]) -> None:
        for sid in sibling_ids:
            if self._nodes[sid].name == name:
                raise ValueError(f"sibling name collision: {name!r}")

    def remove(self, agent_id: UUID) -> AgentNode:
        """Remove a leaf node from the tree and return it.

        Raises KeyError if missing, ValueError if the node has children.
        """
        node = self._nodes[agent_id]  # KeyError if missing.
        if node.children:
            raise ValueError(f"agent {agent_id} has children; remove them first")
        if node.parent_id is not None:
            parent = self._nodes.get(node.parent_id)
            if parent is not None:
                parent.children.remove(agent_id)
        del self._nodes[agent_id]
        return node

    def get(self, agent_id: UUID) -> AgentNode:
        """Return a node by id. Raises KeyError if missing."""
        return self._nodes[agent_id]

    def __contains__(self, agent_id: UUID) -> bool:
        return agent_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def parent(self, agent_id: UUID) -> AgentNode | None:
        """Return the parent node, or None for roots."""
        node = self._nodes[agent_id]
        if node.parent_id is None:
            return None
        return self._nodes[node.parent_id]

    def children(self, agent_id: UUID) -> list[AgentNode]:
        """Return direct children as nodes."""
        node = self._nodes[agent_id]
        return [self._nodes[cid] for cid in node.children]

    def team(self, agent_id: UUID) -> list[AgentNode]:
        """Return siblings excluding self. Empty for roots."""
        node = self._nodes[agent_id]
        if node.parent_id is None:
            return []
        parent = self._nodes[node.parent_id]
        return [self._nodes[cid] for cid in parent.children if cid != agent_id]

    def roots(self) -> list[AgentNode]:
        """Return all root nodes (no parent)."""
        return [n for n in self._nodes.values() if n.parent_id is None]

    def subtree(self, agent_id: UUID) -> list[AgentNode]:
        """Return all descendants depth-first. Does not include the node itself."""
        node = self._nodes[agent_id]
        result: list[AgentNode] = []
        stack = list(reversed(node.children))
        while stack:
            nid = stack.pop()
            child = self._nodes[nid]
            result.append(child)
            stack.extend(reversed(child.children))
        return result
