# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator — composition root bridging agent and session layers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from substrat.agent.inbox import Inbox
from substrat.agent.message import (
    SYSTEM,
    USER,
    MessageEnvelope,
    MessageKind,
    sentinel_name,
)
from substrat.agent.node import AgentNode, AgentState, AgentStateError
from substrat.agent.prompt import build_prompt
from substrat.agent.tools import (
    CancelReminderCallback,
    DeferredWork,
    InboxRegistry,
    LogCallback,
    RemindCallback,
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


@dataclass
class Subscription:
    """A state transition subscription."""

    subscriber_id: UUID
    target_id: UUID
    from_state: str = "*"  # State name or "*" for any.
    to_state: str = "*"
    once: bool = False
    id: UUID = field(default_factory=uuid4)

    def matches(self, from_s: AgentState, to_s: AgentState) -> bool:
        """Check if a transition matches this subscription."""
        if self.from_state != "*" and self.from_state != from_s.value:
            return False
        return self.to_state == "*" or self.to_state == to_s.value


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
        default_model: str | None = None,
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
        # Per-agent reminder tasks: agent_id -> {reminder_id -> Task}.
        self._reminders: dict[UUID, dict[UUID, asyncio.Task[None]]] = {}
        # Subscriptions: target_id -> list of subscriptions.
        self._subscriptions: dict[UUID, list[Subscription]] = {}
        # Reverse index: subscription_id -> (subscriber_id, target_id).
        self._sub_index: dict[UUID, tuple[UUID, UUID]] = {}
        # USER inbox — collects messages from root agents to the operator.
        self._inboxes[USER] = Inbox()

    @property
    def tree(self) -> AgentTree:
        return self._tree

    @property
    def inboxes(self) -> InboxRegistry:
        return self._inboxes

    @property
    def user_inbox(self) -> Inbox:
        """The USER inbox — messages from root agents to the operator."""
        return self._inboxes[USER]

    # -- Public API -----------------------------------------------------------

    async def create_root_agent(
        self,
        name: str,
        instructions: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        workspace: tuple[UUID, str] | None = None,
        metadata: dict[str, str] | None = None,
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
            metadata=dict(metadata) if metadata else {},
        )
        prompt = build_prompt(instructions)
        _log.info("system prompt for %s:\n%s", name, prompt)
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
                "metadata": node.metadata or None,
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

    async def stream_turn(
        self, agent_id: UUID, prompt: str
    ) -> AsyncGenerator[str, None]:
        """Stream a turn's response chunks. Mirrors run_turn lifecycle."""
        node = self._tree.get(agent_id)
        node.begin_turn()
        try:
            async for chunk in self._scheduler.stream_turn(node.session_id, prompt):
                yield chunk
        except Exception:
            if node.state == AgentState.BUSY:
                node.end_turn()
                self._fire_transition(
                    node.id,
                    AgentState.BUSY,
                    AgentState.IDLE,
                )
            await self._drain_deferred(node.id, wake_children=False)
            raise
        node.end_turn()
        self._fire_transition(node.id, AgentState.BUSY, AgentState.IDLE)
        await self._drain_deferred(node.id)
        self._rewake_if_pending(node.id)

    async def terminate_agent(self, agent_id: UUID) -> None:
        """Terminate a leaf agent and clean up all associated state."""
        node = self._tree.get(agent_id)
        if node.children:
            raise ValueError(f"agent {agent_id} has children; terminate them first")
        prev_state = node.state
        node.terminate()
        self._fire_transition(node.id, prev_state, AgentState.TERMINATED)
        self._log_lifecycle(
            node.session_id,
            "agent.terminated",
            {
                "agent_id": node.id.hex,
            },
        )
        await self._scheduler.terminate_session(node.session_id)
        self._cancel_all_reminders(agent_id)
        self._cleanup_subscriptions(agent_id)
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
        """Cancel the background wake task, all reminders, and wait for cleanup."""
        # Cancel all reminder tasks.
        for agent_reminders in self._reminders.values():
            for task in agent_reminders.values():
                task.cancel()
        self._reminders.clear()
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
        """Wake a single IDLE agent that has pending messages.

        Uses peek-then-drain: the prompt is built from a non-destructive
        peek. Messages are only drained after the turn succeeds. On
        failure the inbox is untouched and the agent stays IDLE — the
        branch is frozen until someone pokes or the parent intervenes.
        """
        if agent_id not in self._tree:
            _log.debug("wake skip: %s not in tree", agent_id.hex[:8])
            return
        node = self._tree.get(agent_id)
        if node.state != AgentState.IDLE:
            _log.debug("wake skip: %s state=%s", node.name, node.state.value)
            return
        inbox = self._inboxes.get(agent_id)
        if not inbox:
            _log.debug("wake skip: %s empty inbox", node.name)
            return
        if agent_id not in self._handlers:
            _log.debug("wake skip: %s no handler (pending spawn)", node.name)
            return  # Session not ready yet (pending spawn).
        # Gate check: gated agents don't wake unless permit_once is set.
        if node.gated and not node.permit_once:
            _log.debug("wake skip: %s gated", node.name)
            return
        try:
            node.begin_turn()
        except AgentStateError:
            # begin_turn failed — don't consume permit_once.
            return
        prompt = self._format_wake_prompt(agent_id)
        if not prompt:
            # Inbox drained by a concurrent check_inbox — don't waste permit.
            node.end_turn()
            self._fire_transition(
                agent_id,
                AgentState.BUSY,
                AgentState.IDLE,
            )
            return
        # Consume permit_once only after confirming a turn will actually run.
        if node.gated and node.permit_once:
            node.permit_once = False
        try:
            await self._execute_turn(node, prompt)
        except Exception as exc:
            _log.warning(
                "wake turn failed for agent %s — inbox preserved",
                agent_id.hex,
                exc_info=True,
            )
            self._notify_parent_error(node, exc)
            return
        # Turn succeeded — drain inbox and log delivery.
        self._drain_inbox(agent_id)

    def _notify_parent_error(self, node: AgentNode, exc: Exception) -> None:
        """Deliver ERROR message to parent on child wake-turn failure.

        Root agents have no parent — the error is only logged.
        """
        if node.parent_id is None:
            return
        # Summarize pending messages the child failed to process.
        inbox = self._inboxes.get(node.id)
        summaries: list[str] = []
        if inbox:
            for m in inbox.peek():
                name = self._sender_display_name(m.sender)
                summaries.append(f"- from {name}: {m.payload!r}")
        parts = [
            f"Child agent {node.name!r} crashed during wake turn.",
            f"Error: {type(exc).__name__}: {exc}",
        ]
        if summaries:
            parts.append("Pending messages (preserved in child inbox):")
            parts.extend(summaries)
        parts.append("Use poke(agent_name) to retry, or terminate the child.")
        payload = "\n".join(parts)
        envelope = MessageEnvelope(
            sender=SYSTEM,
            recipient=node.parent_id,
            kind=MessageKind.ERROR,
            payload=payload,
            metadata={"failed_agent": node.id.hex},
        )
        parent_inbox = self._inboxes.get(node.parent_id)
        if parent_inbox is None:
            return
        self._log_lifecycle_for_agent(
            node.parent_id,
            "message.enqueued",
            {
                "message_id": envelope.id.hex,
                "sender": SYSTEM.hex,
                "recipient": node.parent_id.hex,
                "kind": "error",
                "payload": payload,
            },
        )
        parent_inbox.deliver(envelope)
        self._notify_wake(node.parent_id)

    def _drain_inbox(self, agent_id: UUID) -> None:
        """Drain inbox and log message.delivered events after a successful wake turn."""
        inbox = self._inboxes.get(agent_id)
        if not inbox:
            return
        for m in inbox.collect():
            self._log_lifecycle_for_agent(
                agent_id,
                "message.delivered",
                {"message_id": m.id.hex},
            )

    def _format_wake_prompt(self, agent_id: UUID) -> str:
        """Build prompt from inbox without draining. Empty if inbox empty."""
        inbox = self._inboxes.get(agent_id)
        if not inbox:
            return ""
        messages = inbox.peek()
        if not messages:
            return ""
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

    def log_tool_call(
        self,
        agent_id: UUID,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Log a tool.call event to the caller's own session log.

        Records tool name, arguments, and result (or error) so the
        session's event log contains the full tool call history.
        Prerequisite for scripted provider crash recovery.
        """
        data: dict[str, Any] = {"tool": tool, "args": arguments}
        if "error" in result:
            data["error"] = result["error"]
        else:
            data["result"] = result
        self._log_lifecycle_for_agent(agent_id, "tool.call", data)

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
                self._fire_transition(
                    node.id,
                    AgentState.BUSY,
                    AgentState.IDLE,
                )
            await self._drain_deferred(node.id, wake_children=False)
            raise
        node.end_turn()
        self._fire_transition(node.id, AgentState.BUSY, AgentState.IDLE)
        await self._drain_deferred(node.id)
        # Re-wake if messages arrived during the turn.
        self._rewake_if_pending(node.id)
        return response

    def _enqueue_first_turn(self, node: AgentNode) -> None:
        """Schedule a first turn for a newly spawned agent with empty inbox.

        Runs as a background task — the agent gets a simple bootstrap prompt
        instead of formatted inbox messages.
        """

        async def _run() -> None:
            if node.state != AgentState.IDLE:
                return
            if node.id not in self._handlers:
                return
            if node.gated and not node.permit_once:
                return
            try:
                node.begin_turn()
            except AgentStateError:
                return
            # Consume permit_once after successful begin_turn.
            if node.gated and node.permit_once:
                node.permit_once = False
            try:
                await self._execute_turn(
                    node,
                    "You have been spawned. Your task is in your system prompt."
                    " Start working now — read any referenced files and execute"
                    " your instructions. Do not wait for further messages.",
                )
            except Exception:
                _log.warning(
                    "first turn failed for agent %s", node.id.hex, exc_info=True
                )

        if self._wake_task is not None:
            asyncio.get_event_loop().create_task(_run())

    # -- Subscriptions -------------------------------------------------------

    def _add_subscription(
        self,
        subscriber_id: UUID,
        target_id: UUID,
        from_state: str,
        to_state: str,
        once: bool,
    ) -> UUID:
        """Register a subscription. Returns the subscription ID."""
        sub = Subscription(
            subscriber_id=subscriber_id,
            target_id=target_id,
            from_state=from_state,
            to_state=to_state,
            once=once,
        )
        self._subscriptions.setdefault(target_id, []).append(sub)
        self._sub_index[sub.id] = (subscriber_id, target_id)
        return sub.id

    def _remove_subscription(self, subscription_id: UUID) -> bool:
        """Remove a subscription by ID. Returns True if found."""
        entry = self._sub_index.pop(subscription_id, None)
        if entry is None:
            return False
        _, target_id = entry
        subs = self._subscriptions.get(target_id)
        if subs is not None:
            self._subscriptions[target_id] = [
                s for s in subs if s.id != subscription_id
            ]
            if not self._subscriptions[target_id]:
                del self._subscriptions[target_id]
        return True

    def _fire_transition(
        self,
        agent_id: UUID,
        from_state: AgentState,
        to_state: AgentState,
    ) -> None:
        """Deliver notifications for matching subscriptions."""
        subs = self._subscriptions.get(agent_id)
        if not subs:
            return
        try:
            node = self._tree.get(agent_id)
        except KeyError:
            _log.debug("fire_transition skip: %s not in tree", agent_id.hex[:8])
            return
        name = node.name or agent_id.hex[:8]
        fired: list[UUID] = []
        for sub in subs:
            if not sub.matches(from_state, to_state):
                continue
            payload = f"[state] {name}: {from_state.value} -> {to_state.value}"
            envelope = MessageEnvelope(
                sender=SYSTEM,
                recipient=sub.subscriber_id,
                kind=MessageKind.NOTIFICATION,
                payload=payload,
                metadata={
                    "subscription_id": sub.id.hex,
                    "agent": name,
                    "from": from_state.value,
                    "to": to_state.value,
                },
            )
            inbox = self._inboxes.get(sub.subscriber_id)
            if inbox is not None:
                self._log_lifecycle_for_agent(
                    sub.subscriber_id,
                    "message.enqueued",
                    {
                        "message_id": envelope.id.hex,
                        "sender": SYSTEM.hex,
                        "recipient": sub.subscriber_id.hex,
                        "kind": "notification",
                        "payload": payload,
                    },
                )
                inbox.deliver(envelope)
                self._notify_wake(sub.subscriber_id)
            if sub.once:
                fired.append(sub.id)
        for sid in fired:
            self._remove_subscription(sid)

    def _cleanup_subscriptions(self, agent_id: UUID) -> None:
        """Remove all subscriptions involving an agent (as target or subscriber)."""
        # Remove as target.
        for sub in self._subscriptions.pop(agent_id, []):
            self._sub_index.pop(sub.id, None)
        # Remove as subscriber.
        to_remove = [
            sid
            for sid, (subscriber_id, _) in self._sub_index.items()
            if subscriber_id == agent_id
        ]
        for sid in to_remove:
            self._remove_subscription(sid)

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
        model: str | None,
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
            remind_callback=self._make_remind_callback(agent_id),
            cancel_reminder_callback=self._make_cancel_reminder_callback(agent_id),
            subscribe_callback=self._make_subscribe_callback(agent_id),
            unsubscribe_callback=self._make_unsubscribe_callback(agent_id),
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

    def _cancel_all_reminders(self, agent_id: UUID) -> None:
        """Cancel all pending reminders for an agent."""
        agent_reminders = self._reminders.pop(agent_id, {})
        for task in agent_reminders.values():
            task.cancel()

    def _make_remind_callback(self, agent_id: UUID) -> RemindCallback:
        """Return a callback that schedules a deferred reminder timer."""

        def callback(
            reason: str, timeout: float, every: float | None
        ) -> tuple[UUID, DeferredWork]:
            reminder_id = uuid4()

            async def do_remind() -> None:
                async def timer() -> None:
                    try:
                        await asyncio.sleep(timeout)
                        self._deliver_reminder(
                            agent_id,
                            reason,
                            reminder_id,
                            repeating=every is not None,
                        )
                        if every is not None:
                            while True:
                                await asyncio.sleep(every)
                                self._deliver_reminder(
                                    agent_id,
                                    reason,
                                    reminder_id,
                                    repeating=True,
                                )
                    except asyncio.CancelledError:
                        pass
                    finally:
                        # Clean up from registry.
                        agent_reminders = self._reminders.get(agent_id)
                        if agent_reminders is not None:
                            agent_reminders.pop(reminder_id, None)

                task = asyncio.get_event_loop().create_task(timer())
                self._reminders.setdefault(agent_id, {})[reminder_id] = task

            return reminder_id, do_remind

        return callback

    def _make_cancel_reminder_callback(self, agent_id: UUID) -> CancelReminderCallback:
        """Return a callback that cancels a reminder by ID."""

        def callback(reminder_id: UUID) -> bool:
            agent_reminders = self._reminders.get(agent_id)
            if agent_reminders is None:
                return False
            task = agent_reminders.pop(reminder_id, None)
            if task is None:
                return False
            task.cancel()
            return True

        return callback

    def _make_subscribe_callback(
        self, agent_id: UUID
    ) -> Callable[[UUID, str, str, bool], UUID]:
        """Return a callback that registers a subscription."""

        def callback(
            target_id: UUID,
            from_state: str,
            to_state: str,
            once: bool,
        ) -> UUID:
            return self._add_subscription(
                agent_id,
                target_id,
                from_state,
                to_state,
                once,
            )

        return callback

    def _make_unsubscribe_callback(self, agent_id: UUID) -> Callable[[UUID], bool]:
        """Return a callback that removes a subscription owned by agent."""

        def callback(subscription_id: UUID) -> bool:
            # Only allow removing own subscriptions.
            entry = self._sub_index.get(subscription_id)
            if entry is None or entry[0] != agent_id:
                return False
            return self._remove_subscription(subscription_id)

        return callback

    def _deliver_reminder(
        self,
        agent_id: UUID,
        reason: str,
        reminder_id: UUID,
        *,
        repeating: bool = False,
    ) -> None:
        """Deliver a reminder NOTIFICATION to the agent's inbox."""
        if agent_id not in self._tree:
            return
        payload = f"Reminder: {reason}"
        if repeating:
            payload += f'\nTo cancel: cancel_reminder("{reminder_id.hex}")'
        envelope = MessageEnvelope(
            sender=SYSTEM,
            recipient=agent_id,
            kind=MessageKind.NOTIFICATION,
            payload=payload,
            metadata={"reminder_id": reminder_id.hex},
        )
        inbox = self._inboxes.get(agent_id)
        if inbox is None:
            return
        self._log_lifecycle_for_agent(
            agent_id,
            "message.enqueued",
            {
                "message_id": envelope.id.hex,
                "sender": SYSTEM.hex,
                "recipient": agent_id.hex,
                "kind": "notification",
                "payload": envelope.payload,
            },
        )
        inbox.deliver(envelope)
        self._notify_wake(agent_id)

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
        model: str | None,
    ) -> SpawnCallback:
        """Return a callback that defers child session creation.

        The child inherits the parent's provider and model. Its placeholder
        session_id is patched to match the real session after creation.
        """

        def callback(child: AgentNode, ws_key: tuple[UUID, str] | None) -> DeferredWork:
            async def do_spawn() -> None:
                try:
                    # Assign workspace mapping if requested.
                    if ws_key is not None and self._ws_mapping is not None:
                        self._ws_mapping.assign(child.id, ws_key[0], ws_key[1])
                    ws_path, wrap_cmd = self._resolve_workspace(ws_key)
                    prompt = build_prompt(child.instructions)
                    _log.info("system prompt for %s:\n%s", child.name, prompt)
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
                            "metadata": child.metadata or None,
                        },
                    )
                    self._handlers[child.id] = self._make_handler(
                        child.id,
                        provider,
                        model,
                    )
                except Exception:
                    _log.exception(
                        "spawn failed for child %s — cleaning up orphan",
                        child.id.hex,
                    )
                    if child.id in self._tree:
                        self._tree.remove(child.id)
                    self._inboxes.pop(child.id, None)
                    if self._ws_mapping is not None and child.id in self._ws_mapping:
                        self._ws_mapping.unassign(child.id)

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
                "metadata": created_data.get("metadata"),
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
                # Restore spawn-time metadata, then replay updates.
                meta = dict(info["metadata"]) if info.get("metadata") else {}
                gated = False
                for entry in info.get("entries", []):
                    ev = entry.get("event")
                    if ev == "metadata.updated":
                        d = entry.get("data", {})
                        k = d.get("key")
                        if k is not None:
                            v = d.get("value")
                            if v is None:
                                meta.pop(k, None)
                            else:
                                meta[k] = v
                    elif ev == "tool.gate":
                        action = entry.get("data", {}).get("action")
                        gated = action == "gate"
                node = AgentNode(
                    id=info["agent_id"],
                    session_id=info["session"].id,
                    name=info["name"],
                    parent_id=parent_agent_id,
                    instructions=info["instructions"],
                    metadata=meta,
                    gated=gated,
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

        # -- Subscription recovery: replay subscribe/unsubscribe events. --
        for _sid, info in valid.items():
            subscriber_id = info["agent_id"]
            if subscriber_id not in placed:
                continue
            for entry in info.get("entries", []):
                ev = entry.get("event")
                if ev == "tool.subscribe":
                    d = entry.get("data", {})
                    # One-shot subscriptions are not restored — if they
                    # fired before crash we'd duplicate; if not, the
                    # pipeline re-subscribes after recovery.
                    if d.get("once", False):
                        continue
                    target_hex = d.get("target_id")
                    if not target_hex:
                        continue
                    try:
                        target_id = UUID(target_hex)
                    except ValueError:
                        continue
                    if target_id not in placed:
                        continue
                    sub_id = self._add_subscription(
                        subscriber_id,
                        target_id,
                        d.get("from", "*"),
                        d.get("to", "*"),
                        once=False,
                    )
                    # Patch the subscription ID to match the logged one.
                    logged_id = d.get("subscription_id")
                    if logged_id:
                        try:
                            real_id = UUID(logged_id)
                        except ValueError:
                            continue
                        old_id = sub_id
                        target_subs = self._subscriptions.get(
                            target_id,
                            [],
                        )
                        for sub in target_subs:
                            if sub.id == old_id:
                                sub.id = real_id
                                break
                        self._sub_index.pop(old_id, None)
                        self._sub_index[real_id] = (
                            subscriber_id,
                            target_id,
                        )
                elif ev == "tool.unsubscribe":
                    d = entry.get("data", {})
                    sid_hex = d.get("subscription_id")
                    if sid_hex:
                        try:
                            self._remove_subscription(UUID(sid_hex))
                        except ValueError:
                            continue

        # -- Recovery wake: agents with pending messages get woken. --
        for nid in placed:
            node = self._tree.get(nid)
            inbox = self._inboxes.get(nid)
            if inbox and node.state == AgentState.IDLE:
                self._notify_wake(nid)

    async def _drain_deferred(
        self, agent_id: UUID, *, wake_children: bool = True
    ) -> None:
        """Drain and execute deferred work from the agent's tool handler.

        When *wake_children* is True (default), children with non-empty
        inboxes are woken after the drain. Pass False on the error path
        to avoid infinite wake loops between a failing parent and child.
        """
        handler = self._handlers[agent_id]
        # Snapshot which children have handlers before drain. Children added
        # to the tree during tool calls won't have handlers yet — the deferred
        # work creates them. After drain, a child with a new handler is newly
        # spawned and needs its first-turn wake.
        pre_handlers = (
            {c.id for c in self._tree.children(agent_id) if c.id in self._handlers}
            if agent_id in self._tree
            else set()
        )
        for work in handler.drain_deferred():
            try:
                await work()
            except Exception:
                _log.exception("deferred work failed for agent %s", agent_id)
        if not wake_children:
            return
        # Post-spawn wake: new children get their first turn, existing
        # children with non-empty inboxes get re-woken.
        if agent_id not in self._tree:
            return
        for child in self._tree.children(agent_id):
            inbox = self._inboxes.get(child.id)
            has_handler = child.id in self._handlers
            is_new = child.id not in pre_handlers
            _log.debug(
                "post-drain check: child=%s state=%s inbox=%d handler=%s new=%s",
                child.name,
                child.state.value,
                len(inbox) if inbox else 0,
                has_handler,
                is_new,
            )
            if child.state == AgentState.IDLE and (is_new or inbox):
                _log.debug("post-drain wake: %s (new=%s)", child.name, is_new)
                if is_new and not inbox:
                    # First turn with no inbox — run directly with bootstrap
                    # prompt instead of going through the wake loop (which
                    # requires inbox messages).
                    self._enqueue_first_turn(child)
                else:
                    self._notify_wake(child.id)
