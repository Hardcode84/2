# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tool catalog and logic layer.

AGENT_TOOLS is the catalog of Substrat's five agent-facing tools.
WORKSPACE_TOOLS adds the five workspace management tools.
ALL_TOOLS is the union for daemon/MCP consumption.

ToolHandler implements them as pure operations on the agent tree,
inboxes, and (optionally) workspace store/mapping — no wire protocol,
no I/O, no daemon.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from substrat.agent.inbox import Inbox
from substrat.agent.message import (
    MessageEnvelope,
    MessageKind,
    sentinel_name,
)
from substrat.agent.node import AgentNode
from substrat.agent.router import RoutingError, resolve_broadcast, validate_route
from substrat.agent.tree import AgentTree
from substrat.model import ToolDef, ToolParam

if TYPE_CHECKING:
    from substrat.workspace.mapping import WorkspaceMapping
    from substrat.workspace.store import WorkspaceStore

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
        "Multicast a message to all siblings in the team.",
        (ToolParam("text", "string", "Message body."),),
    ),
    ToolDef(
        "check_inbox",
        "Retrieve pending async messages.",
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
)


WORKSPACE_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        "list_workspaces",
        "List visible workspaces (own, children's, parent's scopes).",
    ),
    ToolDef(
        "create_workspace",
        "Create a workspace in the calling agent's scope.",
        (
            ToolParam("name", "string", "Workspace name."),
            ToolParam(
                "network_access",
                "boolean",
                "Allow network access inside the sandbox.",
                required=False,
                default=False,
            ),
            ToolParam(
                "view_of",
                "string",
                "Source workspace ref for live view.",
                required=False,
            ),
            ToolParam(
                "subdir",
                "string",
                "Subfolder within source (view_of only).",
                required=False,
                default=".",
            ),
            ToolParam(
                "mode",
                "string",
                "View mode: ro or rw (view_of only).",
                required=False,
                default="ro",
            ),
        ),
    ),
    ToolDef(
        "delete_workspace",
        "Delete a workspace. Must be in a mutable scope.",
        (ToolParam("name", "string", "Workspace ref (scoped)."),),
    ),
    ToolDef(
        "link_dir",
        "Link a directory into a workspace.",
        (
            ToolParam("workspace", "string", "Target workspace ref (scoped)."),
            ToolParam("source", "string", "Path inside caller's own workspace."),
            ToolParam("target", "string", "Mount path inside target workspace."),
            ToolParam(
                "mode",
                "string",
                "Bind mode: ro or rw.",
                required=False,
                default="ro",
            ),
        ),
    ),
    ToolDef(
        "unlink_dir",
        "Remove a linked directory from a workspace.",
        (
            ToolParam("workspace", "string", "Workspace ref (scoped)."),
            ToolParam("target", "string", "Mount path to remove."),
        ),
    ),
)

ALL_TOOLS: tuple[ToolDef, ...] = AGENT_TOOLS + WORKSPACE_TOOLS


class ToolError(Exception):
    """Raised when a tool call fails for a recoverable reason."""


