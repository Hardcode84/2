# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-agent message inbox. Sync, deque-backed, daemon wraps with asyncio later."""

from __future__ import annotations

from collections import deque
from uuid import UUID

from substrat.agent.message import MessageEnvelope, MessageKind


class Inbox:
    """FIFO queue for inbound messages."""

    def __init__(self) -> None:
        self._queue: deque[MessageEnvelope] = deque()

    def deliver(self, envelope: MessageEnvelope) -> None:
        """Append a message to the inbox."""
        self._queue.append(envelope)

    def collect(
        self,
        *,
        sender: UUID | None = None,
        kind: MessageKind | None = None,
    ) -> list[MessageEnvelope]:
        """Drain messages in FIFO order, optionally filtering.

        With no filters, drains everything (fast path). With filters,
        pops matching messages and leaves the rest in the deque.

        Not atomic — assumes single-threaded access.
        """
        if sender is None and kind is None:
            items = list(self._queue)
            self._queue.clear()
            return items
        # Filtered path: iterate once, partition into matched/kept.
        matched: list[MessageEnvelope] = []
        kept: deque[MessageEnvelope] = deque()
        for envelope in self._queue:
            if (sender is not None and envelope.sender != sender) or (
                kind is not None and envelope.kind != kind
            ):
                kept.append(envelope)
            else:
                matched.append(envelope)
        self._queue = kept
        return matched

    def peek(self) -> list[MessageEnvelope]:
        """Return all messages without removing them."""
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)

    def __bool__(self) -> bool:
        return bool(self._queue)
