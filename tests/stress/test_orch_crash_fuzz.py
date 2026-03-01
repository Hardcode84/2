# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator crash-recovery fuzzer.

Exercises agent lifecycle operations (create, spawn, turn, terminate,
send, check_inbox) with crash injection at arbitrary IO boundaries via
VirtualFS. After each crash: thaw, build a fresh orchestrator, run
``recover()``, and verify the recovered state is consistent with what
was committed to disk.

The IO-level crash fuzzer (``test_crash_fuzz.py``) proves that
``atomic_write`` and ``EventLog`` survive crashes individually. This
fuzzer proves the full recovery path: crash the orchestrator mid-operation,
reconstruct from disk, verify everything lines up.

Gated behind ``--run-stress``.

Do NOT use ``random`` in rules. All randomness must go through Hypothesis
strategies so that shrinking, replay, and ``derandomize=True`` work.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from substrat.agent import AgentState
from substrat.logging.event_log import read_log
from substrat.orchestrator import Orchestrator
from substrat.scheduler import TurnScheduler
from substrat.session import SessionStore
from substrat.session.multiplexer import SessionMultiplexer

from .vfs import CrashError, VirtualFS, patch_io

pytestmark = pytest.mark.stress


# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Minimal provider session for crash fuzzing."""

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        yield "ok"

    async def suspend(self) -> bytes:
        return b"s"

    async def restore(self, state: bytes) -> None:
        pass

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
        log: Any = None,
    ) -> FakeProviderSession:
        return FakeProviderSession()

    async def restore(
        self,
        state: bytes,
        log: Any = None,
    ) -> FakeProviderSession:
        return FakeProviderSession()


# -- Shadow state ----------------------------------------------------------


@dataclass
class ShadowAgent:
    """Committed agent tracked by the shadow."""

    agent_id: UUID
    name: str
    parent_id: UUID | None
    session_id: UUID
    instructions: str


# -- Helpers ---------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine in the current event loop or create one."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        import nest_asyncio  # type: ignore[import-untyped]

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    return asyncio.run(coro)


def _make_orch(vfs: VirtualFS) -> Orchestrator:
    """Build a fresh orchestrator stack against the VFS root."""
    root = Path(vfs.root) / "sessions"
    store = SessionStore(root)
    mux = SessionMultiplexer(store, max_slots=2)
    scheduler = TurnScheduler(
        providers={"fake": FakeProvider()},
        mux=mux,
        store=store,
        log_root=root,
    )
    return Orchestrator(
        scheduler,
        default_provider="fake",
        default_model="m",
    )


def _tree_agent_ids(orch: Orchestrator) -> set[UUID]:
    """Collect all agent IDs from the orchestrator's tree."""
    ids: set[UUID] = set()
    for root in orch.tree.roots():
        ids.add(root.id)
        ids.update(n.id for n in orch.tree.subtree(root.id))
    return ids


# -- State machine ---------------------------------------------------------


