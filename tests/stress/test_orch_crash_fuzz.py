# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Dual-orchestrator crash-recovery fuzzer.

Runs two orchestrators in lockstep on separate VFS instances. The *reference*
never crashes; the *test* crashes and recovers. After recovery, structural
state is compared: every agent in the reference must appear in the test with
matching tree shape and state.

This eliminates the hand-rolled shadow model — the reference orchestrator *is*
the oracle.

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
from substrat.logging import EventLog
from substrat.logging.event_log import read_log
from substrat.orchestrator import Orchestrator
from substrat.scheduler import TurnScheduler
from substrat.session import SessionStore
from substrat.session.multiplexer import SessionMultiplexer

from .vfs import CrashError, VirtualFS, patch_io_multi

pytestmark = pytest.mark.stress


# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Minimal provider session for crash fuzzing."""

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
        **kwargs: object,
    ) -> FakeProviderSession:
        return FakeProviderSession()

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
        **kwargs: object,
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


# -- Name paths ------------------------------------------------------------


NamePath = tuple[str, ...]


def _name_path(orch: Orchestrator, agent_id: UUID) -> NamePath:
    """Build (root_name, ..., agent_name) path for an agent."""
    parts: list[str] = []
    node = orch.tree.get(agent_id)
    while True:
        parts.append(node.name)
        if node.parent_id is None:
            break
        node = orch.tree.get(node.parent_id)
    return tuple(reversed(parts))


def _resolve(orch: Orchestrator, path: NamePath) -> UUID | None:
    """Resolve a name path to an agent UUID. Returns None if not found."""
    if not path:
        return None
    # Find root by first name component.
    root_node = None
    for r in orch.tree.roots():
        if r.name == path[0]:
            root_node = r
            break
    if root_node is None:
        return None
    current = root_node
    for name in path[1:]:
        found = None
        for child in orch.tree.children(current.id):
            if child.name == name:
                found = child
                break
        if found is None:
            return None
        current = found
    return current.id


def _all_tree_ids(orch: Orchestrator) -> set[UUID]:
    """Collect all agent IDs from the orchestrator's tree."""
    ids: set[UUID] = set()
    for root in orch.tree.roots():
        ids.add(root.id)
        ids.update(n.id for n in orch.tree.subtree(root.id))
    return ids


# -- Snapshots -------------------------------------------------------------


@dataclass(frozen=True)
class AgentSnapshot:
    """Structural snapshot of one agent."""

    name_path: NamePath
    children: frozenset[NamePath]
    state: str


def _snapshot(orch: Orchestrator) -> dict[NamePath, AgentSnapshot]:
    """Build structural snapshot keyed by name path."""
    result: dict[NamePath, AgentSnapshot] = {}
    for root in orch.tree.roots():
        _snapshot_subtree(orch, root.id, (), result)
    return result


def _snapshot_subtree(
    orch: Orchestrator,
    aid: UUID,
    parent_path: NamePath,
    result: dict[NamePath, AgentSnapshot],
) -> None:
    node = orch.tree.get(aid)
    path = parent_path + (node.name,)
    children_paths = frozenset(path + (c.name,) for c in orch.tree.children(aid))
    result[path] = AgentSnapshot(
        name_path=path,
        children=children_paths,
        state=node.state.value,
    )
    for child in orch.tree.children(aid):
        _snapshot_subtree(orch, child.id, path, result)


# -- Sentinel for failed bundle entries ------------------------------------

_DEAD: NamePath = ("",)


def _is_dead(path: NamePath) -> bool:
    return len(path) == 1 and path[0] == ""


# -- State machine ---------------------------------------------------------


