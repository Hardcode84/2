# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the orchestrator — agent-session bridge."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from substrat.agent import AgentState, AgentStateError
from substrat.logging import EventLog
from substrat.orchestrator import Orchestrator
from substrat.scheduler import TurnScheduler
from substrat.session import SessionStore
from substrat.session.multiplexer import SessionMultiplexer
from substrat.workspace.mapping import WorkspaceMapping
from substrat.workspace.model import Workspace
from substrat.workspace.store import WorkspaceStore

# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Minimal provider session for testing."""

    def __init__(
        self,
        chunks: list[str] | None = None,
        prompt_log: list[str] | None = None,
    ) -> None:
        self._chunks = chunks if chunks is not None else ["ok"]
        self._prompt_log = prompt_log
        self.stopped = False

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        if self._prompt_log is not None:
            self._prompt_log.append(message)
        for chunk in self._chunks:
            yield chunk

    async def suspend(self) -> bytes:
        return b"fake-state"

    async def stop(self) -> None:
        self.stopped = True


class ErrorProviderSession(FakeProviderSession):
    """Provider session whose send() always raises."""

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        raise RuntimeError("send failed")
        yield ""  # noqa: RUF027  # Unreachable, makes it a generator.


class FakeProvider:
    """Tracks create calls for assertions."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self._chunks = chunks
        self.created: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self._error_on_send = False

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
        self.created.append((model, system_prompt))
        if self._error_on_send:
            return ErrorProviderSession()
        return FakeProviderSession(self._chunks, self.prompts)

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
        **kwargs: object,
    ) -> FakeProviderSession:
        return FakeProviderSession(self._chunks, self.prompts)


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@pytest.fixture()
def mux(store: SessionStore) -> SessionMultiplexer:
    return SessionMultiplexer(store, max_slots=4)


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture()
def scheduler(
    provider: FakeProvider,
    mux: SessionMultiplexer,
    store: SessionStore,
) -> TurnScheduler:
    return TurnScheduler(
        providers={"fake": provider},
        mux=mux,
        store=store,
    )


@pytest.fixture()
def orch(scheduler: TurnScheduler) -> Orchestrator:
    return Orchestrator(
        scheduler,
        default_provider="fake",
        default_model="test-model",
    )


# -- create_root_agent -----------------------------------------------------


async def test_create_root_agent(orch: Orchestrator) -> None:
    """Creates node in tree, inbox, handler, and backing session."""
    node = await orch.create_root_agent("alpha", "do things")
    assert node.name == "alpha"
    assert node.instructions == "do things"
    assert node.state == AgentState.IDLE
    assert node.id in orch.tree
    assert node.id in orch.inboxes
    assert orch.get_handler(node.id) is not None


async def test_create_root_agent_custom_provider(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    """Custom provider/model override."""
    alt = FakeProvider()
    sched = TurnScheduler(
        providers={"fake": FakeProvider(), "alt": alt},
        mux=mux,
        store=store,
    )
    o = Orchestrator(sched, default_provider="fake", default_model="default-m")
    node = await o.create_root_agent(
        "beta",
        "instructions",
        provider="alt",
        model="special-m",
    )
    # Alt provider was called with the override model. Prompt wraps instructions.
    model, prompt = alt.created[-1]
    assert model == "special-m"
    assert "instructions" in prompt
    assert node.name == "beta"


async def test_create_root_agent_duplicate_name(orch: Orchestrator) -> None:
    """Duplicate name raises ValueError; session is cleaned up."""
    await orch.create_root_agent("dup", "first")
    with pytest.raises(ValueError, match="sibling name collision"):
        await orch.create_root_agent("dup", "second")
    # Only one node in the tree — the orphaned session was terminated.
    assert len(orch.tree) == 1


async def test_create_root_agent_unknown_provider(
    scheduler: TurnScheduler,
) -> None:
    """Unknown provider propagates scheduler ValueError."""
    o = Orchestrator(
        scheduler,
        default_provider="nonexistent",
        default_model="m",
    )
    with pytest.raises(ValueError, match="unknown provider"):
        await o.create_root_agent("x", "y")


async def test_create_root_agent_with_workspace(tmp_path: Path) -> None:
    """Root agent with workspace gets wrap_command and ws_mapping entry."""
    ws_root = tmp_path / "workspaces"
    ws_store = WorkspaceStore(ws_root)
    ws_mapping = WorkspaceMapping()
    scope = uuid4()
    ws = Workspace(name="env", scope=scope, root_path=tmp_path / "ws-root")
    (tmp_path / "ws-root").mkdir()
    ws_store.save(ws)

    factory_calls: list[tuple[UUID, str]] = []

    def fake_factory(s: UUID, n: str) -> object:
        factory_calls.append((s, n))
        return lambda cmd, binds, env: cmd

    store = SessionStore(tmp_path / "sessions")
    mux = SessionMultiplexer(store, max_slots=4)
    prov = FakeProvider()
    sched = TurnScheduler(providers={"fake": prov}, mux=mux, store=store)
    o = Orchestrator(
        sched,
        default_provider="fake",
        default_model="m",
        ws_store=ws_store,
        ws_mapping=ws_mapping,
        wrap_command_factory=fake_factory,
    )
    node = await o.create_root_agent("w", "i", workspace=(scope, "env"))
    assert ws_mapping.get(node.id) == (scope, "env")
    assert len(factory_calls) == 1
    assert factory_calls[0] == (scope, "env")


# -- run_turn ---------------------------------------------------------------


async def test_run_turn_basic(orch: Orchestrator) -> None:
    """Basic round-trip returns response."""
    node = await orch.create_root_agent("a", "p")
    response = await orch.run_turn(node.id, "hello")
    assert response == "ok"


async def test_run_turn_state_transitions(orch: Orchestrator) -> None:
    """IDLE → BUSY → IDLE over a turn."""
    node = await orch.create_root_agent("a", "p")
    assert node.state == AgentState.IDLE
    # After the turn, back to IDLE.
    await orch.run_turn(node.id, "go")
    assert node.state == AgentState.IDLE


async def test_run_turn_error_resets_state(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    """Send error resets state to IDLE and propagates exception."""
    prov = FakeProvider()
    prov._error_on_send = True
    sched = TurnScheduler(providers={"fake": prov}, mux=mux, store=store)
    o = Orchestrator(sched, default_provider="fake", default_model="m")
    node = await o.create_root_agent("a", "p")
    with pytest.raises(RuntimeError, match="send failed"):
        await o.run_turn(node.id, "go")
    assert node.state == AgentState.IDLE


async def test_run_turn_not_idle(orch: Orchestrator) -> None:
    """Agent not IDLE raises AgentStateError."""
    node = await orch.create_root_agent("a", "p")
    node.begin_turn()  # Force BUSY.
    with pytest.raises(AgentStateError):
        await orch.run_turn(node.id, "go")


async def test_run_turn_drains_deferred(orch: Orchestrator) -> None:
    """Deferred spawns from tool handler are executed after the turn."""
    node = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(node.id)
    # Spawn a child via tool handler (defers session creation).
    result = handler.spawn_agent("child", "be helpful")
    assert result["status"] == "accepted"
    child_id = UUID(result["agent_id"])

    # Child exists in tree but has a placeholder session_id.
    child_node = orch.tree.get(child_id)
    placeholder_sid = child_node.session_id

    # Run a turn on the parent — this drains deferred, creating child's session.
    await orch.run_turn(node.id, "go")

    # Child's session_id is now patched to the real session.
    assert child_node.session_id != placeholder_sid
    # Child has its own handler.
    assert child_id in orch._handlers


# -- spawn lifecycle --------------------------------------------------------


async def test_spawn_child_session_patched(orch: Orchestrator) -> None:
    """Child session_id is patched to match the real session."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    result = handler.spawn_agent("kid", "instructions")
    child_id = UUID(result["agent_id"])
    child = orch.tree.get(child_id)
    old_sid = child.session_id

    await orch.run_turn(parent.id, "go")
    assert child.session_id != old_sid


