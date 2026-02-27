# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Session data model and state machine."""

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


class SessionState(enum.Enum):
    CREATED = "created"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


# Valid state transitions.
_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset({SessionState.ACTIVE}),
    SessionState.ACTIVE: frozenset({SessionState.SUSPENDED, SessionState.TERMINATED}),
    SessionState.SUSPENDED: frozenset({SessionState.ACTIVE, SessionState.TERMINATED}),
    SessionState.TERMINATED: frozenset(),
}


class SessionStateError(Exception):
    """Raised on invalid state transition."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Session:
    """A single provider session. Knows nothing about agents or messages."""

    id: UUID = field(default_factory=uuid4)
    state: SessionState = SessionState.CREATED
    provider_name: str = ""
    model: str = ""
    created_at: str = field(default_factory=_now_iso)
    suspended_at: str | None = None
    provider_state: bytes = b""

    def transition(self, target: SessionState) -> None:
        """Transition to a new state. Raises SessionStateError if invalid."""
        allowed = _TRANSITIONS[self.state]
        if target not in allowed:
            msg = f"{self.state.value} → {target.value}"
            raise SessionStateError(msg)
        if target == SessionState.SUSPENDED:
            self.suspended_at = _now_iso()
        self.state = target

    def activate(self) -> None:
        """CREATED/SUSPENDED → ACTIVE."""
        self.transition(SessionState.ACTIVE)
        self.suspended_at = None

    def suspend(self, provider_state: bytes) -> None:
        """ACTIVE → SUSPENDED. Stores the provider's opaque state blob."""
        self.transition(SessionState.SUSPENDED)
        self.provider_state = provider_state

    def terminate(self) -> None:
        """ACTIVE/SUSPENDED → TERMINATED."""
        self.transition(SessionState.TERMINATED)