class DualOrchCrashMachine(RuleBasedStateMachine):
    """Fuzz orchestrator crash recovery with a dual-orchestrator oracle.

    Reference orchestrator never crashes. Test orchestrator crashes and
    recovers. After recovery, ref is rebuilt from test's disk state so both
    stay in sync. Structural comparison replaces the old shadow model.
    """

    agents = Bundle("agents")

    def __init__(self) -> None:
        super().__init__()
        self.ref_vfs = VirtualFS(root="/virtual/ref", fd_base=1000)
        self.test_vfs = VirtualFS(root="/virtual/test", fd_base=2000)
        self._patch_ctx = patch_io_multi(self.ref_vfs, self.test_vfs)
        self._patch_ctx.__enter__()

        # Ensure session dirs exist.
        for vfs in (self.ref_vfs, self.test_vfs):
            vfs.mkdir(str(Path(vfs.root) / "sessions"), parents=True, exist_ok=True)

        self.ref = _make_orch(self.ref_vfs)
        self.test = _make_orch(self.test_vfs)

        # Pending children: in tree but no session yet (deferred spawn).
        self.pending_paths: set[NamePath] = set()
        # Parents with un-drained deferred spawns.
        self.parents_needing_drain: set[NamePath] = set()

    def teardown(self) -> None:
        self._patch_ctx.__exit__(None, None, None)

    # -- Crash + recovery --------------------------------------------------

    def _do_crash_and_recover(self) -> None:
        """Thaw test VFS, recover test, rebuild ref from test's disk."""
        # 1. Thaw and recover test.
        self.test_vfs.thaw()
        self.test = _make_orch(self.test_vfs)
        _run(self.test.recover())

        # 2. Copy test disk to ref (adjusting path prefixes).
        #    Reset all ephemeral state — this is a full "reboot".
        self.ref_vfs._disk.clear()
        self.ref_vfs._cache.clear()
        self.ref_vfs._fd_table.clear()
        self.ref_vfs._all_fds.clear()
        self.ref_vfs._next_fd = 1000  # Reset to fd_base.
        for path, content in self.test_vfs._disk.items():
            ref_path = path.replace("/virtual/test/", "/virtual/ref/", 1)
            self.ref_vfs._disk[ref_path] = content
        self.ref_vfs._dirs = {
            d.replace("/virtual/test/", "/virtual/ref/", 1) for d in self.test_vfs._dirs
        }

        # 3. Recover ref from copied disk.
        self.ref = _make_orch(self.ref_vfs)
        _run(self.ref.recover())

        # 4. Clear pending tracking — no disk footprint survived.
        self.pending_paths.clear()
        self.parents_needing_drain.clear()

    # -- Rules -------------------------------------------------------------

    _NAMES = st.sampled_from(["a", "b", "c", "d", "e"])

    @initialize(target=agents)
    def seed_agent(self) -> NamePath:
        """Create the first root on both orchs without crash."""
        _run(self.ref.create_root_agent("seed", "init"))
        _run(self.test.create_root_agent("seed", "init"))
        return ("seed",)

    @rule(target=agents, name=_NAMES, crash_at=st.integers(0, 200))
    def create_root(self, name: str, crash_at: int) -> NamePath:
        """Create a root agent on both orchs, optionally crashing test."""
        # Ref first.
        try:
            _run(self.ref.create_root_agent(name, f"inst-{name}"))
        except ValueError:
            # Name collision — skip test too.
            return _DEAD

        # Test (may crash).
        if crash_at > 0:
            self.test_vfs.arm(crash_at)
        try:
            _run(self.test.create_root_agent(name, f"inst-{name}"))
        except CrashError:
            self._do_crash_and_recover()
            return _DEAD
        except ValueError:
            # Shouldn't happen (ref succeeded), but defend anyway.
            if crash_at > 0:
                self.test_vfs.disarm()
            return _DEAD
        if crash_at > 0:
            self.test_vfs.disarm()
        return (name,)

    @precondition(lambda self: bool(_all_tree_ids(self.ref)))
    @rule(target=agents, agent=agents, name=_NAMES)
    def spawn_child(self, agent: NamePath, name: str) -> NamePath:
        """Spawn a child on a living agent. No crash — purely in-memory."""
        if _is_dead(agent) or agent in self.pending_paths:
            return _DEAD
        ref_id = _resolve(self.ref, agent)
        test_id = _resolve(self.test, agent)
        if ref_id is None or test_id is None:
            return _DEAD

        ref_handler = self.ref.get_handler(ref_id)
        result = ref_handler.spawn_agent(name, f"child-{name}")
        if "error" in result:
            return _DEAD

        test_handler = self.test.get_handler(test_id)
        test_result = test_handler.spawn_agent(name, f"child-{name}")
        if "error" in test_result:
            # Shouldn't happen if ref succeeded, but be safe.
            return _DEAD

        child_path = agent + (name,)
        self.pending_paths.add(child_path)
        self.parents_needing_drain.add(agent)
        return child_path

    @precondition(lambda self: bool(_all_tree_ids(self.ref)))
    @rule(agent=agents, crash_at=st.integers(0, 200))
    def run_turn(self, agent: NamePath, crash_at: int) -> None:
        """Run a turn on a living agent, draining deferred spawns."""
        if _is_dead(agent) or agent in self.pending_paths:
            return
        ref_id = _resolve(self.ref, agent)
        test_id = _resolve(self.test, agent)
        if ref_id is None or test_id is None:
            return

        # Ref always succeeds.
        _run(self.ref.run_turn(ref_id, "go"))

        # Test may crash.
        if crash_at > 0:
            self.test_vfs.arm(crash_at)
        try:
            _run(self.test.run_turn(test_id, "go"))
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.test_vfs.disarm()

        # Drain pending children of this parent.
        self.parents_needing_drain.discard(agent)
        drained = {p for p in self.pending_paths if p[:-1] == agent}
        self.pending_paths -= drained

    @precondition(lambda self: bool(_all_tree_ids(self.ref)))
    @rule(agent=agents, data=st.data(), crash_at=st.integers(0, 200))
    def send_message(self, agent: NamePath, data: st.DataObject, crash_at: int) -> None:
        """Send a message from parent to a materialized child."""
        if _is_dead(agent) or agent in self.pending_paths:
            return
        ref_id = _resolve(self.ref, agent)
        test_id = _resolve(self.test, agent)
        if ref_id is None or test_id is None:
            return

        # Find materialized children via ref tree.
        materialized: list[str] = []
        for child in self.ref.tree.children(ref_id):
            child_path = agent + (child.name,)
            if child_path not in self.pending_paths:
                materialized.append(child.name)
        if not materialized:
            return
        child_name = data.draw(st.sampled_from(sorted(materialized)))

        # Ref always succeeds.
        ref_handler = self.ref.get_handler(ref_id)
        ref_handler.send_message(child_name, "hello")

        # Test may crash.
        test_handler = self.test.get_handler(test_id)
        if crash_at > 0:
            self.test_vfs.arm(crash_at)
        try:
            test_handler.send_message(child_name, "hello")
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.test_vfs.disarm()

    @precondition(lambda self: bool(_all_tree_ids(self.ref)))
    @rule(agent=agents, crash_at=st.integers(0, 200))
    def broadcast_to_team(self, agent: NamePath, crash_at: int) -> None:
        """Broadcast from a non-root agent to its materialized siblings."""
        if _is_dead(agent) or agent in self.pending_paths:
            return
        if len(agent) < 2:
            return  # Roots have no team.
        ref_id = _resolve(self.ref, agent)
        test_id = _resolve(self.test, agent)
        if ref_id is None or test_id is None:
            return

        # All siblings must be materialized.
        parent_path = agent[:-1]
        ref_parent_id = _resolve(self.ref, parent_path)
        if ref_parent_id is None:
            return
        siblings = self.ref.tree.children(ref_parent_id)
        for sib in siblings:
            if sib.id == ref_id:
                continue
            sib_path = parent_path + (sib.name,)
            if sib_path in self.pending_paths:
                return

        # Ref always succeeds.
        ref_handler = self.ref.get_handler(ref_id)
        ref_handler.broadcast("team update")

        # Test may crash.
        test_handler = self.test.get_handler(test_id)
        if crash_at > 0:
            self.test_vfs.arm(crash_at)
        try:
            test_handler.broadcast("team update")
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.test_vfs.disarm()

    @precondition(lambda self: bool(_all_tree_ids(self.ref)))
    @rule(agent=agents, crash_at=st.integers(0, 200))
    def check_inbox(self, agent: NamePath, crash_at: int) -> None:
        """Drain an agent's inbox."""
        if _is_dead(agent) or agent in self.pending_paths:
            return
        ref_id = _resolve(self.ref, agent)
        test_id = _resolve(self.test, agent)
        if ref_id is None or test_id is None:
            return

        # Ref always succeeds.
        ref_handler = self.ref.get_handler(ref_id)
        ref_handler.check_inbox()

        # Test may crash.
        test_handler = self.test.get_handler(test_id)
        if crash_at > 0:
            self.test_vfs.arm(crash_at)
        try:
            test_handler.check_inbox()
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.test_vfs.disarm()

    @precondition(lambda self: bool(_all_tree_ids(self.ref)))
    @rule(agent=agents, crash_at=st.integers(0, 200))
    def terminate_leaf(self, agent: NamePath, crash_at: int) -> None:
        """Terminate a living leaf agent."""
        if _is_dead(agent) or agent in self.pending_paths:
            return
        ref_id = _resolve(self.ref, agent)
        test_id = _resolve(self.test, agent)
        if ref_id is None or test_id is None:
            return
        # Must be a leaf in both orchs.
        if self.ref.tree.children(ref_id) or self.test.tree.children(test_id):
            return

        # Ref always succeeds.
        _run(self.ref.terminate_agent(ref_id))

        # Test may crash.
        if crash_at > 0:
            self.test_vfs.arm(crash_at)
        try:
            _run(self.test.terminate_agent(test_id))
        except CrashError:
            self._do_crash_and_recover()
            return
        if crash_at > 0:
            self.test_vfs.disarm()

    # -- Invariants --------------------------------------------------------

    @invariant()
    def ref_subset_of_test(self) -> None:
        """Every non-pending agent in ref appears in test with matching state."""
        ref_snap = _snapshot(self.ref)
        test_snap = _snapshot(self.test)
        for path, ref_agent in ref_snap.items():
            if path in self.pending_paths:
                continue
            assert path in test_snap, (
                f"ref agent {path} missing from test. "
                f"ref paths={set(ref_snap)}, test paths={set(test_snap)}"
            )
            test_agent = test_snap[path]
            assert ref_agent.state == test_agent.state, (
                f"state mismatch at {path}: ref={ref_agent.state}, "
                f"test={test_agent.state}"
            )
            # Compare children excluding pending.
            ref_children = ref_agent.children - self.pending_paths
            test_children = test_agent.children - self.pending_paths
            assert ref_children == test_children, (
                f"children mismatch at {path}: ref={ref_children}, test={test_children}"
            )

    @invariant()
    def registries_in_sync(self) -> None:
        """Tree, handlers, and inboxes contain the same agent IDs."""
        for label, orch, pending in [
            ("ref", self.ref, self.pending_paths),
            ("test", self.test, self.pending_paths),
        ]:
            tree_ids = _all_tree_ids(orch)
            handler_ids = set(orch._handlers.keys())
            inbox_ids = set(orch.inboxes.keys())
            # Resolve pending paths to UUIDs for this orch.
            pending_ids = set[UUID]()
            for p in pending:
                uid = _resolve(orch, p)
                if uid is not None:
                    pending_ids.add(uid)
            materialized = tree_ids - pending_ids
            assert materialized == handler_ids, (
                f"{label}: tree(materialized)={materialized}, handlers={handler_ids}"
            )
            assert tree_ids == inbox_ids, (
                f"{label}: tree={tree_ids}, inboxes={inbox_ids}"
            )

    @invariant()
    def all_idle(self) -> None:
        """All living agents are IDLE between steps."""
        for label, orch in [("ref", self.ref), ("test", self.test)]:
            for root in orch.tree.roots():
                for node in [root] + orch.tree.subtree(root.id):
                    assert node.state == AgentState.IDLE, (
                        f"{label} agent {node.name} in state "
                        f"{node.state.value}, expected IDLE"
                    )

    @invariant()
    def event_logs_valid_jsonl(self) -> None:
        """Every events.jsonl on disk is valid JSONL."""
        for vfs in (self.ref_vfs, self.test_vfs):
            for path, content in vfs._disk.items():
                if not path.endswith("/events.jsonl"):
                    continue
                for line in content.split(b"\n"):
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AssertionError(
                            f"corrupt JSONL in {path}: {line!r}"
                        ) from exc

    @invariant()
    def session_files_valid(self) -> None:
        """Every session.json on disk is valid JSON with a valid state."""
        valid_states = {"created", "active", "suspended", "terminated"}
        for vfs in (self.ref_vfs, self.test_vfs):
            for path, content in vfs._disk.items():
                if not path.endswith("/session.json"):
                    continue
                obj = json.loads(content)
                assert obj.get("state") in valid_states, (
                    f"bad state in {path}: {obj.get('state')}"
                )

    @invariant()
    def inbox_matches_events(self) -> None:
        """For each materialized agent in test, inbox matches events."""
        for root in self.test.tree.roots():
            for node in [root] + self.test.tree.subtree(root.id):
                path = _name_path(self.test, node.id)
                if path in self.pending_paths:
                    continue
                log_path = (
                    Path(self.test_vfs.root)
                    / "sessions"
                    / node.session_id.hex
                    / "events.jsonl"
                )
                entries = read_log(log_path)
                enqueued: set[str] = set()
                delivered: set[str] = set()
                for entry in entries:
                    ev = entry.get("event")
                    edata = entry.get("data", {})
                    if ev == "message.enqueued":
                        mid = edata.get("message_id", "")
                        if mid:
                            enqueued.add(mid)
                    elif ev == "message.delivered":
                        mid = edata.get("message_id", "")
                        if mid:
                            delivered.add(mid)
                expected_pending = enqueued - delivered
                inbox = self.test.inboxes.get(node.id)
                actual_pending = set[str]()
                if inbox is not None:
                    actual_pending = {m.id.hex for m in inbox.peek()}
                assert expected_pending == actual_pending, (
                    f"test agent {path}: expected pending="
                    f"{expected_pending}, actual inbox={actual_pending}"
                )


# Hypothesis needs a concrete TestCase class.
TestDualOrchCrashFuzz = DualOrchCrashMachine.TestCase
TestDualOrchCrashFuzz.settings = settings(
    max_examples=300,
    stateful_step_count=40,
    deadline=None,
)
