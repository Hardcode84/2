# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the orchestrator — agent-session bridge."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest

from substrat.agent import AgentState, AgentStateError
from substrat.logging import EventLog
from substrat.orchestrator import Orchestrator
from substrat.scheduler import TurnScheduler
from substrat.session import SessionStore
from substrat.session.multiplexer import SessionMultiplexer

# -- Fakes -----------------------------------------------------------------


class FakeProviderSession:
    """Minimal provider session for testing."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self._chunks = chunks if chunks is not None else ["ok"]
        self.stopped = False

    async def send(self, message: str) -> AsyncGenerator[str, None]:
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
        self._error_on_send = False

    @property
    def name(self) -> str:
        return "fake"

    async def create(
        self,
        model: str,
        system_prompt: str,
        log: EventLog | None = None,
    ) -> FakeProviderSession:
        self.created.append((model, system_prompt))
        if self._error_on_send:
            return ErrorProviderSession()
        return FakeProviderSession(self._chunks)

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
    ) -> FakeProviderSession:
        return FakeProviderSession(self._chunks)


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
    # Alt provider was called with the override model.
    assert alt.created[-1] == ("special-m", "instructions")
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
    node.activate()  # Force BUSY.
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
    # Child session was created with same provider/model as parent.
    assert ("test-model", "child instructions") in provider.created


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
