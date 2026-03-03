# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator — composition root bridging agent and session layers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from substrat.agent.inbox import Inbox
from substrat.agent.message import MessageEnvelope, MessageKind, sentinel_name
from substrat.agent.node import AgentNode, AgentState, AgentStateError
from substrat.agent.prompt import build_prompt
from substrat.agent.tools import (
    DeferredWork,
    InboxRegistry,
    LogCallback,
    SpawnCallback,
    TerminateCallback,
    ToolHandler,
)
from substrat.agent.tree import AgentTree
from substrat.logging.event_log import read_log
from substrat.model import CommandWrapper
from substrat.scheduler import TurnScheduler
from substrat.session.model import Session, SessionState
from substrat.workspace.handler import WorkspaceToolHandler
from substrat.workspace.mapping import WorkspaceMapping
from substrat.workspace.store import WorkspaceStore

# Factory that builds a CommandWrapper from a workspace key (scope, name).
# The returned closure re-reads workspace state on each invocation.
WrapCommandFactory = Callable[[UUID, str], CommandWrapper]

_log = logging.getLogger(__name__)


class Orchestrator:
    """Wires agent lifecycle to session management.

    Owns the agent tree, inbox registry, and per-agent tool handlers.
    The TurnScheduler is injected — it owns sessions and the multiplexer.
    """

    def __init__(
        self,
        scheduler: TurnScheduler,
        *,
        default_provider: str,
        default_model: str,
        ws_store: WorkspaceStore | None = None,
        ws_mapping: WorkspaceMapping | None = None,
        wrap_command_factory: WrapCommandFactory | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._default_provider = default_provider
        self._default_model = default_model
        self._tree = AgentTree()
        self._inboxes: InboxRegistry = {}
        self._handlers: dict[UUID, ToolHandler] = {}
        self._ws_handlers: dict[UUID, WorkspaceToolHandler] = {}
        self._ws_store = ws_store
        self._ws_mapping = ws_mapping
        self._wrap_factory = wrap_command_factory
        self._wake_queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._wake_task: asyncio.Task[None] | None = None

    @property
    def tree(self) -> AgentTree:
        return self._tree

    @property
    def inboxes(self) -> InboxRegistry:
        return self._inboxes

    # -- Public API -----------------------------------------------------------

    async def create_root_agent(
        self,
        name: str,
        instructions: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        workspace: tuple[UUID, str] | None = None,
    ) -> AgentNode:
        """Create a root agent with a backing session.

        Creates the session first, then registers the node. If tree insertion
        fails (name collision), the session is terminated to avoid orphans.
        """
        prov = provider or self._default_provider
        mdl = model or self._default_model

        # Resolve workspace → wrap_command + path.
        ws_path, wrap_cmd = self._resolve_workspace(workspace)

        node = AgentNode(
            session_id=uuid4(),
            name=name,
            instructions=instructions,
        )
        prompt = build_prompt(instructions)
        session = await self._scheduler.create_session(
            prov,
            mdl,
            prompt,
            workspace=ws_path,
            wrap_command=wrap_cmd,
            agent_id=node.id,
        )
        node.session_id = session.id

        try:
            self._tree.add(node)
        except ValueError:
            await self._scheduler.terminate_session(session.id)
            raise

        if workspace is not None and self._ws_mapping is not None:
            self._ws_mapping.assign(node.id, workspace[0], workspace[1])

        ws_data = [workspace[0].hex, workspace[1]] if workspace else None
        self._log_lifecycle(
            session.id,
            "agent.created",
            {
                "agent_id": node.id.hex,
                "name": node.name,
                "parent_session_id": None,
                "instructions": node.instructions,
                "workspace": ws_data,
            },
        )

        self._inboxes[node.id] = Inbox()
        self._handlers[node.id] = self._make_handler(node.id, prov, mdl)
        return node

    async def run_turn(self, agent_id: UUID, prompt: str) -> str:
        """Send a turn to the agent's backing session.

        Manages IDLE → BUSY → IDLE transitions and drains deferred spawn
        work after the turn completes.
        """
        node = self._tree.get(agent_id)
        node.begin_turn()
        return await self._execute_turn(node, prompt)

    async def terminate_agent(self, agent_id: UUID) -> None:
        """Terminate a leaf agent and clean up all associated state."""
        node = self._tree.get(agent_id)
        if node.children:
            raise ValueError(f"agent {agent_id} has children; terminate them first")
        node.terminate()
        self._log_lifecycle(
            node.session_id,
            "agent.terminated",
            {
                "agent_id": node.id.hex,
            },
        )
        await self._scheduler.terminate_session(node.session_id)
        self._tree.remove(agent_id)
        self._handlers.pop(agent_id, None)
        self._ws_handlers.pop(agent_id, None)
        self._inboxes.pop(agent_id, None)

    def get_handler(self, agent_id: UUID) -> ToolHandler:
        """Return the tool handler for an agent. KeyError if unknown."""
        return self._handlers[agent_id]

    def get_ws_handler(self, agent_id: UUID) -> WorkspaceToolHandler | None:
        """Return the workspace tool handler for an agent, or None."""
        return self._ws_handlers.get(agent_id)

    # -- Wake loop ------------------------------------------------------------

    _WAKE_LIMIT = 100  # Max wakes per drain cycle.

    def start_wake_loop(self) -> None:
        """Start the background wake-processing task."""
        if self._wake_task is not None:
            return
        self._wake_task = asyncio.get_event_loop().create_task(self._wake_loop())

    async def stop_wake_loop(self) -> None:
        """Cancel the background wake task and wait for cleanup."""
        if self._wake_task is None:
            return
        self._wake_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._wake_task
        self._wake_task = None

    def _notify_wake(self, agent_id: UUID) -> None:
        """Enqueue a wake notification. Called synchronously from _deliver."""
        self._wake_queue.put_nowait(agent_id)

    async def _wake_loop(self) -> None:
        """Background consumer: drain queue, process wakes."""
        while True:
            agent_id = await self._wake_queue.get()
            # Batch-drain up to limit.
            batch: list[UUID] = [agent_id]
            while len(batch) < self._WAKE_LIMIT:
                try:
                    batch.append(self._wake_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if len(batch) >= self._WAKE_LIMIT:
                _log.warning(
                    "wake limit hit (%d), possible ping-pong",
                    self._WAKE_LIMIT,
                )
            # Deduplicate — only the first occurrence per agent matters.
            seen: set[UUID] = set()
            for aid in batch:
                if aid not in seen:
                    seen.add(aid)
                    await self._process_wake(aid)

    async def _process_wake(self, agent_id: UUID) -> None:
        """Wake a single IDLE agent that has pending messages."""
        if agent_id not in self._tree:
            return
        node = self._tree.get(agent_id)
        if node.state != AgentState.IDLE:
            return
        inbox = self._inboxes.get(agent_id)
        if not inbox:
            return
        if agent_id not in self._handlers:
            return  # Session not ready yet (pending spawn).
        try:
            node.begin_turn()
        except AgentStateError:
            return
        prompt = self._format_wake_prompt(agent_id)
        if not prompt:
            # Inbox drained by a concurrent check_inbox.
            node.end_turn()
            return
        await self._execute_turn(node, prompt)

    def _format_wake_prompt(self, agent_id: UUID) -> str:
        """Drain inbox and format as a prompt string. Empty if inbox empty."""
        inbox = self._inboxes.get(agent_id)
        if not inbox:
            return ""
        messages = inbox.collect()
        if not messages:
            return ""
        for m in messages:
            self._log_lifecycle_for_agent(
                agent_id,
                "message.delivered",
                {"message_id": m.id.hex},
            )
        if len(messages) == 1:
            m = messages[0]
            name = self._sender_display_name(m.sender)
            return f"Message from {name}:\n{m.payload}"
        lines: list[str] = []
        for i, m in enumerate(messages, 1):
            name = self._sender_display_name(m.sender)
            lines.append(f"{i}. From {name}: {m.payload}")
        return "\n".join(lines)

    def _sender_display_name(self, sender_id: UUID) -> str:
        """Human-readable sender name."""
        name = sentinel_name(sender_id)
        if name is not None:
            return name
        try:
            return self._tree.get(sender_id).name or str(sender_id)
        except KeyError:
            return str(sender_id)

    def _log_lifecycle_for_agent(
        self,
        agent_id: UUID,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """Log lifecycle event using agent_id to resolve session."""
        try:
            node = self._tree.get(agent_id)
        except KeyError:
            return
        self._log_lifecycle(node.session_id, event, data)

    # -- Private --------------------------------------------------------------

    async def _execute_turn(self, node: AgentNode, prompt: str) -> str:
        """Execute a turn on an already-BUSY node.

        Shared by run_turn (external RPC) and _process_wake (internal).
        Handles error recovery and deferred drain.
        """
        try:
            response = await self._scheduler.send_turn(node.session_id, prompt)
        except Exception:
            if node.state == AgentState.BUSY:
                node.end_turn()
            raise
        node.end_turn()
        await self._drain_deferred(node.id)
        # Re-wake if messages arrived during the turn.
        self._rewake_if_pending(node.id)
        return response

    def _rewake_if_pending(self, agent_id: UUID) -> None:
        """Re-enqueue wake if agent is IDLE with a non-empty inbox.

        Catches messages delivered mid-turn whose wake was skipped
        because the agent was BUSY at notification time.
        """
        if agent_id not in self._tree:
            return
        node = self._tree.get(agent_id)
        if node.state != AgentState.IDLE:
            return
        inbox = self._inboxes.get(agent_id)
        if inbox:
            self._notify_wake(agent_id)

    def _resolve_workspace(
        self,
        ws_key: tuple[UUID, str] | None,
    ) -> tuple[Path | None, CommandWrapper | None]:
        """Look up workspace and build wrap_command. Returns (path, wrapper)."""
        if ws_key is None:
            return None, None
        if self._ws_store is None:
            raise ValueError("workspace store not configured")
        scope, ws_name = ws_key
        ws = self._ws_store.load(scope, ws_name)
        wrap_cmd = None
        if self._wrap_factory is not None:
            wrap_cmd = self._wrap_factory(scope, ws_name)
        return ws.root_path, wrap_cmd

    def _make_handler(
        self,
        agent_id: UUID,
        provider: str,
        model: str,
    ) -> ToolHandler:
        """Build a ToolHandler with spawn, log, wake, and terminate callbacks."""
        validate_ws_ref = None
        if self._ws_store is not None and self._ws_mapping is not None:
            ws_handler = WorkspaceToolHandler(
                store=self._ws_store,
                mapping=self._ws_mapping,
                caller_id=agent_id,
                resolve_ctx=self._make_resolve_ctx(agent_id),
                scope_namer=self._make_scope_namer(agent_id),
            )
            self._ws_handlers[agent_id] = ws_handler
            validate_ws_ref = ws_handler.validate_ref
        return ToolHandler(
            self._tree,
            self._inboxes,
            agent_id,
            spawn_callback=self._make_spawn_callback(provider, model),
            log_callback=self._make_log_callback(),
            wake_callback=self._notify_wake,
            terminate_callback=self._make_terminate_callback(),
            validate_ws_ref=validate_ws_ref,
        )

    def _make_resolve_ctx(
        self, agent_id: UUID
    ) -> Callable[[], tuple[UUID | None, list[UUID], Callable[[str], UUID]]]:
        """Return a closure that reads tree state fresh each call."""

        def ctx() -> tuple[UUID | None, list[UUID], Callable[[str], UUID]]:
            parent = self._tree.parent(agent_id)
            parent_id = parent.id if parent else None
            caller = self._tree.get(agent_id)

            def child_lookup(name: str) -> UUID:
                return self._tree.child_by_name(agent_id, name).id

            return parent_id, caller.children, child_lookup

        return ctx

    def _make_scope_namer(self, agent_id: UUID) -> Callable[[UUID], str]:
        """Return a closure that maps scope UUIDs to display labels."""

        def namer(scope: UUID) -> str:
            if scope == agent_id:
                return "self"
            caller = self._tree.get(agent_id)
            for cid in caller.children:
                child = self._tree.get(cid)
                if child.id == scope:
                    return child.name
            return "parent"

        return namer

    def _make_log_callback(self) -> LogCallback:
        """Return a closure that logs message events to the recipient's session log."""

        def callback(agent_id: UUID, event: str, data: dict[str, Any]) -> None:
            try:
                node = self._tree.get(agent_id)
            except KeyError:
                return
            self._log_lifecycle(node.session_id, event, data)

        return callback

    def _make_terminate_callback(self) -> TerminateCallback:
        """Return a callback that defers agent termination."""

        def callback(agent_id: UUID) -> DeferredWork:
            async def do_terminate() -> None:
                await self.terminate_agent(agent_id)

            return do_terminate

        return callback

    def _make_spawn_callback(
        self,
        provider: str,
        model: str,
    ) -> SpawnCallback:
        """Return a callback that defers child session creation.

        The child inherits the parent's provider and model. Its placeholder
        session_id is patched to match the real session after creation.
        """

        def callback(child: AgentNode, ws_key: tuple[UUID, str] | None) -> DeferredWork:
            async def do_spawn() -> None:
                # Assign workspace mapping if requested.
                if ws_key is not None and self._ws_mapping is not None:
                    self._ws_mapping.assign(child.id, ws_key[0], ws_key[1])
                ws_path, wrap_cmd = self._resolve_workspace(ws_key)
                prompt = build_prompt(child.instructions)
                session = await self._scheduler.create_session(
                    provider,
                    model,
                    prompt,
                    workspace=ws_path,
                    wrap_command=wrap_cmd,
                    agent_id=child.id,
                )
                child.session_id = session.id
                # Log after session created so the event goes to the real log.
                parent_node = self._tree.parent(child.id)
                parent_sid = parent_node.session_id.hex if parent_node else None
                ws_data = [ws_key[0].hex, ws_key[1]] if ws_key else None
                self._log_lifecycle(
                    session.id,
                    "agent.created",
                    {
                        "agent_id": child.id.hex,
                        "name": child.name,
                        "parent_session_id": parent_sid,
                        "instructions": child.instructions,
                        "workspace": ws_data,
                    },
                )
                self._handlers[child.id] = self._make_handler(
                    child.id,
                    provider,
                    model,
                )

            return do_spawn

        return callback

    def _log_lifecycle(
        self,
        session_id: UUID,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """Best-effort lifecycle event logging. Silent if no log configured."""
        with contextlib.suppress(KeyError):
            self._scheduler.log_event(session_id, event, data)

    async def recover(self) -> None:
        """Reconstruct agent tree from persisted session event logs.

        Called once on a fresh orchestrator at daemon startup.
        """
        store = self._scheduler.store
        sessions = store.recover()

        # Index: sid -> (agent_id, name, parent_session_id, instructions, session).
        index: dict[UUID, dict[str, Any]] = {}
        orphans: list[Session] = []
        session_by_id: dict[UUID, Session] = {}

        for session in sessions:
            session_by_id[session.id] = session
            if session.state == SessionState.TERMINATED:
                continue

            log_path = store.agent_dir(session.id) / "events.jsonl"
            entries = read_log(log_path)

            # Find agent.created and agent.terminated events.
            created_data: dict[str, Any] | None = None
            terminated = False
            for entry in entries:
                ev = entry.get("event")
                if ev == "agent.created":
                    created_data = entry.get("data", {})
                elif ev == "agent.terminated":
                    terminated = True

            if terminated:
                continue
            if created_data is None:
                orphans.append(session)
                continue

            index[session.id] = {
                "agent_id": UUID(created_data["agent_id"]),
                "name": created_data.get("name", ""),
                "parent_session_id": created_data.get("parent_session_id"),
                "instructions": created_data.get("instructions", ""),
                "workspace": created_data.get("workspace"),
                "session": session,
                "entries": entries,
            }

        # Clean up orphaned sessions.
        for s in orphans:
            _log.warning("orphan session %s — no agent.created event", s.id.hex)
            s.terminate()
            store.save(s)

        # Build session_id -> agent_id lookup for parent resolution.
        sid_to_aid: dict[str, UUID] = {}
        for sid, info in index.items():
            sid_to_aid[sid.hex] = info["agent_id"]

        # Drop agents whose parent doesn't resolve. Terminate their sessions.
        valid: dict[UUID, dict[str, Any]] = {}
        for sid, info in index.items():
            psid = info["parent_session_id"]
            if psid is not None and psid not in sid_to_aid:
                _log.warning(
                    "agent %s parent session %s not found — terminating",
                    info["agent_id"].hex,
                    psid,
                )
                info["session"].terminate()
                store.save(info["session"])
                continue
            valid[sid] = info

        # Topological insert: roots first, then children whose parent is placed.
        placed: set[UUID] = set()  # agent_ids already in tree.
        remaining = dict(valid)

        while remaining:
            progress = False
            for sid in list(remaining):
                info = remaining[sid]
                psid = info["parent_session_id"]
                if psid is None:
                    parent_agent_id = None
                else:
                    parent_agent_id = sid_to_aid.get(psid)
                    if parent_agent_id not in placed:
                        continue

                ws_raw = info.get("workspace")
                ws_tuple = (UUID(ws_raw[0]), ws_raw[1]) if ws_raw is not None else None
                node = AgentNode(
                    id=info["agent_id"],
                    session_id=info["session"].id,
                    name=info["name"],
                    parent_id=parent_agent_id,
                    instructions=info["instructions"],
                )
                self._tree.add(node)
                self._inboxes[node.id] = Inbox()
                prov = info["session"].provider_name or self._default_provider
                mdl = info["session"].model or self._default_model
                self._handlers[node.id] = self._make_handler(node.id, prov, mdl)
                # Rebuild workspace mapping and wrap_command.
                if ws_tuple is not None and self._ws_mapping is not None:
                    self._ws_mapping.assign(node.id, ws_tuple[0], ws_tuple[1])
                _, wrap_cmd = self._resolve_workspace(ws_tuple)
                self._scheduler.restore_session(info["session"], wrap_command=wrap_cmd)
                placed.add(node.id)
                del remaining[sid]
                progress = True

            if not progress:
                # Remaining agents form a cycle or have unresolvable parents.
                for _sid, info in remaining.items():
                    _log.warning(
                        "unplaceable agent %s — terminating",
                        info["agent_id"].hex,
                    )
                    info["session"].terminate()
                    store.save(info["session"])
                break

        # -- Message recovery: re-inject pending messages. --
        for _sid, info in valid.items():
            if info["agent_id"] not in placed:
                continue
            entries = info.get("entries", [])
            enqueued: dict[str, dict[str, Any]] = {}
            delivered: set[str] = set()
            for entry in entries:
                ev = entry.get("event")
                if ev == "message.enqueued":
                    data = entry.get("data", {})
                    mid = data.get("message_id", "")
                    if mid:
                        enqueued[mid] = data
                elif ev == "message.delivered":
                    data = entry.get("data", {})
                    mid = data.get("message_id", "")
                    if mid:
                        delivered.add(mid)

            pending = {k: v for k, v in enqueued.items() if k not in delivered}
            for mid, data in pending.items():
                recipient_id = info["agent_id"]
                kind_str = data.get("kind", MessageKind.REQUEST.value)
                try:
                    kind = MessageKind(kind_str)
                except ValueError:
                    kind = MessageKind.REQUEST
                reply_to_hex = data.get("reply_to")
                envelope = MessageEnvelope(
                    sender=UUID(data["sender"]),
                    id=UUID(mid),
                    timestamp=data.get("timestamp", ""),
                    recipient=UUID(data["recipient"]),
                    reply_to=UUID(reply_to_hex) if reply_to_hex else None,
                    kind=kind,
                    payload=data.get("payload", ""),
                    metadata=data.get("metadata", {}),
                )
                inbox = self._inboxes.get(recipient_id)
                if inbox is not None:
                    inbox.deliver(envelope)

        # -- Recovery wake: agents with pending messages get woken. --
        for nid in placed:
            node = self._tree.get(nid)
            inbox = self._inboxes.get(nid)
            if inbox and node.state == AgentState.IDLE:
                self._notify_wake(nid)

    async def _drain_deferred(self, agent_id: UUID) -> None:
        """Drain and execute deferred work from the agent's tool handler."""
        handler = self._handlers[agent_id]
        for work in handler.drain_deferred():
            await work()
        # Post-spawn wake: newly created children with non-empty inboxes.
        if agent_id not in self._tree:
            return
        for child in self._tree.children(agent_id):
            inbox = self._inboxes.get(child.id)
            if inbox and child.state == AgentState.IDLE:
                self._notify_wake(child.id)
