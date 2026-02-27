# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the append-only JSONL event log."""

import json
import os
from pathlib import Path

import pytest

from substrat.logging.event_log import EventLog


@pytest.fixture()
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def test_basic_append(log_path: Path) -> None:
    log = EventLog(log_path)
    log.open()
    log.log("test.event", {"key": "value"})
    log.close()
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "test.event"
    assert entry["data"]["key"] == "value"
    assert "ts" in entry


def test_context_injected(log_path: Path) -> None:
    log = EventLog(log_path, context={"agent_id": "abc-123"})
    log.open()
    log.log("test.event")
    log.close()
    entry = json.loads(log_path.read_text().strip())
    assert entry["agent_id"] == "abc-123"


def test_context_cannot_clobber_reserved_fields(log_path: Path) -> None:
    """Context keys must not overwrite ts or event."""
    log = EventLog(log_path, context={"event": "evil", "ts": "fake"})
    log.open()
    log.log("real.event")
    log.close()
    entry = json.loads(log_path.read_text().strip())
    assert entry["event"] == "real.event"
    assert entry["ts"] != "fake"


def test_multiple_entries(log_path: Path) -> None:
    log = EventLog(log_path)
    log.open()
    for i in range(5):
        log.log("event", {"i": i})
    log.close()
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 5
    for i, line in enumerate(lines):
        assert json.loads(line)["data"]["i"] == i


def test_no_data_field_when_none(log_path: Path) -> None:
    log = EventLog(log_path)
    log.open()
    log.log("bare.event")
    log.close()
    entry = json.loads(log_path.read_text().strip())
    assert "data" not in entry


def test_empty_data_dict_preserved(log_path: Path) -> None:
    """An explicit empty dict is not the same as None."""
    log = EventLog(log_path)
    log.open()
    log.log("event", {})
    log.close()
    entry = json.loads(log_path.read_text().strip())
    assert entry["data"] == {}


def test_log_not_open_raises(log_path: Path) -> None:
    log = EventLog(log_path)
    with pytest.raises(RuntimeError, match="not open"):
        log.log("boom")


def test_creates_parent_dirs(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "events.jsonl"
    log = EventLog(deep)
    log.open()
    log.log("deep.event")
    log.close()
    assert deep.exists()


def test_pending_file_removed_after_write(log_path: Path) -> None:
    log = EventLog(log_path)
    log.open()
    log.log("test.event")
    log.close()
    assert not log_path.with_suffix(".pending").exists()


def test_recovery_from_pending_file(log_path: Path) -> None:
    """Simulate crash after writing .pending but before appending to main log."""
    pending = log_path.with_suffix(".pending")
    entry = json.dumps({"ts": "t", "event": "lost"}) + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(entry)
    # Open should recover the pending entry.
    log = EventLog(log_path)
    log.open()
    log.close()
    content = log_path.read_text()
    assert '"lost"' in content
    assert not pending.exists()


def test_recovery_skips_duplicate(log_path: Path) -> None:
    """If crash happened after append but before unlinking .pending."""
    entry = json.dumps({"ts": "t", "event": "dup"}) + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(entry)
    log_path.with_suffix(".pending").write_text(entry)
    log = EventLog(log_path)
    log.open()
    log.close()
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1


def test_recovery_truncates_partial_trailing_line(log_path: Path) -> None:
    """Crash mid-append leaves a partial JSON line; recovery cleans it up."""
    good_line = json.dumps({"ts": "t", "event": "good"}) + "\n"
    pending_line = json.dumps({"ts": "t2", "event": "pending"}) + "\n"
    # Simulate: one good entry + partial garbage (no trailing newline).
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(good_line.encode() + b'{"ts":"t2","event":"pen')
    log_path.with_suffix(".pending").write_text(pending_line)
    log = EventLog(log_path)
    log.open()
    log.close()
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "good"
    assert json.loads(lines[1])["event"] == "pending"


def test_durable_after_log(log_path: Path) -> None:
    """Entry is on disk after log() returns (check via separate fd)."""
    log = EventLog(log_path)
    log.open()
    log.log("durable.event")
    # Read via a completely separate fd to bypass page cache sharing.
    fd = os.open(str(log_path), os.O_RDONLY)
    try:
        data = os.read(fd, 4096)
    finally:
        os.close(fd)
    assert b"durable.event" in data
    log.close()
