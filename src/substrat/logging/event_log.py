# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Append-only JSONL event log with durable writes."""

import contextlib
import json
import os
from pathlib import Path
from types import TracebackType
from typing import Any

from substrat import now_iso
from substrat.persistence import _full_write


def _fsync_dir(dirpath: Path) -> None:
    """Fsync a directory to make its entries durable."""
    fd = os.open(dirpath, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EventLog:
    """Per-agent structured event log.

    Each entry is durable on return from log(). Uses a pending file as a
    mini write-ahead log: entry goes to .pending first (fsynced), then
    appended to the main log (fsynced), then .pending is removed. Crash
    at any point is recoverable.
    """

    def __init__(self, path: Path, context: dict[str, str] | None = None) -> None:
        self._path = path
        self._pending_path = path.with_suffix(".pending")
        self._context = context or {}
        self._fd: int | None = None

    def open(self) -> None:
        """Open the log file, replaying any pending entry from a prior crash."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._recover_pending()
        self._fd = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        # Fsync the directory so the new file's dir entry is durable.
        _fsync_dir(self._path.parent)

    def __enter__(self) -> "EventLog":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def log(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Append one event. Durable on return."""
        if self._fd is None:
            msg = "EventLog not open"
            raise RuntimeError(msg)
        line = self._serialize(event, data)
        self._write_pending(line)
        _full_write(self._fd, line)
        os.fsync(self._fd)
        self._remove_pending()

    def close(self) -> None:
        """Close the file descriptor."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _serialize(self, event: str, data: dict[str, Any] | None) -> bytes:
        entry: dict[str, Any] = {
            **self._context,
            "ts": now_iso(),
            "event": event,
        }
        if data is not None:
            entry["data"] = data
        return (json.dumps(entry, separators=(",", ":")) + "\n").encode()

    def _write_pending(self, line: bytes) -> None:
        fd = os.open(
            self._pending_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644,
        )
        try:
            _full_write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _remove_pending(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._pending_path)

    def _recover_pending(self) -> None:
        """If a .pending file exists, a prior write was interrupted.

        Append it to the main log if it's not already there.
        """
        if not self._pending_path.exists():
            return
        pending_data = self._pending_path.read_bytes()
        if not pending_data:
            self._remove_pending()
            return
        # Truncate any partial trailing line from a crash mid-append.
        self._truncate_partial_tail()
        # Check if the pending entry is already the tail of the log.
        if self._path.exists():
            size = self._path.stat().st_size
            if size >= len(pending_data):
                with self._path.open("rb") as f:
                    f.seek(size - len(pending_data))
                    if f.read() == pending_data:
                        self._remove_pending()
                        return
        # Append the pending entry to the main log.
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            _full_write(fd, pending_data)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._remove_pending()

    def _truncate_partial_tail(self) -> None:
        """Remove an incomplete trailing line left by a crash mid-append."""
        if not self._path.exists():
            return
        with self._path.open("rb") as f:
            content = f.read()
        if not content:
            return
        # A well-formed log always ends with b'\n'.
        if content.endswith(b"\n"):
            return
        # Find the last newline — everything after it is garbage.
        last_nl = content.rfind(b"\n")
        truncate_to = last_nl + 1 if last_nl >= 0 else 0
        fd = os.open(self._path, os.O_WRONLY)
        try:
            os.ftruncate(fd, truncate_to)
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = ["EventLog"]
