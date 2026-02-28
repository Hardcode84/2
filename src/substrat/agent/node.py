# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent node data model and state machine."""

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


class AgentState(enum.Enum):
    IDLE = "idle"
    BUSY = "busy"
    WAITING = "waiting"
    TERMINATED = "terminated"


# Valid state transitions.
_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.IDLE: frozenset({AgentState.BUSY, AgentState.TERMINATED}),
    AgentState.BUSY: frozenset(
        {AgentState.IDLE, AgentState.WAITING, AgentState.TERMINATED}
    ),
    AgentState.WAITING: frozenset({AgentState.BUSY, AgentState.TERMINATED}),
    AgentState.TERMINATED: frozenset(),
}


class AgentStateError(Exception):
    """Raised on invalid agent state transition."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class AgentNode:
    """A single agent in the hierarchy. Knows nothing about messages or routing."""

    session_id: UUID  # 1:1 backing session. Required.
    id: UUID = field(default_factory=uuid4)
    name: str = ""
    parent_id: UUID | None = None
    children: list[UUID] = field(default_factory=list)
    instructions: str = ""
    workspace_id: UUID | None = None
    state: AgentState = AgentState.IDLE
    created_at: str = field(default_factory=_now_iso)

    def transition(self, target: AgentState) -> None:
        """Transition to a new state. Raises AgentStateError if invalid."""
        allowed = _TRANSITIONS[self.state]
        if target not in allowed:
            msg = f"{self.state.value} → {target.value}"
            raise AgentStateError(msg)
        self.state = target

    def activate(self) -> None:
        """IDLE → BUSY."""
        self.transition(AgentState.BUSY)

    def finish(self) -> None:
        """BUSY → IDLE."""
        self.transition(AgentState.IDLE)

    def wait(self) -> None:
        """BUSY → WAITING."""
        self.transition(AgentState.WAITING)

    def terminate(self) -> None:
        """Any non-terminated → TERMINATED."""
        self.transition(AgentState.TERMINATED)
