# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Scripted provider for integration tests that exercise real UDS tool callbacks.

Each "turn script" is an async generator function receiving (agent_id_hex,
socket_path, message) and yielding response chunks.  ScriptedProvider assigns
scripts to agents in FIFO creation order — parent first, deferred-spawn
children second.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from uuid import UUID

from substrat.logging.event_log import EventLog
from substrat.model import CommandWrapper

# Turn script signature: (agent_id_hex, socket_path, message) -> chunks.
TurnFn = Callable[[str, str, str], AsyncGenerator[str, None]]


class ScriptedSession:
    """Provider session that replays scripted turn functions.

    Each send() pops the next turn function, executes it (allowing UDS
    callbacks mid-turn), and yields the resulting chunks.
    """

    def __init__(
        self,
        turns: deque[TurnFn],
        agent_id: str,
        socket_path: str,
        on_turn_complete: Callable[[], None],
    ) -> None:
        self._turns = turns
        self._agent_id = agent_id
        self._socket_path = socket_path
        self._on_turn_complete = on_turn_complete

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        turn_fn = self._turns.popleft()
        async for chunk in turn_fn(self._agent_id, self._socket_path, message):
            yield chunk
        self._on_turn_complete()

    async def suspend(self) -> bytes:
        return b"scripted"

    async def stop(self) -> None:
        pass


class ScriptedProvider:
    """Provider that pops pre-configured turn scripts per agent.

    add_agent_script() enqueues one agent's turn list.  create() pops in
    FIFO order, matching creation order (parent first, children from
    deferred spawn second).  Tracks completed_turns for polling assertions.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._agent_scripts: deque[list[TurnFn]] = deque()
        self.completed_turns: int = 0

    @property
    def name(self) -> str:
        return "scripted"

    def add_agent_script(self, turns: list[TurnFn]) -> None:
        """Enqueue turn scripts for the next agent to be created."""
        self._agent_scripts.append(turns)

    async def create(
        self,
        model: str | None,
        system_prompt: str,
        log: EventLog | None = None,
        *,
        workspace: Path | None = None,
        wrap_command: CommandWrapper | None = None,
        agent_id: UUID | None = None,
        daemon_socket: str | None = None,
    ) -> ScriptedSession:
        scripts = self._agent_scripts.popleft()
        aid_hex = agent_id.hex if agent_id is not None else ""
        return ScriptedSession(
            deque(scripts),
            aid_hex,
            self._socket_path,
            self._on_turn_complete,
        )

    def models(self) -> list[str]:
        return ["test-model"]

    async def restore(
        self,
        state: bytes,
        log: EventLog | None = None,
        *,
        wrap_command: CommandWrapper | None = None,
    ) -> ScriptedSession:
        raise NotImplementedError("scripted provider does not support restore")

    def _on_turn_complete(self) -> None:
        self.completed_turns += 1


async def poll_until(
    predicate: Callable[[], bool],
    timeout: float = 5.0,
    interval: float = 0.05,
) -> None:
    """Poll predicate until True. Raises TimeoutError on expiry."""
    elapsed = 0.0
    while not predicate():
        if elapsed >= timeout:
            raise TimeoutError(f"poll_until timed out after {timeout}s")
        await asyncio.sleep(interval)
        elapsed += interval
