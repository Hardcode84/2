# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the @log_method decorator."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from substrat.logging import EventLog, log_method


class FakeSession:
    """Minimal session-like class for testing the decorator."""

    def __init__(self, log: EventLog | None = None) -> None:
        self._log = log

    @log_method(before=True, after=True)
    async def send(self, message: str) -> AsyncGenerator[str, None]:
        yield "hello "
        yield "world"

    @log_method(after=True)
    async def suspend(self) -> bytes:
        return b"state-blob"

    @log_method(after=True)
    async def stop(self) -> None:
        pass

    @log_method(before=True)
    async def create(self, workspace: Path, agent_id: UUID) -> None:
        pass

    @log_method(before=True, after=True)
    async def send_error(self, message: str) -> AsyncGenerator[str, None]:
        yield "partial"
        raise RuntimeError("boom")


@pytest.fixture()
def event_log(tmp_path: Path) -> EventLog:
    log = EventLog(tmp_path / "events.jsonl")
    log.open()
    return log


def _read_events(log: EventLog) -> list[dict[str, Any]]:
    log.close()
    lines = log._path.read_text().strip().split("\n")
    return [json.loads(line) for line in lines if line]


@pytest.mark.asyncio
async def test_send_logs_before_and_after(event_log: EventLog) -> None:
    session = FakeSession(log=event_log)
    chunks = [c async for c in session.send("hi")]
    assert chunks == ["hello ", "world"]
    events = _read_events(event_log)
    assert len(events) == 2
    assert events[0]["event"] == "send"
    assert events[0]["data"]["message"] == "hi"
    assert events[1]["event"] == "send.result"
    assert events[1]["data"]["message"] == "hi"
    assert events[1]["data"]["result"] == "hello world"


@pytest.mark.asyncio
async def test_suspend_logs_after(event_log: EventLog) -> None:
    session = FakeSession(log=event_log)
    result = await session.suspend()
    assert result == b"state-blob"
    events = _read_events(event_log)
    assert len(events) == 1
    assert events[0]["event"] == "suspend.result"
    # bytes serialized as base64.
    assert events[0]["data"]["result"] == "c3RhdGUtYmxvYg=="


@pytest.mark.asyncio
async def test_stop_logs_after(event_log: EventLog) -> None:
    session = FakeSession(log=event_log)
    await session.stop()
    events = _read_events(event_log)
    assert len(events) == 1
    assert events[0]["event"] == "stop.result"


@pytest.mark.asyncio
async def test_no_log_still_works() -> None:
    """Methods work fine without a log attached."""
    session = FakeSession(log=None)
    chunks = [c async for c in session.send("hi")]
    assert chunks == ["hello ", "world"]
    assert await session.suspend() == b"state-blob"


@pytest.mark.asyncio
async def test_error_in_generator_logs_partial(event_log: EventLog) -> None:
    session = FakeSession(log=event_log)
    with pytest.raises(RuntimeError, match="boom"):
        async for _ in session.send_error("hi"):
            pass
    events = _read_events(event_log)
    assert len(events) == 2
    assert events[0]["event"] == "send_error"
    # finally block logs whatever was yielded before the error.
    assert events[1]["event"] == "send_error.result"
    assert events[1]["data"]["message"] == "hi"
    assert events[1]["data"]["result"] == "partial"


# --- non-serializable args ------------------------------------------------


@pytest.mark.asyncio
async def test_non_serializable_args_do_not_crash(event_log: EventLog) -> None:
    """Path and UUID args are serialized to strings, not passed raw to json.dumps."""
    session = FakeSession(log=event_log)
    uid = UUID("12345678-1234-5678-1234-567812345678")
    await session.create(workspace=Path("/tmp/ws"), agent_id=uid)
    events = _read_events(event_log)
    assert len(events) == 1
    assert events[0]["event"] == "create"
    assert events[0]["data"]["workspace"] == "/tmp/ws"
    assert events[0]["data"]["agent_id"] == "12345678-1234-5678-1234-567812345678"


# --- decorator edge cases -----------------------------------------------------


class EdgeSession:
    """Extra methods for decorator edge case tests."""

    def __init__(self, log: EventLog | None = None) -> None:
        self._log = log

    @log_method(before=True, after=True)
    async def kwonly(self, *, label: str, count: int = 1) -> str:
        return f"{label}:{count}"

    @log_method(after=True)
    async def empty_gen(self, tag: str) -> AsyncGenerator[str, None]:
        return
        yield  # Make it a generator.

    @log_method(before=True, after=False)
    async def before_only(self, x: int) -> int:
        return x * 2

    @log_method(before=False, after=False)
    async def no_logging(self, x: int) -> int:
        return x + 1


@pytest.mark.asyncio
async def test_keyword_only_args(event_log: EventLog) -> None:
    """Keyword-only args and defaults are captured correctly."""
    s = EdgeSession(log=event_log)
    result = await s.kwonly(label="hi", count=3)
    assert result == "hi:3"
    events = _read_events(event_log)
    assert len(events) == 2
    assert events[0]["data"]["label"] == "hi"
    assert events[0]["data"]["count"] == 3


@pytest.mark.asyncio
async def test_default_values_omitted(event_log: EventLog) -> None:
    """Default kwarg values not passed explicitly are absent from the log."""
    s = EdgeSession(log=event_log)
    await s.kwonly(label="yo")
    events = _read_events(event_log)
    assert events[0]["data"]["label"] == "yo"
    assert "count" not in events[0]["data"]


@pytest.mark.asyncio
async def test_empty_generator(event_log: EventLog) -> None:
    """Generator that yields nothing still logs empty result."""
    s = EdgeSession(log=event_log)
    chunks = [c async for c in s.empty_gen("t")]
    assert chunks == []
    events = _read_events(event_log)
    assert len(events) == 1
    assert events[0]["data"]["result"] == ""


@pytest.mark.asyncio
async def test_before_only(event_log: EventLog) -> None:
    """before=True, after=False logs only the call, not the result."""
    s = EdgeSession(log=event_log)
    result = await s.before_only(5)
    assert result == 10
    events = _read_events(event_log)
    assert len(events) == 1
    assert events[0]["event"] == "before_only"


@pytest.mark.asyncio
async def test_no_logging(event_log: EventLog) -> None:
    """before=False, after=False produces no log entries."""
    s = EdgeSession(log=event_log)
    result = await s.no_logging(5)
    assert result == 6
    # Log should be empty — close and check.
    event_log.close()
    content = event_log._path.read_text().strip()
    assert content == ""
