# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Message envelope and well-known sentinel identities."""

import enum
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from substrat import now_iso

# Re-export sentinels so existing `from substrat.agent.message import USER` works.
from substrat.model import SYSTEM as SYSTEM
from substrat.model import USER as USER
from substrat.model import is_sentinel as is_sentinel
from substrat.model import sentinel_name as sentinel_name


class MessageKind(enum.Enum):
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    ERROR = "error"


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
    kind: MessageKind = MessageKind.NOTIFICATION
    payload: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    # Kinds that require a recipient.
    _DIRECTED_KINDS: frozenset[MessageKind] = frozenset(
        {MessageKind.REQUEST, MessageKind.RESPONSE, MessageKind.ERROR}
    )

    def __post_init__(self) -> None:
        if self.kind in self._DIRECTED_KINDS and self.recipient is None:
            msg = f"{self.kind.value} envelope requires a recipient"
            raise ValueError(msg)
