# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tool catalog and logic layer.

AGENT_TOOLS is the catalog of Substrat's seven agent-facing tools.
ToolHandler implements agent tools as pure operations on the agent tree
and inboxes. Workspace validation is injected as a callable.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID, uuid4

from substrat.agent.inbox import Inbox
from substrat.agent.message import (
    MessageEnvelope,
    MessageKind,
)
from substrat.agent.node import AgentNode
from substrat.agent.router import RoutingError, resolve_broadcast, validate_route
from substrat.agent.tree import AgentTree
from substrat.model import ToolDef, ToolParam, sentinel_name

# -- Substrat agent tool catalog -----------------------------------------

AGENT_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        "send_message",
        "Send a message to a reachable agent (parent, child, or sibling).",
        (
            ToolParam("recipient", "string", "Agent name."),
            ToolParam("text", "string", "Message body."),
            ToolParam(
                "sync",
                "boolean",
                "Request synchronous reply delivery.",
                required=False,
                default=True,
            ),
        ),
    ),
    ToolDef(
        "broadcast",
        "Send a message to all siblings in the team.",
        (ToolParam("text", "string", "Message body."),),
    ),
    ToolDef(
        "check_inbox",
        "Retrieve pending async messages. Optional filters narrow results.",
        (
            ToolParam(
                "sender",
                "string",
                "Only return messages from this agent name.",
                required=False,
            ),
            ToolParam(
                "kind",
                "string",
                "Message kind filter (request/response/notification/error).",
                required=False,
            ),
        ),
    ),
    ToolDef(
        "spawn_agent",
        "Create a child agent. Returns immediately; session creation is deferred.",
        (
            ToolParam("name", "string", "Child agent name."),
            ToolParam("instructions", "string", "System prompt / task description."),
            ToolParam("workspace", "string", "Workspace name or spec.", required=False),
        ),
    ),
    ToolDef(
        "inspect_agent",
        "View a subordinate's state and recent activity.",
        (ToolParam("name", "string", "Child agent name."),),
    ),
    ToolDef(
        "complete",
        "Send result to parent and self-terminate. Leaf agents only.",
        (ToolParam("result", "string", "Final result to deliver."),),
    ),
    ToolDef(
        "poke",
        "Re-wake a child agent without sending a message. Retries a failed wake turn.",
        (ToolParam("agent_name", "string", "Name of a direct child."),),
    ),
)


class ToolError(Exception):
    """Raised when a tool call fails for a recoverable reason."""


