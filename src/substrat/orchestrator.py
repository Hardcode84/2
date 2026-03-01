# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator — composition root bridging agent and session layers."""

from __future__ import annotations

import contextlib
import logging
from typing import Any
from uuid import UUID

from substrat.agent.inbox import Inbox
from substrat.agent.node import AgentNode, AgentState
from substrat.agent.tools import (
    DeferredWork,
    InboxRegistry,
    SpawnCallback,
    ToolHandler,
)
from substrat.agent.tree import AgentTree
from substrat.logging.event_log import read_log
from substrat.scheduler import TurnScheduler
from substrat.session.model import Session, SessionState

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
    ) -> None:
        self._scheduler = scheduler
        self._default_provider = default_provider
        self._default_model = default_model
        self._tree = AgentTree()
        self._inboxes: InboxRegistry = {}
        self._handlers: dict[UUID, ToolHandler] = {}

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
    ) -> AgentNode:
        """Create a root agent with a backing session.

        Creates the session first, then registers the node. If tree insertion
        fails (name collision), the session is terminated to avoid orphans.
        """
        prov = provider or self._default_provider
        mdl = model or self._default_model

        session = await self._scheduler.create_session(prov, mdl, instructions)
        node = AgentNode(session_id=session.id, name=name, instructions=instructions)

        try:
            self._tree.add(node)
        except ValueError:
            await self._scheduler.terminate_session(session.id)
            raise

        self._log_lifecycle(
            session.id,
            "agent.created",
            {
                "agent_id": node.id.hex,
                "name": node.name,
                "parent_session_id": None,
                "instructions": node.instructions,
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
        node.activate()
        try:
            response = await self._scheduler.send_turn(node.session_id, prompt)
        except Exception:
            # Reset state if still BUSY (activate succeeded but send blew up).
            if node.state == AgentState.BUSY:
                node.finish()
            raise
        node.finish()
        await self._drain_deferred(agent_id)
        return response

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
        self._inboxes.pop(agent_id, None)

    def get_handler(self, agent_id: UUID) -> ToolHandler:
        """Return the tool handler for an agent. KeyError if unknown."""
        return self._handlers[agent_id]

    # -- Private --------------------------------------------------------------

    def _make_handler(
        self,
        agent_id: UUID,
        provider: str,
        model: str,
    ) -> ToolHandler:
        """Build a ToolHandler with a spawn callback that inherits provider/model."""
        return ToolHandler(
            self._tree,
            self._inboxes,
            agent_id,
            spawn_callback=self._make_spawn_callback(provider, model),
        )

    def _make_spawn_callback(
        self,
        provider: str,
        model: str,
    ) -> SpawnCallback:
        """Return a callback that defers child session creation.

        The child inherits the parent's provider and model. Its placeholder
        session_id is patched to match the real session after creation.
        """

        def callback(child: AgentNode) -> DeferredWork:
            async def do_spawn() -> None:
                session = await self._scheduler.create_session(
                    provider,
                    model,
                    child.instructions,
                )
                child.session_id = session.id
                # Log after session created so the event goes to the real log.
                parent_node = self._tree.parent(child.id)
                parent_sid = parent_node.session_id.hex if parent_node else None
                self._log_lifecycle(
                    session.id,
                    "agent.created",
                    {
                        "agent_id": child.id.hex,
                        "name": child.name,
                        "parent_session_id": parent_sid,
                        "instructions": child.instructions,
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
                "session": session,
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
                self._scheduler.restore_session(info["session"])
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

    async def _drain_deferred(self, agent_id: UUID) -> None:
        """Drain and execute deferred work from the agent's tool handler."""
        handler = self._handlers[agent_id]
        for work in handler.drain_deferred():
            await work()