DeferredWork = Callable[[], Coroutine[Any, Any, None]]
SpawnCallback = Callable[[AgentNode], DeferredWork]
LogCallback = Callable[[UUID, str, dict[str, Any]], None]
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
        ws_store: WorkspaceStore | None = None,
        ws_mapping: WorkspaceMapping | None = None,
    ) -> None:
        self._tree = tree
        self._inboxes = inboxes
        self._caller_id = caller_id
        self._spawn_callback = spawn_callback
        self._log_callback = log_callback
        self._ws_store = ws_store
        self._ws_mapping = ws_mapping
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

    def list_workspaces(self) -> dict[str, Any]:
        """List workspaces visible to the caller."""
        from substrat.workspace.resolve import (
            mutable_scopes,
            visible_scopes,
        )

        try:
            store, _mapping = self._require_ws_deps()
        except ToolError as exc:
            return {"error": str(exc)}
        caller = self._tree.get(self._caller_id)
        vis = visible_scopes(caller, self._tree)
        mut = mutable_scopes(caller, self._tree)
        workspaces = [
            {
                "name": ws.name,
                "scope": self._scope_label(ws.scope, caller),
                "mutable": ws.scope in mut,
            }
            for ws in store.scan()
            if ws.scope in vis
        ]
        return {"workspaces": workspaces}

    def create_workspace(
        self,
        name: str,
        *,
        network_access: bool = False,
        view_of: str | None = None,
        subdir: str = ".",
        mode: Literal["ro", "rw"] = "ro",
    ) -> dict[str, Any]:
        """Create a workspace in the caller's own scope."""
        from pathlib import Path

        from substrat.workspace.model import LinkSpec, Workspace
        from substrat.workspace.resolve import resolve, visible_scopes
        from substrat.workspace.store import validate_name

        try:
            store, _mapping = self._require_ws_deps()
        except ToolError as exc:
            return {"error": str(exc)}
        try:
            validate_name(name)
        except ValueError as exc:
            return {"error": str(exc)}
        caller = self._tree.get(self._caller_id)
        scope = caller.id
        if store.exists(scope, name):
            return {"error": f"workspace {name!r} already exists in own scope"}
        links: list[LinkSpec] = []
        if view_of is not None:
            try:
                src_scope, src_name = resolve(caller, view_of, self._tree)
            except (ValueError, KeyError) as exc:
                return {"error": str(exc)}
            vis = visible_scopes(caller, self._tree)
            if src_scope not in vis:
                return {"error": f"workspace {view_of!r} not visible"}
            try:
                src_ws = store.load(src_scope, src_name)
            except FileNotFoundError:
                return {"error": f"workspace {view_of!r} not found"}
            host_path = src_ws.root_path / subdir
            links.append(LinkSpec(host_path=host_path, mount_path=Path("."), mode=mode))
        ws_dir = store.workspace_dir(scope, name) / "root"
        ws = Workspace(
            name=name,
            scope=scope,
            root_path=ws_dir,
            network_access=network_access,
            links=links,
        )
        store.save(ws)
        return {"status": "created", "name": name}

    def delete_workspace(self, name: str) -> dict[str, Any]:
        """Delete a workspace. Must be in a mutable scope."""
        from substrat.workspace.resolve import mutable_scopes, resolve

        try:
            store, mapping = self._require_ws_deps()
        except ToolError as exc:
            return {"error": str(exc)}
        caller = self._tree.get(self._caller_id)
        try:
            scope, local_name = resolve(caller, name, self._tree)
        except (ValueError, KeyError) as exc:
            return {"error": str(exc)}
        mut = mutable_scopes(caller, self._tree)
        if scope not in mut:
            return {"error": f"workspace {name!r} is not in a mutable scope"}
        if not store.exists(scope, local_name):
            return {"error": f"workspace {name!r} not found"}
        agents = mapping.agents_in(scope, local_name)
        if agents:
            return {"error": f"workspace {name!r} has {len(agents)} assigned agent(s)"}
        store.delete(scope, local_name)
        return {"status": "deleted"}

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

    def _require_ws_deps(self) -> tuple[WorkspaceStore, WorkspaceMapping]:
        """Return workspace deps or raise ToolError if not configured."""
        if self._ws_store is None or self._ws_mapping is None:
            raise ToolError("workspace tools not available")
        return self._ws_store, self._ws_mapping

    def _scope_label(self, scope: UUID, caller: AgentNode) -> str:
        """Map a scope UUID to a human-readable label for the caller."""
        if scope == caller.id:
            return "self"
        # Check children.
        for cid in caller.children:
            child = self._tree.get(cid)
            if child.id == scope:
                return child.name
        # Must be parent scope.
        return "parent"
