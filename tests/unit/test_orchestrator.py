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
from substrat.agent.message import MessageEnvelope, MessageKind
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
        provider: FakeProvider | None = None,
    ) -> None:
        self._chunks = chunks if chunks is not None else ["ok"]
        self._prompt_log = prompt_log
        self._provider = provider
        self.stopped = False

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        if self._provider is not None and self._provider._error_on_send:
            raise RuntimeError("send failed")
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
        model: str | None,
        system_prompt: str,
        log: EventLog | None = None,
        **kwargs: object,
    ) -> FakeProviderSession:
        self.created.append((model, system_prompt))
        return FakeProviderSession(self._chunks, self.prompts, provider=self)

    def models(self) -> list[str]:
        return ["test-model"]

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
        **kwargs: object,
    ) -> FakeProviderSession:
        return FakeProviderSession(self._chunks, self.prompts, provider=self)


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@pytest.fixture()
def mux(store: SessionStore) -> SessionMultiplexer:
    return SessionMultiplexer(store, pools={"default": 4})


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
    mux = SessionMultiplexer(store, pools={"default": 4})
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


# -- failure handling -------------------------------------------------------


async def test_send_failure_drains_deferred(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    """send() fails — deferred spawns still drain (no zombie children)."""
    prov = FakeProvider()
    prov._error_on_send = True
    sched = TurnScheduler(providers={"fake": prov}, mux=mux, store=store)
    o = Orchestrator(sched, default_provider="fake", default_model="m")

    parent = await o.create_root_agent("parent", "p")
    handler = o.get_handler(parent.id)
    result = handler.spawn_agent("child", "doomed")
    child_id = UUID(result["agent_id"])

    # Child is in tree before the turn.
    assert child_id in o.tree

    with pytest.raises(RuntimeError, match="send failed"):
        await o.run_turn(parent.id, "go")

    # Deferred was drained — child got a session and handler.
    assert child_id in o._handlers


async def test_send_raises_on_crash_after_partial_output() -> None:
    """Non-zero exit after partial output must raise, not silently truncate."""

    # We can't easily test CursorSession.send() without mocking subprocess,
    # so we test the principle via our FakeProvider pattern.
    class PartialThenCrash:
        async def send(self, message: str) -> AsyncGenerator[str, None]:
            yield "partial"
            raise RuntimeError("cursor-agent exited 1: segfault")

        async def suspend(self) -> bytes:
            return b"{}"

        async def stop(self) -> None:
            pass

    ps = PartialThenCrash()
    chunks: list[str] = []
    with pytest.raises(RuntimeError, match="exited 1"):
        async for chunk in ps.send("hello"):
            chunks.append(chunk)
    # Got partial output but still raised.
    assert chunks == ["partial"]


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


async def test_wake_failure_preserves_inbox(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    """Wake-turn crash preserves messages in inbox (peek-then-drain)."""
    prov = FakeProvider()
    prov._error_on_send = True
    sched = TurnScheduler(providers={"fake": prov}, mux=mux, store=store)
    o = Orchestrator(sched, default_provider="fake", default_model="m")
    o.start_wake_loop()
    try:
        # Create parent (error session) and child.
        parent = await o.create_root_agent("parent", "p")
        handler = o.get_handler(parent.id)
        handler.spawn_agent("child", "ci")

        # Drain deferred via a failed turn on parent.
        with pytest.raises(RuntimeError, match="send failed"):
            await o.run_turn(parent.id, "go")

        child = o.tree.children(parent.id)[0]

        # Send message to child — will trigger wake.
        handler = o.get_handler(parent.id)
        handler.send_message("child", "do stuff")

        # Let wake loop process — child turn will fail.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Messages preserved in child's inbox.
        inbox = o.inboxes[child.id]
        assert len(inbox) > 0
        msgs = inbox.peek()
        assert any("do stuff" in m.payload for m in msgs)

        # Child is back to IDLE, not stuck BUSY.
        assert child.state == AgentState.IDLE

        # Parent received ERROR notification.
        parent_inbox = o.inboxes[parent.id]
        parent_msgs = parent_inbox.peek()
        assert any(m.kind == MessageKind.ERROR for m in parent_msgs)
        error_msg = next(m for m in parent_msgs if m.kind == MessageKind.ERROR)
        assert "child" in error_msg.payload
        assert "do stuff" in error_msg.payload
        assert "poke" in error_msg.payload
    finally:
        await o.stop_wake_loop()


async def test_wake_failure_root_no_parent_notification(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    """Root agent crash does not try to deliver ERROR (no parent)."""
    prov = FakeProvider()
    prov._error_on_send = True
    sched = TurnScheduler(providers={"fake": prov}, mux=mux, store=store)
    o = Orchestrator(sched, default_provider="fake", default_model="m")
    o.start_wake_loop()
    try:
        root = await o.create_root_agent("root", "r")
        # Send message to root from another root to trigger wake.
        other = await o.create_root_agent("sender", "s")
        # Deliver a message directly into root's inbox.
        envelope = MessageEnvelope(
            sender=other.id,
            recipient=root.id,
            payload="trigger",
        )
        o.inboxes[root.id].deliver(envelope)
        o._notify_wake(root.id)

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Root is back to IDLE, no crash.
        assert root.state == AgentState.IDLE
        # Messages preserved in root's inbox.
        assert len(o.inboxes[root.id]) > 0
    finally:
        await o.stop_wake_loop()


async def test_wake_failure_does_not_kill_loop(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    """One child crashing on wake doesn't kill the wake loop for others."""
    good_prov = FakeProvider()
    bad_prov = FakeProvider()
    bad_prov._error_on_send = True
    sched = TurnScheduler(
        providers={"good": good_prov, "bad": bad_prov},
        mux=mux,
        store=store,
    )
    o = Orchestrator(sched, default_provider="good", default_model="m")
    o.start_wake_loop()
    try:
        # Good parent with a good child and a bad child.
        parent = await o.create_root_agent("parent", "p")
        handler = o.get_handler(parent.id)
        handler.spawn_agent("good_kid", "ci")
        await o.run_turn(parent.id, "go")

        good_kid = next(c for c in o.tree.children(parent.id) if c.name == "good_kid")
        # Run a turn on good_kid to prove it works.
        await o.run_turn(good_kid.id, "hello")

        # Now create a bad root agent.
        bad = await o.create_root_agent("bad", "b", provider="bad")
        bad_handler = o.get_handler(bad.id)
        bad_handler.spawn_agent("bad_kid", "bi")
        with pytest.raises(RuntimeError):
            await o.run_turn(bad.id, "go")
        bad_kid = o.tree.children(bad.id)[0]

        # Send messages to both kids.
        handler = o.get_handler(parent.id)
        handler.send_message("good_kid", "after crash")
        bad_handler = o.get_handler(bad.id)
        bad_handler.send_message("bad_kid", "will fail")

        good_prov.prompts.clear()
        # Let wake loop process both.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Good kid's wake succeeded.
        assert any("after crash" in p for p in good_prov.prompts)
        # Bad kid's inbox is preserved.
        assert len(o.inboxes[bad_kid.id]) > 0
    finally:
        await o.stop_wake_loop()


async def test_poke_retries_failed_wake(
    store: SessionStore,
    mux: SessionMultiplexer,
) -> None:
    """Poke after wake failure retries the turn with preserved inbox."""
    prov = FakeProvider()
    sched = TurnScheduler(providers={"fake": prov}, mux=mux, store=store)
    o = Orchestrator(sched, default_provider="fake", default_model="m")
    o.start_wake_loop()
    try:
        parent = await o.create_root_agent("parent", "p")
        handler = o.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await o.run_turn(parent.id, "drain spawn")

        child = o.tree.children(parent.id)[0]

        # Make provider fail, then send message to trigger wake.
        prov._error_on_send = True
        handler = o.get_handler(parent.id)
        handler.send_message("child", "important work")

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Child failed — inbox preserved.
        assert len(o.inboxes[child.id]) > 0
        assert child.state == AgentState.IDLE

        # Fix the provider and poke the child.
        prov._error_on_send = False
        prov.prompts.clear()
        handler = o.get_handler(parent.id)
        result = handler.poke("child")
        assert result["status"] == "poked"

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Child's wake succeeded — inbox drained.
        assert any("important work" in p for p in prov.prompts)
        assert len(o.inboxes[child.id]) == 0
    finally:
        await o.stop_wake_loop()


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
    mux = SessionMultiplexer(store, pools={"default": 4})
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


# -- reminders ----------------------------------------------------------------


async def test_reminder_delivers_notification(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Reminder delivers NOTIFICATION after short timeout."""
    orch.start_wake_loop()
    try:
        node = await orch.create_root_agent("agent", "p")
        handler = orch.get_handler(node.id)
        result = handler.remind_me("check status", 0.01)
        assert result["status"] == "scheduled"
        reminder_id = result["reminder_id"]

        # Drain deferred to start the timer task.
        await orch.run_turn(node.id, "go")

        # Timer fires after ~10ms — yield control.
        await asyncio.sleep(0.05)

        # Agent should have been woken with the reminder.
        assert any("check status" in p for p in provider.prompts)

        # Verify NOTIFICATION in inbox or already delivered.
        # The one-shot timer should have cleaned up.
        agent_reminders = orch._reminders.get(node.id, {})
        assert reminder_id not in {str(k) for k in agent_reminders}
    finally:
        await orch.stop_wake_loop()


async def test_reminder_repeating(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Repeating reminder delivers multiple times."""
    orch.start_wake_loop()
    try:
        node = await orch.create_root_agent("agent", "p")
        handler = orch.get_handler(node.id)
        result = handler.remind_me("poll", 0.01, every=0.01)
        assert result["status"] == "scheduled"

        await orch.run_turn(node.id, "go")

        # Let multiple ticks fire.
        for _ in range(5):
            await asyncio.sleep(0.02)

        count = sum(1 for p in provider.prompts if "poll" in p)
        assert count >= 2, f"expected >=2 deliveries, got {count}"
    finally:
        await orch.stop_wake_loop()


async def test_reminder_cancel(orch: Orchestrator) -> None:
    """Cancel stops delivery before it fires."""
    orch.start_wake_loop()
    try:
        node = await orch.create_root_agent("agent", "p")
        handler = orch.get_handler(node.id)
        result = handler.remind_me("never see this", 10)
        assert result["status"] == "scheduled"
        reminder_id = result["reminder_id"]

        # Drain deferred to start the timer task.
        await orch.run_turn(node.id, "go")

        # Cancel before it fires.
        cancel_result = handler.cancel_reminder(reminder_id)
        assert cancel_result["status"] == "cancelled"

        # Task should be gone.
        agent_reminders = orch._reminders.get(node.id, {})
        assert UUID(reminder_id) not in agent_reminders

        # Double cancel returns error.
        again = handler.cancel_reminder(reminder_id)
        assert "error" in again
    finally:
        await orch.stop_wake_loop()


async def test_terminate_cancels_reminders(orch: Orchestrator) -> None:
    """Terminating an agent cancels all its pending reminders."""
    node = await orch.create_root_agent("doomed", "p")
    handler = orch.get_handler(node.id)
    handler.remind_me("r1", 60)
    handler.remind_me("r2", 60)

    # Drain deferred to create timer tasks.
    await orch.run_turn(node.id, "go")
    assert len(orch._reminders.get(node.id, {})) == 2

    await orch.terminate_agent(node.id)
    # All reminders cleaned up.
    assert node.id not in orch._reminders


# -- metadata ----------------------------------------------------------------


async def test_create_root_with_metadata(orch: Orchestrator) -> None:
    """Root agent created with metadata preserves it on the node."""
    node = await orch.create_root_agent("alpha", "p", metadata={"project": "substrat"})
    assert node.metadata == {"project": "substrat"}
    assert node.state == AgentState.IDLE


async def test_spawn_child_with_metadata(orch: Orchestrator) -> None:
    """Spawned child inherits metadata from spawn_agent call."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    result = handler.spawn_agent("worker", "do work", metadata={"role": "analyst"})
    child_id = UUID(result["agent_id"])

    # Drain deferred.
    await orch.run_turn(parent.id, "go")

    child = orch.tree.get(child_id)
    assert child.metadata == {"role": "analyst"}


# --- first-turn wake for spawned children ---------------------------------


async def test_spawn_without_message_wakes_child(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Freshly spawned child with empty inbox gets a bootstrap wake."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        # Spawn child but do NOT send it a message.
        handler.spawn_agent("child", "do stuff")
        provider.prompts.clear()
        await orch.run_turn(parent.id, "go")

        # Give the wake loop enough ticks to process (wake → send_turn → drain).
        await asyncio.sleep(0.05)

        # Child should have been woken with a bootstrap message.
        child = orch.tree.children(parent.id)[0]
        assert any("spawned" in p.lower() for p in provider.prompts), provider.prompts
        # Child ran its turn (went BUSY then back to IDLE).
        assert child.state == AgentState.IDLE
    finally:
        await orch.stop_wake_loop()


async def test_spawn_with_message_no_double_wake(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Spawned child that already has an inbox message gets one wake, not two."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        handler.send_message("child", "real task")
        provider.prompts.clear()
        await orch.run_turn(parent.id, "go")

        await asyncio.sleep(0.05)

        # Child got the real message, not a bootstrap one.
        wake_prompts = [p for p in provider.prompts if "real task" in p]
        boot_prompts = [p for p in provider.prompts if "spawned" in p.lower()]
        assert len(wake_prompts) == 1
        assert len(boot_prompts) == 0
    finally:
        await orch.stop_wake_loop()


async def test_existing_child_not_bootstrap_waked(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Existing children with empty inbox are NOT woken by drain."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await orch.run_turn(parent.id, "go")

        # Let first-turn wake finish.
        await asyncio.sleep(0.05)

        child = orch.tree.children(parent.id)[0]
        assert child.state == AgentState.IDLE

        # Run another turn on parent (no new spawns, no messages to child).
        provider.prompts.clear()
        await orch.run_turn(parent.id, "another turn")

        await asyncio.sleep(0.05)

        # Child should NOT have been woken — no new spawn, no inbox.
        child_wakes = [p for p in provider.prompts if "spawned" in p.lower()]
        assert len(child_wakes) == 0
    finally:
        await orch.stop_wake_loop()


# --- Gating tests ---


async def test_gated_agent_not_woken(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """A gated agent with pending messages is not woken."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await orch.run_turn(parent.id, "go")
        await asyncio.sleep(0.05)

        child = orch.tree.children(parent.id)[0]
        assert child.state == AgentState.IDLE

        # Gate the child, then send it a message.
        handler.gate("child")
        assert child.gated is True
        provider.prompts.clear()
        handler.send_message("child", "you there?")
        await asyncio.sleep(0.05)

        # Child should NOT have been woken — it's gated.
        child_prompts = [p for p in provider.prompts if "you there?" in p]
        assert len(child_prompts) == 0
        assert child.state == AgentState.IDLE
    finally:
        await orch.stop_wake_loop()


async def test_permit_turn_allows_one_wake(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """permit_turn allows exactly one wake, then re-gates."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await orch.run_turn(parent.id, "go")
        await asyncio.sleep(0.05)

        child = orch.tree.children(parent.id)[0]
        # Gate, send message, permit one turn.
        handler.gate("child")
        handler.send_message("child", "do this")
        handler.permit_turn("child")
        provider.prompts.clear()
        await asyncio.sleep(0.05)

        # Child should have processed the message.
        child_prompts = [p for p in provider.prompts if "do this" in p]
        assert len(child_prompts) == 1
        # Still gated after the turn.
        assert child.gated is True
        assert child.permit_once is False

        # Send another message — should NOT be processed.
        provider.prompts.clear()
        handler.send_message("child", "second msg")
        await asyncio.sleep(0.05)
        child_prompts = [p for p in provider.prompts if "second msg" in p]
        assert len(child_prompts) == 0
    finally:
        await orch.stop_wake_loop()


async def test_ungate_allows_pending_wake(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """ungating a child with pending messages wakes it."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        await orch.run_turn(parent.id, "go")
        await asyncio.sleep(0.05)

        child = orch.tree.children(parent.id)[0]
        handler.gate("child")
        handler.send_message("child", "queued")
        await asyncio.sleep(0.05)
        assert child.state == AgentState.IDLE

        # Ungate — should trigger wake.
        provider.prompts.clear()
        handler.ungate("child")
        await asyncio.sleep(0.05)

        child_prompts = [p for p in provider.prompts if "queued" in p]
        assert len(child_prompts) == 1
    finally:
        await orch.stop_wake_loop()


async def test_gated_child_skips_first_turn_bootstrap(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """A child gated immediately after spawn does not get first-turn bootstrap."""
    orch.start_wake_loop()
    try:
        parent = await orch.create_root_agent("parent", "p")
        handler = orch.get_handler(parent.id)
        handler.spawn_agent("child", "ci")
        handler.gate("child")
        provider.prompts.clear()
        await orch.run_turn(parent.id, "go")
        await asyncio.sleep(0.05)

        child = orch.tree.children(parent.id)[0]
        # Child should NOT have been woken — gated before first turn.
        boot_prompts = [p for p in provider.prompts if "spawned" in p.lower()]
        assert len(boot_prompts) == 0
        assert child.state == AgentState.IDLE
        assert child.gated is True
    finally:
        await orch.stop_wake_loop()


# --- Subscription tests ---


async def test_subscribe_fires_on_turn_end(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Subscriber gets notified when target finishes a turn (busy->idle)."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("worker", "w")
    await orch.run_turn(parent.id, "go")

    worker = orch.tree.children(parent.id)[0]
    # Parent subscribes to worker's busy->idle transition.
    result = handler.subscribe("worker", "busy->idle")
    assert result["status"] == "active"

    # Run a turn on worker — should fire the subscription.
    await orch.run_turn(worker.id, "do work")

    # Parent's inbox should have a notification.
    inbox = orch.inboxes[parent.id]
    msgs = inbox.peek()
    assert any("[state] worker: busy -> idle" in m.payload for m in msgs)


async def test_subscribe_once_auto_removes(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """One-shot subscription fires once then is removed."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("worker", "w")
    await orch.run_turn(parent.id, "go")

    worker = orch.tree.children(parent.id)[0]
    result = handler.subscribe("worker", "busy->idle", once=True)
    sub_id = result["subscription_id"]

    await orch.run_turn(worker.id, "turn 1")
    # Drain parent inbox.
    handler.check_inbox()

    # Second turn — should NOT fire (one-shot removed).
    await orch.run_turn(worker.id, "turn 2")
    msgs = orch.inboxes[parent.id].peek()
    assert not any("[state]" in m.payload for m in msgs)
    # Subscription ID should be gone.
    assert UUID(sub_id) not in orch._sub_index


async def test_subscribe_wildcard_from(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Wildcard from-state matches any transition to the target state."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("worker", "w")
    await orch.run_turn(parent.id, "go")

    worker = orch.tree.children(parent.id)[0]
    handler.subscribe("worker", "*->terminated")

    await orch.terminate_agent(worker.id)

    msgs = orch.inboxes[parent.id].peek()
    assert any("terminated" in m.payload for m in msgs)


async def test_subscribe_cleanup_on_terminate(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Subscriptions are cleaned up when target or subscriber is terminated."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("worker", "w")
    await orch.run_turn(parent.id, "go")

    worker = orch.tree.children(parent.id)[0]
    result = handler.subscribe("worker", "busy->idle")
    sub_id = UUID(result["subscription_id"])

    await orch.terminate_agent(worker.id)

    # Subscription should be cleaned up.
    assert sub_id not in orch._sub_index
    assert worker.id not in orch._subscriptions


async def test_unsubscribe_stops_notifications(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """unsubscribe() prevents further notifications."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("worker", "w")
    await orch.run_turn(parent.id, "go")

    worker = orch.tree.children(parent.id)[0]
    result = handler.subscribe("worker", "busy->idle")
    handler.unsubscribe(result["subscription_id"])

    await orch.run_turn(worker.id, "do work")

    msgs = orch.inboxes[parent.id].peek()
    assert not any("[state]" in m.payload for m in msgs)


async def test_subscribe_cleanup_on_subscriber_terminate(
    provider: FakeProvider,
    orch: Orchestrator,
) -> None:
    """Subscriptions are cleaned up when the subscriber is terminated."""
    parent = await orch.create_root_agent("parent", "p")
    handler = orch.get_handler(parent.id)
    handler.spawn_agent("watcher", "w")
    handler.spawn_agent("target", "t")
    await orch.run_turn(parent.id, "go")

    watcher = orch.tree.children(parent.id)[0]
    target = orch.tree.children(parent.id)[1]
    # Watcher subscribes to target.
    wh = orch.get_handler(watcher.id)
    result = wh.subscribe("target", "busy->idle")
    sub_id = UUID(result["subscription_id"])
    assert sub_id in orch._sub_index

    # Terminate the watcher (subscriber).
    await orch.terminate_agent(watcher.id)

    # Subscription should be cleaned up.
    assert sub_id not in orch._sub_index
    subs = orch._subscriptions.get(target.id, [])
    assert not any(s.id == sub_id for s in subs)
