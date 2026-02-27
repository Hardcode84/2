# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for CursorAgentProvider. Requires cursor-agent + network."""

import pytest

from substrat.provider.base import AgentProvider, ProviderSession
from substrat.provider.cursor_agent import CursorAgentProvider

pytestmark = pytest.mark.e2e


def test_satisfies_provider_protocol() -> None:
    assert isinstance(CursorAgentProvider(), AgentProvider)


@pytest.mark.asyncio
async def test_create_and_send() -> None:
    provider = CursorAgentProvider()
    session = await provider.create("sonnet-4.6", "")
    assert isinstance(session, ProviderSession)
    chunks = [c async for c in session.send("Say exactly: ping")]
    response = "".join(chunks)
    assert "ping" in response.lower()
    await session.stop()


@pytest.mark.asyncio
async def test_session_remembers_context() -> None:
    provider = CursorAgentProvider()
    session = await provider.create("sonnet-4.6", "")
    async for _ in session.send("Remember the word: banana"):
        pass
    chunks = [c async for c in session.send("What word did I ask you to remember?")]
    response = "".join(chunks)
    assert "banana" in response.lower()
    await session.stop()


@pytest.mark.asyncio
async def test_suspend_restore() -> None:
    provider = CursorAgentProvider()
    session = await provider.create("sonnet-4.6", "")
    async for _ in session.send("Remember the word: giraffe"):
        pass
    state = await session.suspend()
    await session.stop()
    # Restore and verify context survived.
    restored = await provider.restore(state)
    chunks = [c async for c in restored.send("What word did I ask you to remember?")]
    response = "".join(chunks)
    assert "giraffe" in response.lower()
    await restored.stop()
