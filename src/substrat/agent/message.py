# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Message envelope and well-known sentinel identities."""

import enum
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from substrat import now_iso

# Daemon and CLI get deterministic UUIDs so they serialise cleanly.
SYSTEM: UUID = UUID(int=0)
USER: UUID = UUID(int=1)
_SENTINELS: frozenset[UUID] = frozenset({SYSTEM, USER})


def is_sentinel(agent_id: UUID) -> bool:
    """True for SYSTEM and USER pseudo-identities."""
    return agent_id in _SENTINELS


class MessageKind(enum.Enum):
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    MULTICAST = "multicast"


@dataclass
class MessageEnvelope:
    """Wire format for inter-agent messages.

    ``sender`` is positional and required — every message has an origin.
    """

    sender: UUID
    id: UUID = field(default_factory=uuid4)
    timestamp: str = field(default_factory=now_iso)
    recipient: UUID | None = None
    reply_to: UUID | None = None
    kind: MessageKind = MessageKind.REQUEST
    payload: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
