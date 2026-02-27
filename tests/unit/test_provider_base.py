# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the AgentProvider / ProviderSession protocols."""

from collections.abc import AsyncGenerator

import pytest

from substrat.provider import AgentProvider, ProviderSession


class FakeSession:
    """Minimal ProviderSession for protocol compliance testing."""

    def __init__(self, prompt: str) -> None:
        self._prompt = prompt

    async def send(self, message: str) -> AsyncGenerator[str, None]:
        yield f"echo: {message}"

    async def suspend(self) -> bytes:
        return self._prompt.encode()

    async def stop(self) -> None:
        pass


class FakeProvider:
    """Minimal AgentProvider for protocol compliance testing."""

    @property
    def name(self) -> str:
        return "fake"

    async def create(self, model: str, system_prompt: str) -> FakeSession:
        return FakeSession(system_prompt)

    async def restore(self, state: bytes) -> FakeSession:
        return FakeSession(state.decode())


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(FakeProvider(), AgentProvider)


def test_fake_session_satisfies_protocol() -> None:
    assert isinstance(FakeSession("test"), ProviderSession)


def test_provider_name() -> None:
    assert FakeProvider().name == "fake"


@pytest.mark.asyncio
async def test_create_returns_session() -> None:
    provider = FakeProvider()
    session = await provider.create("gpt-4", "be helpful")
    assert isinstance(session, ProviderSession)


@pytest.mark.asyncio
async def test_send_yields_response() -> None:
    provider = FakeProvider()
    session = await provider.create("gpt-4", "be helpful")
    chunks = [c async for c in session.send("hello")]
    assert chunks == ["echo: hello"]


@pytest.mark.asyncio
async def test_suspend_restore_roundtrip() -> None:
    provider = FakeProvider()
    session = await provider.create("gpt-4", "be helpful")
    state = await session.suspend()
    assert isinstance(state, bytes)
    restored = await provider.restore(state)
    assert isinstance(restored, ProviderSession)


@pytest.mark.asyncio
async def test_full_lifecycle() -> None:
    provider = FakeProvider()
    session = await provider.create("gpt-4", "be helpful")
    chunks = [c async for c in session.send("test")]
    assert len(chunks) > 0
    state = await session.suspend()
    await session.stop()
    # Restore and continue.
    restored = await provider.restore(state)
    chunks = [c async for c in restored.send("resumed")]
    assert chunks == ["echo: resumed"]
    await restored.stop()