async def test_spawn_child_gets_handler(orch: Orchestrator) -> None:
    """Spawned child gets its own ToolHandler in the registry."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    result = handler.spawn_agent("kid", "inst")
    child_id = UUID(result["agent_id"])

    await orch.run_turn(parent.id, "go")
    child_handler = orch.get_handler(child_id)
    assert child_handler is not None
    assert child_handler is not handler


async def test_spawn_child_inherits_provider(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Child inherits parent's provider/model."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("kid", "child instructions")

    provider.created.clear()
    await orch.run_turn(parent.id, "go")
    # Child session was created with same provider/model. Prompt wraps instructions.
    assert len(provider.created) >= 1
    model, prompt = provider.created[-1]
    assert model == "test-model"
    assert "child instructions" in prompt


async def test_spawn_multiple_children(orch: Orchestrator) -> None:
    """Multiple children spawned in one turn all get resolved."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    r1 = handler.spawn_agent("c1", "i1")
    r2 = handler.spawn_agent("c2", "i2")
    r3 = handler.spawn_agent("c3", "i3")

    await orch.run_turn(parent.id, "go")

    for r in (r1, r2, r3):
        cid = UUID(r["agent_id"])
        child = orch.tree.get(cid)
        # Each child has a handler and a real (non-placeholder) session.
        assert cid in orch._handlers
        assert child.session_id is not None


