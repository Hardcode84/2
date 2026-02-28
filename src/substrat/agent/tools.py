# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tool logic layer — pure operations on the agent tree and inboxes.

Five agent-facing tools implemented as methods on ToolHandler. No wire
protocol, no I/O, no daemon. The transport wrapper comes later.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID, uuid4

from substrat.agent.inbox import Inbox
from substrat.agent.message import MessageEnvelope, MessageKind, is_sentinel
from substrat.agent.node import AgentNode
from substrat.agent.router import RoutingError, resolve_broadcast, validate_route
from substrat.agent.tree import AgentTree


class ToolError(Exception):
    """Raised when a tool call fails for a recoverable reason."""


DeferredWork = Callable[[], Coroutine[Any, Any, None]]
SpawnCallback = Callable[[AgentNode], DeferredWork]
InboxRegistry = dict[UUID, Inbox]


class ToolHandler:
    """Per-agent tool handler. One instance per agent.

    Deps injected at construction, caller_id baked in. Methods return
    result dicts on success, error dicts on recoverable failure. Programming
    bugs propagate as normal exceptions.
    """

    def __init__(
        self,
        tree: AgentTree,
        inboxes: InboxRegistry,
        caller_id: UUID,
        spawn_callback: SpawnCallback | None = None,
    ) -> None:
        self._tree = tree
        self._inboxes = inboxes
        self._caller_id = caller_id
        self._spawn_callback = spawn_callback
        self._deferred: list[DeferredWork] = []

    # --- Public tools ---

    def send_message(
        self,
        recipient: str,
        text: str,
        *,
        sync: bool = True,
    ) -> dict[str, Any]:
        """Send a message to a reachable agent by name."""
        try:
            target = self._resolve_name(recipient)
        except ToolError as exc:
            return {"error": str(exc)}
        try:
            validate_route(self._tree, self._caller_id, target.id)
        except RoutingError as exc:
            return {"error": str(exc)}
        envelope = MessageEnvelope(
            sender=self._caller_id,
            recipient=target.id,
            kind=MessageKind.REQUEST,
            payload=text,
            metadata={"sync": str(sync)},
        )
        self._deliver(target.id, envelope)
        return {
            "status": "sent",
            "message_id": str(envelope.id),
            "waiting_for_reply": sync,
        }

    def broadcast(self, text: str) -> dict[str, Any]:
        """Multicast to all siblings in the team."""
        try:
            sibling_ids = resolve_broadcast(self._tree, self._caller_id)
        except RoutingError as exc:
            return {"error": str(exc)}
        broadcast_id = uuid4()
        for sid in sibling_ids:
            envelope = MessageEnvelope(
                sender=self._caller_id,
                recipient=sid,
                kind=MessageKind.MULTICAST,
                payload=text,
                metadata={"broadcast_id": str(broadcast_id)},
            )
            self._deliver(sid, envelope)
        return {
            "status": "sent",
            "message_id": str(broadcast_id),
            "recipient_count": len(sibling_ids),
        }

    def check_inbox(self) -> dict[str, Any]:
        """Drain the caller's inbox and return messages."""
        inbox = self._inboxes.get(self._caller_id)
        if inbox is None:
            return {"messages": []}
        messages = inbox.collect()
        return {
            "messages": [
                {
                    "from": self._sender_display_name(m.sender),
                    "text": m.payload,
                    "message_id": str(m.id),
                }
                for m in messages
            ],
        }

    def spawn_agent(
        self,
        name: str,
        instructions: str,
        *,
        workspace_subdir: str | None = None,
    ) -> dict[str, Any]:
        """Create a child agent. Actual session creation is deferred."""
        child = AgentNode(
            session_id=uuid4(),
            name=name,
            parent_id=self._caller_id,
            instructions=instructions,
        )
        try:
            self._tree.add(child)
        except ValueError as exc:
            return {"error": str(exc)}
        # Eager inbox so messages sent before provider starts are queued.
        self._inboxes[child.id] = Inbox()
        if self._spawn_callback is not None:
            self._deferred.append(self._spawn_callback(child))
        return {
            "status": "accepted",
            "agent_id": str(child.id),
            "name": child.name,
        }

    def inspect_agent(self, name: str) -> dict[str, Any]:
        """View a subordinate's state and recent messages."""
        try:
            child = self._resolve_child_name(name)
        except ToolError as exc:
            return {"error": str(exc)}
        inbox = self._inboxes.get(child.id)
        recent = inbox.peek() if inbox is not None else []
        return {
            "state": child.state.value,
            "recent_messages": [
                {
                    "from": self._sender_display_name(m.sender),
                    "text": m.payload,
                    "message_id": str(m.id),
                }
                for m in recent
            ],
        }

    def drain_deferred(self) -> list[DeferredWork]:
        """Return and clear accumulated deferred callbacks."""
        work = self._deferred
        self._deferred = []
        return work

    # --- Private helpers ---

    def _resolve_name(self, name: str) -> AgentNode:
        """Search caller's one-hop neighborhood by name.

        Checks parent, children, and siblings. Raises ToolError if not found.
        """
        node = self._tree.get(self._caller_id)
        # Check parent.
        if node.parent_id is not None:
            parent = self._tree.get(node.parent_id)
            if parent.name == name:
                return parent
        # Check children.
        for child in self._tree.children(self._caller_id):
            if child.name == name:
                return child
        # Check siblings.
        for sibling in self._tree.team(self._caller_id):
            if sibling.name == name:
                return sibling
        raise ToolError(f"no reachable agent named {name!r}")

    def _resolve_child_name(self, name: str) -> AgentNode:
        """Resolve name among direct children only. Raises ToolError."""
        for child in self._tree.children(self._caller_id):
            if child.name == name:
                return child
        raise ToolError(f"no child agent named {name!r}")

    def _sender_display_name(self, sender_id: UUID) -> str:
        """Human-readable sender name. Falls back to UUID string for sentinels."""
        if is_sentinel(sender_id):
            return str(sender_id)
        try:
            return self._tree.get(sender_id).name or str(sender_id)
        except KeyError:
            return str(sender_id)

    def _deliver(self, recipient_id: UUID, envelope: MessageEnvelope) -> None:
        """Deliver envelope to recipient's inbox, creating inbox if needed."""
        inbox = self._inboxes.get(recipient_id)
        if inbox is None:
            inbox = Inbox()
            self._inboxes[recipient_id] = inbox
        inbox.deliver(envelope)
