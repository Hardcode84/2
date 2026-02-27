# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Crash-safe file write primitives."""

import os
from pathlib import Path


def _full_write(fd: int, data: bytes) -> None:
    """Write all bytes, retrying on short writes."""
    while data:
        n = os.write(fd, data)
        data = data[n:]


def atomic_write(path: Path, data: bytes) -> None:
    """Write data to path atomically via temp + fsync + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        _full_write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