async def test_spawn_grandchild(orch: Orchestrator) -> None:
    """Grandchild wiring: child spawns grandchild, both get sessions."""
    parent = await orch.create_root_agent("parent", "p")
    h_parent = orch.get_handler(parent.id)
    r_child = h_parent.spawn_agent("child", "ci")
    child_id = UUID(r_child["agent_id"])

    # Drain parent's deferred — child gets session + handler.
    await orch.run_turn(parent.id, "go")

    # Now the child's handler can spawn a grandchild.
    h_child = orch.get_handler(child_id)
    r_grand = h_child.spawn_agent("grandchild", "gi")
    grand_id = UUID(r_grand["agent_id"])

    # Drain child's deferred — grandchild gets session + handler.
    await orch.run_turn(child_id, "go")

    grand = orch.tree.get(grand_id)
    assert grand_id in orch._handlers
    assert grand.session_id is not None


# -- terminate_agent --------------------------------------------------------


async def test_terminate_leaf(orch: Orchestrator) -> None:
    """Leaf agent: session terminated, node removed, handler + inbox gone."""
    node = await orch.create_root_agent("doomed", "p")
    nid = node.id
    await orch.terminate_agent(nid)

    assert nid not in orch.tree
    assert nid not in orch.inboxes
    with pytest.raises(KeyError):
        orch.get_handler(nid)


async def test_terminate_non_leaf(orch: Orchestrator) -> None:
    """Non-leaf agent raises ValueError."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("child", "ci")
    await orch.run_turn(parent.id, "go")

    with pytest.raises(ValueError, match="has children"):
        await orch.terminate_agent(parent.id)


async def test_get_handler_after_terminate(orch: Orchestrator) -> None:
    """get_handler raises KeyError after termination."""
    node = await orch.create_root_agent("gone", "p")
    nid = node.id
    await orch.terminate_agent(nid)
    with pytest.raises(KeyError):
        orch.get_handler(nid)


# -- get_handler ------------------------------------------------------------


async def test_get_handler(orch: Orchestrator) -> None:
    """Returns registered handler."""
    node = await orch.create_root_agent("a", "p")
    handler = orch.get_handler(node.id)
    assert handler is not None


async def test_get_handler_unknown(orch: Orchestrator) -> None:
    """Unknown agent_id raises KeyError."""
    from uuid import uuid4

    with pytest.raises(KeyError):
        orch.get_handler(uuid4())


# -- wake loop --------------------------------------------------------------


async def test_wake_on_send_message(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Sending a message wakes the IDLE recipient via the wake loop."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await orch.run_turn(parent.id, "go")

        child = orch.tree.children(parent.id)[0]
        # Run a turn on child to get it into a known state.
        await orch.run_turn(child.id, "wake up")

        provider.prompts.clear()
        handler = orch.get_handler(parent.id)
        handler.send_message("child", "hello child")

        # Let the wake loop process.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Child should have received a wake turn.
        assert any("hello child" in p for p in provider.prompts)
    finally:
        await orch.stop_wake_loop()


async def test_wake_skips_busy_agent(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """BUSY agent is not woken — wake is silently skipped."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await orch.run_turn(parent.id, "go")

        child = orch.tree.children(parent.id)[0]
        child.begin_turn()  # Force BUSY.

        provider.prompts.clear()
        handler = orch.get_handler(parent.id)
        handler.send_message("child", "while busy")

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # No wake turn sent — child was busy.
        assert not any("while busy" in p for p in provider.prompts)

        child.end_turn()  # Cleanup.
    finally:
        await orch.stop_wake_loop()


