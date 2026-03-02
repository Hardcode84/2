# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Bidirectional agent-workspace index. Pure in-memory state."""

from uuid import UUID

# (scope, name) — the workspace's unique key.
WorkspaceKey = tuple[UUID, str]


class WorkspaceMapping:
    """Tracks which agents are assigned to which workspaces.

    An agent has at most one workspace. A workspace can have multiple agents.
    """

    def __init__(self) -> None:
        self._by_agent: dict[UUID, WorkspaceKey] = {}
        self._by_workspace: dict[WorkspaceKey, set[UUID]] = {}

    def assign(self, agent_id: UUID, scope: UUID, name: str) -> None:
        """Assign an agent to a workspace. Raises ValueError if already assigned."""
        if agent_id in self._by_agent:
            raise ValueError(f"agent {agent_id} already assigned")
        key: WorkspaceKey = (scope, name)
        self._by_agent[agent_id] = key
        self._by_workspace.setdefault(key, set()).add(agent_id)

    def unassign(self, agent_id: UUID) -> None:
        """Remove an agent's workspace assignment. Raises KeyError if not assigned."""
        key = self._by_agent.pop(agent_id)  # KeyError if missing.
        agents = self._by_workspace[key]
        agents.discard(agent_id)
        if not agents:
            del self._by_workspace[key]

    def get(self, agent_id: UUID) -> WorkspaceKey | None:
        """Return the agent's workspace key, or None."""
        return self._by_agent.get(agent_id)

    def agents_in(self, scope: UUID, name: str) -> frozenset[UUID]:
        """Return all agents assigned to this workspace."""
        return frozenset(self._by_workspace.get((scope, name), ()))

    def __len__(self) -> int:
        return len(self._by_agent)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._by_agent
