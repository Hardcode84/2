# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Stateful fuzzer for the orchestrator.

Exercises agent lifecycle operations (create, spawn, turn, terminate) in
random sequences and checks structural invariants after every step.
Gated behind --run-stress.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import settings
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from substrat.agent import AgentState
from substrat.logging import EventLog
from substrat.orchestrator import Orchestrator
from substrat.scheduler import TurnScheduler
from substrat.session import SessionStore
from substrat.session.multiplexer import SessionMultiplexer

pytestmark = pytest.mark.stress


# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Minimal provider session for fuzzing."""

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        yield "ok"

    async def suspend(self) -> bytes:
        return b"s"

    async def stop(self) -> None:
        pass


class FakeProvider:
    """Provider that always succeeds."""

    @property
    def name(self) -> str:
        return "fake"

    async def create(
        self,
        model: str,
        system_prompt: str,
        log: EventLog | None = None,
    ) -> FakeProviderSession:
        return FakeProviderSession()

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
    ) -> FakeProviderSession:
        return FakeProviderSession()


# -- Helpers ---------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine in the current event loop or create one."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # Hypothesis runs synchronously; nest if needed.
        import nest_asyncio  # type: ignore[import-untyped]

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    return asyncio.run(coro)


# -- State machine ---------------------------------------------------------


