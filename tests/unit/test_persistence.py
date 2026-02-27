# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for crash-safe file write primitives."""

from pathlib import Path

from substrat.persistence import atomic_write


def test_basic_write(tmp_path: Path) -> None:
    target = tmp_path / "file.json"
    atomic_write(target, b"hello")
    assert target.read_bytes() == b"hello"


def test_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "file.json"
    atomic_write(target, b"old")
    atomic_write(target, b"new")
    assert target.read_bytes() == b"new"


def test_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.json"
    atomic_write(target, b"deep")
    assert target.read_bytes() == b"deep"


def test_leftover_tmp_does_not_corrupt(tmp_path: Path) -> None:
    """A .tmp file from a prior crash is harmless."""
    target = tmp_path / "file.json"
    leftover = target.with_suffix(".json.tmp")
    leftover.write_bytes(b"garbage")
    atomic_write(target, b"clean")
    assert target.read_bytes() == b"clean"
    # .tmp gets overwritten by the new write, not left as garbage.
    assert not leftover.exists() or leftover.read_bytes() != b"garbage"
