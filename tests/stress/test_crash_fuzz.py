# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Crash-recovery fuzzer for persistence primitives.

Exercises atomic_write and EventLog crash recovery via a virtual filesystem
that simulates power loss at arbitrary IO boundaries. Verifies all-or-nothing
semantics for atomic writes and prefix-consistency for the WAL-based event log.

Gated behind --run-stress.

Do NOT use ``random`` in rules. All randomness must go through Hypothesis
strategies so that shrinking, replay, and ``derandomize=True`` work correctly.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from substrat.logging.event_log import EventLog, read_log
from substrat.persistence import atomic_write

from .vfs import CrashError, VirtualFS, patch_io

pytestmark = pytest.mark.stress


def _event_key(entry: dict[str, Any]) -> tuple[str, Any]:
    """Extract comparable (event, data) pair from a log entry."""
    return (entry["event"], entry.get("data"))


# -- State machine ---------------------------------------------------------


class CrashRecoveryMachine(RuleBasedStateMachine):
    """Fuzz crash recovery of atomic_write and EventLog.

    Uses VirtualFS to simulate crashes at arbitrary IO boundaries. Shadow
    state tracks what should survive each crash. Invariants verify
    consistency after every step.
    """

    def __init__(self) -> None:
        super().__init__()
        self.vfs = VirtualFS()
        self._patch_ctx = patch_io(self.vfs)
        self._patch_ctx.__enter__()

        # atomic_write shadow state.
        self.atomic_path = Path(self.vfs.root) / "data" / "target.json"
        self.atomic_content: bytes | None = None

        # EventLog shadow state.
        self.log_path = Path(self.vfs.root) / "log" / "events.jsonl"
        self.shadow_events: list[tuple[str, dict[str, Any] | None]] = []
        self._event_seq: int = 0
        self._log: EventLog | None = None
        self._log_open: bool = False

    def teardown(self) -> None:
        if self._log is not None:
            with contextlib.suppress(OSError, CrashError):
                self._log.close()
        self._patch_ctx.__exit__(None, None, None)

    def _invalidate_log(self) -> None:
        """Mark the EventLog as dead after a crash."""
        self._log = None
        self._log_open = False

    # -- Setup -------------------------------------------------------------

    @initialize()
    def setup_dirs(self) -> None:
        """Create parent directories for both test targets."""
        self.vfs.mkdir(str(self.atomic_path.parent), parents=True, exist_ok=True)
        self.vfs.mkdir(str(self.log_path.parent), parents=True, exist_ok=True)

    # -- atomic_write rules ------------------------------------------------

    @rule(data=st.binary(min_size=1, max_size=256))
    def write_atomic_ok(self, data: bytes) -> None:
        """Successful atomic write. Updates shadow."""
        atomic_write(self.atomic_path, data)
        self.atomic_content = data

    @rule(
        data=st.binary(min_size=1, max_size=256),
        crash_at=st.integers(min_value=1, max_value=20),
    )
    def crash_atomic_write(self, data: bytes, crash_at: int) -> None:
        """Arm crash counter, attempt atomic_write, verify all-or-nothing."""
        old = self.atomic_content
        self.vfs.arm(crash_at)
        try:
            atomic_write(self.atomic_path, data)
            # Completed before crash point.
            self.vfs.disarm()
            self.atomic_content = data
        except CrashError:
            self._invalidate_log()
            norm = self.vfs._norm(self.atomic_path)
            disk = self.vfs._disk.get(norm)
            assert disk in (old, data), (
                f"corrupt atomic write: disk={disk!r}, old={old!r}, new={data!r}"
            )
            # Shadow follows reality.
            self.atomic_content = disk

    # -- EventLog rules ----------------------------------------------------

    @precondition(lambda self: not self._log_open)
    @rule()
    def open_log(self) -> None:
        """Open (or reopen after crash) the EventLog."""
        self._log = EventLog(self.log_path, context={"sid": "test"})
        self._log.open()
        self._log_open = True

    @precondition(lambda self: self._log_open)
    @rule()
    def log_event_ok(self) -> None:
        """Log one event successfully. Updates shadow."""
        seq = self._event_seq
        self._event_seq += 1
        event = f"test.{seq}"
        data: dict[str, Any] = {"seq": seq}
        assert self._log is not None
        self._log.log(event, data)
        self.shadow_events.append((event, data))

    @precondition(lambda self: self._log_open)
    @rule(crash_at=st.integers(min_value=1, max_value=20))
    def crash_log_event(self, crash_at: int) -> None:
        """Arm crash counter, attempt to log, verify WAL recovery."""
        seq = self._event_seq
        event = f"test.{seq}"
        data: dict[str, Any] = {"seq": seq}

        self.vfs.arm(crash_at)
        try:
            assert self._log is not None
            self._log.log(event, data)
            # Completed before crash point.
            self.vfs.disarm()
            self._event_seq += 1
            self.shadow_events.append((event, data))
            return
        except CrashError:
            pass

        # Log is dead. Run recovery on a fresh instance.
        self._invalidate_log()
        recovery_log = EventLog(self.log_path, context={"sid": "test"})
        recovery_log.open()
        recovery_log.close()

        # Read back and verify.
        entries = read_log(self.log_path)
        recovered = [_event_key(e) for e in entries]
        expected_without = list(self.shadow_events)
        expected_with = list(self.shadow_events) + [(event, data)]

        assert recovered in (expected_without, expected_with), (
            f"bad recovery: got {len(recovered)} events, "
            f"expected {len(expected_without)} or {len(expected_with)}.\n"
            f"recovered={recovered}\n"
            f"shadow={expected_without}"
        )
        # Update shadow to match what actually survived.
        if recovered == expected_with:
            self._event_seq += 1
            self.shadow_events.append((event, data))

    @precondition(lambda self: self._log_open)
    @rule()
    def close_log(self) -> None:
        """Close the EventLog cleanly."""
        assert self._log is not None
        self._log.close()
        self._log = None
        self._log_open = False

    # -- Invariants --------------------------------------------------------

    @invariant()
    def atomic_target_consistent(self) -> None:
        """Disk content of atomic_write target matches shadow."""
        norm = self.vfs._norm(self.atomic_path)
        disk = self.vfs._disk.get(norm)
        assert disk == self.atomic_content, (
            f"atomic mismatch: disk={disk!r}, shadow={self.atomic_content!r}"
        )

    @invariant()
    def log_entries_valid_json(self) -> None:
        """Every line in the durable log is valid JSON."""
        norm = self.vfs._norm(self.log_path)
        raw = self.vfs._disk.get(norm)
        if raw is None:
            return
        for line in raw.split(b"\n"):
            if not line:
                continue
            json.loads(line)  # Raises on corrupt data.

    @invariant()
    def log_matches_shadow(self) -> None:
        """Durable log entries are a prefix of the shadow list."""
        norm = self.vfs._norm(self.log_path)
        raw = self.vfs._disk.get(norm)
        if raw is None:
            return
        entries: list[dict[str, Any]] = []
        for line in raw.split(b"\n"):
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        recovered = [_event_key(e) for e in entries]
        expected = list(self.shadow_events[: len(recovered)])
        assert recovered == expected, (
            f"log diverged from shadow: {recovered} != {expected}"
        )


# Hypothesis needs a concrete TestCase class.
TestCrashRecoveryFuzz = CrashRecoveryMachine.TestCase
TestCrashRecoveryFuzz.settings = settings(
    max_examples=200,
    stateful_step_count=50,
    deadline=None,
)