class OrchestratorStateMachine(RuleBasedStateMachine):
    """Fuzz the orchestrator with random lifecycle operations.

    Shadow state tracks which agents exist, their parent-child relationships,
    and whether they have pending deferred work. Invariants run after every
    step.
    """

    # Bundles hold agent IDs created during the test.
    agents = Bundle("agents")

    def __init__(self) -> None:
        super().__init__()
        self._tmp = Path(f"/tmp/substrat-fuzz-{id(self)}")
        self._tmp.mkdir(parents=True, exist_ok=True)
        store = SessionStore(self._tmp / "sessions")
        mux = SessionMultiplexer(store, max_slots=3)
        provider = FakeProvider()
        scheduler = TurnScheduler(
            providers={"fake": provider},
            mux=mux,
            store=store,
        )
        self.orch = Orchestrator(
            scheduler,
            default_provider="fake",
            default_model="m",
        )
        # Shadow state: set of living agent IDs.
        self.alive: set[UUID] = set()
        # Children spawned but not yet drained (no handler/session yet).
        self.pending_children: set[UUID] = set()
        # Parents that need a run_turn to drain deferred work.
        self.parents_needing_drain: set[UUID] = set()
        # Parent → children mapping (shadow of tree).
        self.children: dict[UUID, set[UUID]] = {}

    def teardown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- Rules -------------------------------------------------------------

    # Small name alphabet to force collisions.
    _NAMES = ["a", "b", "c", "d", "e"]

    @initialize(target=agents)
    def seed_agent(self) -> UUID:
        """Create the first root agent so other rules have something to work with."""
        node = _run(self.orch.create_root_agent("seed", "init"))
        self.alive.add(node.id)
        self.children[node.id] = set()
        return node.id

    @rule(target=agents)
    def create_root(self) -> UUID:
        """Try to create a root agent. May collide on name."""
        import random

        name = random.choice(self._NAMES)
        try:
            node = _run(self.orch.create_root_agent(name, "inst"))
        except ValueError:
            # Name collision — expected.
            return UUID(int=0)  # Dummy, won't match any alive agent.
        self.alive.add(node.id)
        self.children[node.id] = set()
        return node.id

    @precondition(lambda self: bool(self.alive))
    @rule(target=agents, agent=agents)
    def spawn_child(self, agent: UUID) -> UUID:
        """Spawn a child on a living agent via its tool handler."""
        if agent not in self.alive or agent in self.pending_children:
            return UUID(int=0)
        import random

        name = random.choice(self._NAMES)
        handler = self.orch.get_handler(agent)
        result = handler.spawn_agent(name, "child-inst")
        if "error" in result:
            # Name collision among siblings — expected.
            return UUID(int=0)
        child_id = UUID(result["agent_id"])
        self.alive.add(child_id)
        self.pending_children.add(child_id)
        self.parents_needing_drain.add(agent)
        self.children[agent].add(child_id)
        self.children[child_id] = set()
        return child_id

    @precondition(lambda self: bool(self.alive))
    @rule(agent=agents)
    def run_turn(self, agent: UUID) -> None:
        """Run a turn on a living idle agent."""
        if agent not in self.alive or agent in self.pending_children:
            return
        node = self.orch.tree.get(agent)
        if node.state != AgentState.IDLE:
            return
        _run(self.orch.run_turn(agent, "go"))
        # Deferred drained — children now have real sessions and handlers.
        if agent in self.parents_needing_drain:
            self.parents_needing_drain.discard(agent)
            for child_id in self.children[agent]:
                self.pending_children.discard(child_id)

    @precondition(lambda self: bool(self.alive))
    @rule(agent=agents)
    def terminate_leaf(self, agent: UUID) -> None:
        """Terminate a living leaf agent."""
        if agent not in self.alive:
            return
        if self.children[agent]:
            # Not a leaf — skip (don't test the error path every time).
            return
        node = self.orch.tree.get(agent)
        if node.state == AgentState.BUSY:
            return
        # Can't terminate if this child hasn't been drained yet (no session).
        if agent in self.pending_children:
            return
        _run(self.orch.terminate_agent(agent))
        self.alive.discard(agent)
        # Remove from parent's children set.
        for _parent_id, kids in self.children.items():
            kids.discard(agent)
        del self.children[agent]

    # -- Invariants --------------------------------------------------------

    @invariant()
    def registries_in_sync(self) -> None:
        """Tree, handler, and inbox registries contain the same agent IDs."""
        tree_ids = {n.id for n in self.orch.tree.roots()}
        for rid in list(tree_ids):
            tree_ids.update(n.id for n in self.orch.tree.subtree(rid))
        handler_ids = set(self.orch._handlers.keys())
        inbox_ids = set(self.orch.inboxes.keys())
        # Pending children are in the tree but don't have handlers yet.
        # All other tree nodes must have a handler.
        materialized = tree_ids - self.pending_children
        assert materialized == handler_ids, (
            f"tree-handler mismatch: tree(materialized)={materialized}, "
            f"handlers={handler_ids}"
        )
        # Inboxes are created eagerly (on spawn), so they match the full tree.
        assert tree_ids == inbox_ids, (
            f"tree-inbox mismatch: tree={tree_ids}, inboxes={inbox_ids}"
        )

    @invariant()
    def no_stuck_busy(self) -> None:
        """No living agent should be stuck in BUSY state."""
        for aid in self.alive:
            node = self.orch.tree.get(aid)
            assert node.state != AgentState.BUSY, f"agent {aid} stuck BUSY"

    @invariant()
    def shadow_matches_tree(self) -> None:
        """Shadow alive set matches the real tree."""
        tree_ids: set[UUID] = set()
        for root in self.orch.tree.roots():
            tree_ids.add(root.id)
            tree_ids.update(n.id for n in self.orch.tree.subtree(root.id))
        assert self.alive == tree_ids, f"shadow={self.alive}, tree={tree_ids}"

    @invariant()
    def parent_child_consistency(self) -> None:
        """Every agent in tree has consistent parent-child links."""
        for aid in self.alive:
            real_children = {c.id for c in self.orch.tree.children(aid)}
            shadow_children = self.children.get(aid, set())
            assert real_children == shadow_children, (
                f"agent {aid}: real children={real_children}, shadow={shadow_children}"
            )

    @invariant()
    def all_idle_or_terminated(self) -> None:
        """Living agents are IDLE. No other state should persist between steps."""
        for aid in self.alive:
            node = self.orch.tree.get(aid)
            assert node.state == AgentState.IDLE, (
                f"agent {aid} in state {node.state.value}, expected IDLE"
            )


# Hypothesis needs a concrete TestCase class.
TestOrchestratorFuzz = OrchestratorStateMachine.TestCase
TestOrchestratorFuzz.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    deadline=None,
)
