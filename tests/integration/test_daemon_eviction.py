# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests — session eviction/restore under the daemon.

Exercises the mux LRU path: with max_slots=1, every second send triggers
a suspend→evict→restore cycle. Uses FakeProvider (no real APIs, no --run-e2e).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest

from substrat.daemon import Daemon
from substrat.rpc import async_call

# Reuse FakeProvider from unit tests.
from tests.unit.test_orchestrator import FakeProvider

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
async def daemon_env(tmp_path: Path) -> AsyncGenerator[tuple[Daemon, str], None]:
    """Start a daemon with FakeProvider and max_slots=1."""
    provider = FakeProvider()
    daemon = Daemon(
        tmp_path,
        default_provider="fake",
        default_model="test-model",
        max_slots=1,
        providers={"fake": provider},
    )
    await daemon.start()
    yield daemon, str(daemon.socket_path)
    await daemon.stop()


# -- Eviction and restore ------------------------------------------------------


async def test_eviction_and_restore(
    daemon_env: tuple[Daemon, str],
) -> None:
    """A→send, B→send (evicts A), A→send (restores A, evicts B). All succeed."""
    _daemon, sock = daemon_env

    a = await async_call(
        sock,
        "agent.create",
        {"name": "a", "instructions": "i"},
    )
    b = await async_call(
        sock,
        "agent.create",
        {"name": "b", "instructions": "i"},
    )

    # Send to A — takes the sole slot.
    r1 = await async_call(
        sock, "agent.send", {"agent_id": a["agent_id"], "message": "turn-a1"}
    )
    assert r1["response"] == "ok"

    # Send to B — A evicted (suspended), B takes slot.
    r2 = await async_call(
        sock, "agent.send", {"agent_id": b["agent_id"], "message": "turn-b1"}
    )
    assert r2["response"] == "ok"

    # Send to A again — B evicted, A restored from suspension.
    r3 = await async_call(
        sock, "agent.send", {"agent_id": a["agent_id"], "message": "turn-a2"}
    )
    assert r3["response"] == "ok"

    # Cleanup.
    await async_call(sock, "agent.terminate", {"agent_id": a["agent_id"]})
    await async_call(sock, "agent.terminate", {"agent_id": b["agent_id"]})


# -- Eviction callback fires ---------------------------------------------------


async def test_eviction_callback_fires(
    daemon_env: tuple[Daemon, str],
) -> None:
    """on_evict callback fires with the evicted session's UUID."""
    daemon, sock = daemon_env

    # Create both agents first. With max_slots=1, creating B already evicts A.
    a = await async_call(
        sock,
        "agent.create",
        {"name": "a", "instructions": "i"},
    )
    b = await async_call(
        sock,
        "agent.create",
        {"name": "b", "instructions": "i"},
    )

    # Install spy after creates so we only capture send-triggered evictions.
    mux = daemon.orchestrator._scheduler._mux  # type: ignore[attr-defined]
    original_cb = mux.on_evict
    evictions: list[tuple[UUID, int]] = []

    def spy(sid: UUID, nbytes: int) -> None:
        evictions.append((sid, nbytes))
        if original_cb is not None:
            original_cb(sid, nbytes)

    mux.on_evict = spy

    # After creates: B holds the slot (A was evicted during B's create).
    # Sending to A restores it, evicting B.
    await async_call(sock, "agent.send", {"agent_id": a["agent_id"], "message": "go"})

    assert len(evictions) == 1
    evicted_sid, nbytes = evictions[0]
    node_b = daemon.orchestrator.tree.get(UUID(b["agent_id"]))
    assert evicted_sid == node_b.session_id
    assert nbytes > 0

    # Cleanup.
    await async_call(sock, "agent.terminate", {"agent_id": a["agent_id"]})
    await async_call(sock, "agent.terminate", {"agent_id": b["agent_id"]})