class OrchCrashRecoveryMachine(RuleBasedStateMachine):
    """Fuzz orchestrator crash recovery with random lifecycle + crash injection.

    Shadow state tracks which agents have been durably committed. On crash,
    the shadow is reconciled against the recovered tree (ground truth).
    Invariants verify consistency after every step.
    """

    agents = Bundle("agents")

    def __init__(self) -> None:
        super().__init__()
        self.vfs = VirtualFS()
        self._patch_ctx = patch_io(self.vfs)
        self._patch_ctx.__enter__()

        # Ensure the sessions directory exists.
        self.vfs.mkdir(
            str(Path(self.vfs.root) / "sessions"), parents=True, exist_ok=True
        )

        self.orch = _make_orch(self.vfs)

        # Shadow: committed agents (survived to disk).
        self.shadow: dict[UUID, ShadowAgent] = {}
        # Pending children: in tree but no session yet (deferred spawn).
        self.pending: set[UUID] = set()
        # Parents that need run_turn to drain deferred work.
        self.parents_needing_drain: set[UUID] = set()
        # Parent -> children mapping.
        self.children_map: dict[UUID, set[UUID]] = {}

    def teardown(self) -> None:
        self._patch_ctx.__exit__(None, None, None)

    # -- Crash + recovery --------------------------------------------------

    def _do_crash_and_recover(self) -> None:
        """Thaw VFS, build fresh orchestrator, recover, reconcile shadow."""
        self.vfs.thaw()
        self.orch = _make_orch(self.vfs)
        _run(self.orch.recover())

        # Purge pending children — no disk footprint.
        self.pending.clear()
        self.parents_needing_drain.clear()

        self._reconcile_shadow()

    def _reconcile_shadow(self) -> None:
        """Make shadow follow reality after recovery.

        The recovered tree is ground truth. The shadow must match it for
        future rules to work correctly.
        """
        recovered_ids = _tree_agent_ids(self.orch)

        # Remove agents from shadow that didn't survive.
        vanished = set(self.shadow) - recovered_ids
        for aid in vanished:
            del self.shadow[aid]

        # Add agents in recovered tree but not in shadow (partially committed).
        for aid in recovered_ids - set(self.shadow):
            node = self.orch.tree.get(aid)
            self.shadow[aid] = ShadowAgent(
                agent_id=aid,
                name=node.name,
                parent_id=node.parent_id,
                session_id=node.session_id,
                instructions=node.instructions,
            )

        # Rebuild children_map from recovered tree.
        self.children_map.clear()
        for aid in self.shadow:
            node = self.orch.tree.get(aid)
            self.children_map[aid] = {c.id for c in self.orch.tree.children(aid)}

    # -- Rules -------------------------------------------------------------

    _NAMES = st.sampled_from(["a", "b", "c", "d", "e"])

    @initialize(target=agents)
    def seed_agent(self) -> UUID:
        """Create the first root without crash. Other rules need at least one agent."""
        node = _run(self.orch.create_root_agent("seed", "init"))
        self.shadow[node.id] = ShadowAgent(
            agent_id=node.id,
            name="seed",
            parent_id=None,
            session_id=node.session_id,
            instructions="init",
        )
        self.children_map[node.id] = set()
        return node.id

    @rule(target=agents, name=_NAMES, crash_at=st.integers(0, 50))
    def create_root(self, name: str, crash_at: int) -> UUID:
        """Create a root agent, optionally crashing mid-operation."""
        if crash_at > 0:
            self.vfs.arm(crash_at)
        try:
            node = _run(self.orch.create_root_agent(name, f"inst-{name}"))
        except CrashError:
            self._do_crash_and_recover()
            return UUID(int=0)
        except ValueError:
            # Name collision.
            if crash_at > 0:
                self.vfs.disarm()
            return UUID(int=0)
        if crash_at > 0:
            self.vfs.disarm()
        # Success — update shadow.
        self.shadow[node.id] = ShadowAgent(
            agent_id=node.id,
            name=name,
            parent_id=None,
            session_id=node.session_id,
            instructions=f"inst-{name}",
        )
        self.children_map[node.id] = set()
        return node.id

    @precondition(lambda self: bool(self.shadow))
    @rule(target=agents, agent=agents, name=_NAMES)
    def spawn_child(self, agent: UUID, name: str) -> UUID:
        """Spawn a child on a living agent. No crash — purely in-memory."""
        if agent not in self.shadow or agent in self.pending:
            return UUID(int=0)
        handler = self.orch.get_handler(agent)
        result = handler.spawn_agent(name, f"child-{name}")
        if "error" in result:
            return UUID(int=0)
        child_id = UUID(result["agent_id"])
        self.pending.add(child_id)
        self.parents_needing_drain.add(agent)
        self.children_map[agent].add(child_id)
        self.children_map[child_id] = set()
        return child_id

    @precondition(lambda self: bool(self.parents_needing_drain))
    @rule(agent=agents, crash_at=st.integers(0, 50))
    def run_turn(self, agent: UUID, crash_at: int) -> None:
        """Run a turn on a living agent, optionally crashing mid-operation."""
        if agent not in self.shadow or agent in self.pending:
            return
        node = self.orch.tree.get(agent)
        if node.state != AgentState.IDLE:
            return
        if crash_at > 0:
            self.vfs.arm(crash_at)
        try:
            _run(self.orch.run_turn(agent, "go"))
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.vfs.disarm()
        # Success — deferred drained, children now committed.
        if agent in self.parents_needing_drain:
            self.parents_needing_drain.discard(agent)
            for child_id in list(self.children_map.get(agent, set())):
                if child_id in self.pending:
                    self.pending.discard(child_id)
                    # Materialize into shadow.
                    child_node = self.orch.tree.get(child_id)
                    self.shadow[child_id] = ShadowAgent(
                        agent_id=child_id,
                        name=child_node.name,
                        parent_id=agent,
                        session_id=child_node.session_id,
                        instructions=child_node.instructions,
                    )

    @precondition(lambda self: bool(self.shadow))
    @rule(agent=agents, crash_at=st.integers(0, 50))
    def run_turn_no_drain(self, agent: UUID, crash_at: int) -> None:
        """Run a turn on an agent that has no pending children."""
        if agent not in self.shadow or agent in self.pending:
            return
        if agent in self.parents_needing_drain:
            return
        node = self.orch.tree.get(agent)
        if node.state != AgentState.IDLE:
            return
        if crash_at > 0:
            self.vfs.arm(crash_at)
        try:
            _run(self.orch.run_turn(agent, "go"))
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.vfs.disarm()

    @precondition(lambda self: bool(self.shadow))
    @rule(agent=agents, data=st.data(), crash_at=st.integers(0, 50))
    def send_message(self, agent: UUID, data: st.DataObject, crash_at: int) -> None:
        """Send a message from parent to child."""
        if agent not in self.shadow or agent in self.pending:
            return
        materialized = [
            c
            for c in self.children_map.get(agent, set())
            if c not in self.pending and c in self.shadow
        ]
        if not materialized:
            return
        child_id = data.draw(st.sampled_from(sorted(materialized)))
        child_node = self.orch.tree.get(child_id)
        handler = self.orch.get_handler(agent)
        if crash_at > 0:
            self.vfs.arm(crash_at)
        try:
            handler.send_message(child_node.name, "hello")
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.vfs.disarm()

    @precondition(lambda self: bool(self.shadow))
    @rule(agent=agents, crash_at=st.integers(0, 50))
    def check_inbox(self, agent: UUID, crash_at: int) -> None:
        """Drain an agent's inbox."""
        if agent not in self.shadow or agent in self.pending:
            return
        handler = self.orch.get_handler(agent)
        if crash_at > 0:
            self.vfs.arm(crash_at)
        try:
            handler.check_inbox()
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.vfs.disarm()

    @precondition(lambda self: bool(self.shadow))
    @rule(agent=agents, crash_at=st.integers(0, 50))
    def terminate_leaf(self, agent: UUID, crash_at: int) -> None:
        """Terminate a living leaf agent."""
        if agent not in self.shadow:
            return
        kids = self.children_map.get(agent, set())
        if kids:
            return
        if agent in self.pending:
            return
        node = self.orch.tree.get(agent)
        if node.state != AgentState.IDLE:
            return
        if crash_at > 0:
            self.vfs.arm(crash_at)
        try:
            _run(self.orch.terminate_agent(agent))
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.vfs.disarm()
        # Success — remove from shadow.
        del self.shadow[agent]
        for _pid, kids_set in self.children_map.items():
            kids_set.discard(agent)
        del self.children_map[agent]

    # -- Invariants --------------------------------------------------------

    @invariant()
    def tree_matches_shadow(self) -> None:
        """Agent IDs in the live tree match shadow (excluding pending)."""
        tree_ids = _tree_agent_ids(self.orch)
        shadow_ids = set(self.shadow) | self.pending
        assert tree_ids == shadow_ids, f"tree={tree_ids}, shadow+pending={shadow_ids}"

    @invariant()
    def parent_child_consistent(self) -> None:
        """For every committed agent, tree.children matches children_map."""
        for aid in self.shadow:
            if aid not in self.orch.tree:
                continue
            real_children = {c.id for c in self.orch.tree.children(aid)}
            shadow_children = self.children_map.get(aid, set())
            assert real_children == shadow_children, (
                f"agent {aid}: real={real_children}, shadow={shadow_children}"
            )

    @invariant()
    def registries_in_sync(self) -> None:
        """Tree, handlers, and inboxes contain the same agent IDs."""
        tree_ids = _tree_agent_ids(self.orch)
        handler_ids = set(self.orch._handlers.keys())
        inbox_ids = set(self.orch.inboxes.keys())
        # Pending children are in tree but don't have handlers yet.
        materialized = tree_ids - self.pending
        assert materialized == handler_ids, (
            f"tree(materialized)={materialized}, handlers={handler_ids}"
        )
        assert tree_ids == inbox_ids, f"tree={tree_ids}, inboxes={inbox_ids}"

    @invariant()
    def all_idle(self) -> None:
        """All living agents are IDLE between steps."""
        for aid in self.shadow:
            if aid not in self.orch.tree:
                continue
            node = self.orch.tree.get(aid)
            assert node.state == AgentState.IDLE, (
                f"agent {aid} in state {node.state.value}, expected IDLE"
            )

    @invariant()
    def event_logs_valid_jsonl(self) -> None:
        """Every events.jsonl on disk contains only valid JSONL lines."""
        for path, content in self.vfs._disk.items():
            if not path.endswith("/events.jsonl"):
                continue
            for line in content.split(b"\n"):
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"corrupt JSONL in {path}: {line!r}") from exc

    @invariant()
    def session_files_valid(self) -> None:
        """Every session.json on disk is valid JSON with a valid state field."""
        valid_states = {"created", "active", "suspended", "terminated"}
        for path, content in self.vfs._disk.items():
            if not path.endswith("/session.json"):
                continue
            obj = json.loads(content)
            assert obj.get("state") in valid_states, (
                f"bad state in {path}: {obj.get('state')}"
            )

    @invariant()
    def inbox_matches_events(self) -> None:
        """For each materialized agent, inbox matches enqueued - delivered."""
        for aid in self.shadow:
            if aid in self.pending:
                continue
            if aid not in self.orch.tree:
                continue
            node = self.orch.tree.get(aid)
            log_path = (
                Path(self.vfs.root) / "sessions" / node.session_id.hex / "events.jsonl"
            )
            entries = read_log(log_path)
            enqueued: set[str] = set()
            delivered: set[str] = set()
            for entry in entries:
                ev = entry.get("event")
                data = entry.get("data", {})
                if ev == "message.enqueued":
                    mid = data.get("message_id", "")
                    if mid:
                        enqueued.add(mid)
                elif ev == "message.delivered":
                    mid = data.get("message_id", "")
                    if mid:
                        delivered.add(mid)
            expected_pending = enqueued - delivered
            inbox = self.orch.inboxes.get(aid)
            actual_pending = set()
            if inbox is not None:
                actual_pending = {m.id.hex for m in inbox.peek()}
            assert expected_pending == actual_pending, (
                f"agent {aid}: expected pending={expected_pending}, "
                f"actual inbox={actual_pending}"
            )


# Hypothesis needs a concrete TestCase class.
TestOrchCrashRecoveryFuzz = OrchCrashRecoveryMachine.TestCase
TestOrchCrashRecoveryFuzz.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    deadline=None,
)