async def test_rewake_after_busy_turn(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Message arriving mid-turn triggers re-wake after turn ends."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await orch.run_turn(parent.id, "go")
        child = orch.tree.children(parent.id)[0]

        # Deliver a message directly to child's inbox (simulating mid-turn
        # delivery where the wake was skipped because child was BUSY).
        from substrat.agent.message import MessageEnvelope, MessageKind

        env = MessageEnvelope(
            sender=parent.id,
            recipient=child.id,
            kind=MessageKind.REQUEST,
            payload="catch me",
        )
        orch.inboxes[child.id].deliver(env)
        # No wake enqueued — simulates the lost-wake scenario.

        provider.prompts.clear()
        # Run a turn on child — _execute_turn will call _rewake_if_pending.
        await orch.run_turn(child.id, "working")

        # Let wake loop process the re-enqueued wake.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert any("catch me" in p for p in provider.prompts)
    finally:
        await orch.stop_wake_loop()


async def test_wake_skips_pending_spawn(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Wake for a child with no handler yet (pre-spawn) is skipped safely."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        result = handler.spawn_agent("child", "ci")
        child_id = UUID(result["agent_id"])

        # Child is in tree with inbox but no handler (spawn not drained).
        assert child_id not in orch._handlers

        # Manually enqueue a wake — simulates the race.
        orch._notify_wake(child_id)
        provider.prompts.clear()

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # No turn sent — child has no session.
        assert not provider.prompts

        # Inbox untouched — message still there for post-spawn wake.
        handler.send_message("child", "queued")
        assert orch.inboxes[child_id]
    finally:
        await orch.stop_wake_loop()


async def test_wake_skips_terminated_agent(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Terminated agent is skipped in wake processing."""
    orch.start_wake_loop()
    try:
        node = await orch.create_root_agent("doomed", "p")
        nid = node.id
        # Enqueue a wake, then terminate.
        orch._notify_wake(nid)
        await orch.terminate_agent(nid)

        provider.prompts.clear()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # No crash, no wake turn.
        assert not provider.prompts
    finally:
        await orch.stop_wake_loop()


async def test_wake_skips_empty_inbox(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Agent with empty inbox is not woken."""
    orch.start_wake_loop()
    try:
        node = await orch.create_root_agent("quiet", "p")
        provider.prompts.clear()
        orch._notify_wake(node.id)

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not provider.prompts
    finally:
        await orch.stop_wake_loop()


async def test_post_spawn_wake(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """spawn + send pattern: child wakes after parent's turn drains."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        handler.send_message("child", "go now")

        provider.prompts.clear()
        await orch.run_turn(parent.id, "trigger drain")

        # Let wake loop process.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert any("go now" in p for p in provider.prompts)
    finally:
        await orch.stop_wake_loop()


async def test_wake_prompt_format_single(orch: Orchestrator) -> None:
    """Single message formats as 'Message from <name>:\\n<payload>'."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("child", "ci")
    await orch.run_turn(parent.id, "go")

    child = orch.tree.children(parent.id)[0]
    handler = orch.get_handler(parent.id)
    handler.send_message("child", "single msg")

    prompt = orch._format_wake_prompt(child.id)
    assert prompt == "Message from parent:\nsingle msg"


async def test_wake_prompt_format_multi(orch: Orchestrator) -> None:
    """Multiple messages format as numbered list."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("a", "ai")
    handler.spawn_agent("b", "bi")
    await orch.run_turn(parent.id, "go")

    a_node = next(c for c in orch.tree.children(parent.id) if c.name == "a")
    # Send two messages to a.
    handler = orch.get_handler(parent.id)
    handler.send_message("a", "first")
    h_b = orch.get_handler(
        next(c.id for c in orch.tree.children(parent.id) if c.name == "b")
    )
    h_b.send_message("a", "second")

    prompt = orch._format_wake_prompt(a_node.id)
    assert "1. From parent: first" in prompt
    assert "2. From b: second" in prompt


async def test_wake_loop_start_stop(orch: Orchestrator) -> None:
    """start/stop lifecycle doesn't crash."""
    orch.start_wake_loop()
    assert orch._wake_task is not None
    await orch.stop_wake_loop()
    assert orch._wake_task is None


async def test_wake_loop_double_start(orch: Orchestrator) -> None:
    """Double start is idempotent."""
    orch.start_wake_loop()
    task = orch._wake_task
    orch.start_wake_loop()
    assert orch._wake_task is task
    await orch.stop_wake_loop()


async def test_wake_loop_stop_without_start(orch: Orchestrator) -> None:
    """Stopping without starting is a no-op."""
    await orch.stop_wake_loop()  # Should not crash.


# -- complete lifecycle -----------------------------------------------------


async def test_spawn_failure_cleans_up_child(tmp_path: Path) -> None:
    """If child session creation fails, the orphaned node is removed."""

    class FailOnSecondCreate(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self._count = 0

        async def create(
            self,
            model: str,
            system_prompt: str,
            log: EventLog | None = None,
            **kwargs: object,
        ) -> FakeProviderSession:
            self._count += 1
            if self._count > 1:
                raise RuntimeError("boom")
            return await super().create(model, system_prompt, log, **kwargs)

    prov = FailOnSecondCreate()
    store = SessionStore(tmp_path / "agents")
    mux = SessionMultiplexer(store)
    sched = TurnScheduler({"fake": prov}, mux, store, log_root=tmp_path / "agents")
    orch = Orchestrator(sched, default_provider="fake", default_model="m")

    root = await orch.create_root_agent("root", "instructions")
    handler = orch.get_handler(root.id)
    result = handler.spawn_agent("child", "doomed")
    child_id = UUID(result["agent_id"])

    # Child is in the tree before deferred work executes.
    assert child_id in orch.tree
    assert child_id in orch.inboxes

    # Deferred spawn will fail — child should be cleaned up.
    await orch._drain_deferred(root.id)

    assert child_id not in orch.tree
    assert child_id not in orch.inboxes


async def test_complete_lifecycle(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """spawn child → child calls complete → child terminates → parent wakes."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        result = handler.spawn_agent("child", "ci")
        child_id = UUID(result["agent_id"])
        await orch.run_turn(parent.id, "go")

        # Child calls complete — sends RESPONSE + defers termination.
        h_child = orch.get_handler(child_id)
        c_result = h_child.complete("task done")
        assert c_result["status"] == "completing"

        # Run a turn on child to drain deferred (terminate).
        await orch.run_turn(child_id, "finishing")

        # Child should be terminated.
        assert child_id not in orch.tree

        # Parent should have the RESPONSE in its inbox (or wake delivered it).
        # Give the wake loop a chance.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # At minimum the message was in the inbox before wake.
        # Check that the parent got woken with the result.
        assert any("task done" in p for p in provider.prompts)
    finally:
        await orch.stop_wake_loop()
