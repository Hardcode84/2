# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-agent message inbox. Sync, deque-backed, daemon wraps with asyncio later."""

from __future__ import annotations

from collections import deque

from substrat.agent.message import MessageEnvelope


class Inbox:
    """FIFO queue for inbound messages."""

    def __init__(self) -> None:
        self._queue: deque[MessageEnvelope] = deque()

    def deliver(self, envelope: MessageEnvelope) -> None:
        """Append a message to the inbox."""
        self._queue.append(envelope)

    def collect(self) -> list[MessageEnvelope]:
        """Drain all messages in FIFO order. Inbox is empty afterwards."""
        items = list(self._queue)
        self._queue.clear()
        return items

    def peek(self) -> list[MessageEnvelope]:
        """Return all messages without removing them."""
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)

    def __bool__(self) -> bool:
        return bool(self._queue)