DeferredWork = Callable[[], Coroutine[Any, Any, None]]
SpawnCallback = Callable[[AgentNode, tuple[UUID, str] | None], DeferredWork]
LogCallback = Callable[[UUID, str, dict[str, Any]], None]
WakeCallback = Callable[[UUID], None]
TerminateCallback = Callable[[UUID], DeferredWork]
ValidateWsRef = Callable[[str], tuple[UUID, str]]
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
        log_callback: LogCallback | None = None,
        wake_callback: WakeCallback | None = None,
        terminate_callback: TerminateCallback | None = None,
        validate_ws_ref: ValidateWsRef | None = None,
    ) -> None:
        self._tree = tree
        self._inboxes = inboxes
        self._caller_id = caller_id
        self._spawn_callback = spawn_callback
        self._log_callback = log_callback
        self._wake_callback = wake_callback
        self._terminate_callback = terminate_callback
        self._validate_ws_ref = validate_ws_ref
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
        """Send a message to all siblings in the team."""
        try:
            sibling_ids = resolve_broadcast(self._tree, self._caller_id)
        except RoutingError as exc:
            return {"error": str(exc)}
        broadcast_id = uuid4()
        for sid in sibling_ids:
            envelope = MessageEnvelope(
                sender=self._caller_id,
                recipient=sid,
                kind=MessageKind.REQUEST,
                payload=text,
                metadata={"broadcast_id": str(broadcast_id)},
            )
            self._deliver(sid, envelope)
        return {
            "status": "sent",
            "message_id": str(broadcast_id),
            "recipient_count": len(sibling_ids),
        }

    def check_inbox(
        self,
        *,
        sender: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Drain the caller's inbox and return messages.

        Optional filters narrow which messages are collected; unmatched
        messages stay in the inbox for later.
        """
        inbox = self._inboxes.get(self._caller_id)
        if inbox is None:
            return {"messages": []}
        # Resolve optional filters.
        sender_id: UUID | None = None
        if sender is not None:
            try:
                sender_id = self._resolve_name(sender).id
            except ToolError as exc:
                return {"error": str(exc)}
        kind_enum: MessageKind | None = None
        if kind is not None:
            try:
                kind_enum = MessageKind(kind)
            except ValueError:
                return {"error": f"unknown message kind: {kind!r}"}
        messages = inbox.collect(sender=sender_id, kind=kind_enum)
        for m in messages:
            self._log_event(
                self._caller_id,
                "message.delivered",
                {"message_id": m.id.hex},
            )
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
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Create a child agent. Actual session creation is deferred."""
        # Validate workspace ref before mutating the tree.
        ws_key: tuple[UUID, str] | None = None
        if workspace is not None:
            if self._validate_ws_ref is None:
                return {"error": "workspace tools not available"}
            try:
                ws_key = self._validate_ws_ref(workspace)
            except (ValueError, KeyError) as exc:
                return {"error": str(exc)}

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
            self._deferred.append(self._spawn_callback(child, ws_key))
        result: dict[str, Any] = {
            "status": "accepted",
            "agent_id": str(child.id),
            "name": child.name,
        }
        if ws_key is not None:
            result["workspace"] = workspace
        return result

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

    def complete(self, result: str) -> dict[str, Any]:
        """Send RESPONSE to parent and defer self-termination.

        Only valid for leaf agents (no children) with a parent.
        """
        node = self._tree.get(self._caller_id)
        if node.parent_id is None:
            return {"error": "root agent cannot complete — no parent"}
        if node.children:
            return {"error": "agent has children; terminate them first"}
        # Send RESPONSE to parent.
        envelope = MessageEnvelope(
            sender=self._caller_id,
            recipient=node.parent_id,
            kind=MessageKind.RESPONSE,
            payload=result,
        )
        self._deliver(node.parent_id, envelope)
        # Defer self-termination.
        if self._terminate_callback is not None:
            self._deferred.append(self._terminate_callback(self._caller_id))
        return {
            "status": "completing",
            "message_id": str(envelope.id),
        }

    def poke(self, agent_name: str) -> dict[str, Any]:
        """Re-wake a child without sending a message.

        Enqueues a wake notification. If the child is IDLE with pending
        messages, the wake loop retries the turn. Otherwise silently skipped.
        """
        try:
            child = self._resolve_child_name(agent_name)
        except ToolError as exc:
            return {"error": str(exc)}
        if self._wake_callback is not None:
            self._wake_callback(child.id)
        return {"status": "poked", "agent_id": str(child.id)}

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
        """Human-readable sender name. Falls back to UUID string for unknowns."""
        name = sentinel_name(sender_id)
        if name is not None:
            return name
        try:
            return self._tree.get(sender_id).name or str(sender_id)
        except KeyError:
            return str(sender_id)

    def _log_event(
        self,
        agent_id: UUID,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """Fire log callback if configured. Silent otherwise."""
        if self._log_callback is not None:
            self._log_callback(agent_id, event, data)

    def _deliver(self, recipient_id: UUID, envelope: MessageEnvelope) -> None:
        """Deliver envelope to recipient's inbox, creating inbox if needed."""
        self._log_event(
            recipient_id,
            "message.enqueued",
            {
                "message_id": envelope.id.hex,
                "sender": envelope.sender.hex,
                "recipient": recipient_id.hex,
                "kind": envelope.kind.value,
                "payload": envelope.payload,
                "timestamp": envelope.timestamp,
                "reply_to": envelope.reply_to.hex if envelope.reply_to else None,
                "metadata": envelope.metadata,
            },
        )
        inbox = self._inboxes.get(recipient_id)
        if inbox is None:
            inbox = Inbox()
            self._inboxes[recipient_id] = inbox
        inbox.deliver(envelope)
        if self._wake_callback is not None:
            self._wake_callback(recipient_id)
