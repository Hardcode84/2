# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""In-memory virtual filesystem for crash-recovery fuzzing.

Two-tier model: _disk (durable, survives crash) and _cache (volatile, page
cache analogue — discarded on crash). A crash counter ticks on each IO op
and raises CrashError when it hits zero.
"""

from __future__ import annotations

import io
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from unittest.mock import patch


class CrashError(Exception):
    """Simulated power loss."""


@dataclass
class FdState:
    """Open file descriptor state."""

    path: str
    flags: int
    is_dir: bool = False


class VirtualFS:
    """In-memory filesystem with two-tier crash semantics.

    Reads see cache first, then disk (matches Linux page cache behaviour).
    crash() discards cache and fd table; disk and dirs survive.
    """

    def __init__(self, root: str = "/virtual/substrat") -> None:
        self.root = root
        self._disk: dict[str, bytes] = {}
        self._cache: dict[str, bytes] = {}
        self._dirs: set[str] = set()
        self._fd_table: dict[int, FdState] = {}
        self._all_fds: set[int] = set()  # Every fd ever allocated.
        self._next_fd: int = 1000
        self._crash_after: int | None = None
        self._op_count: int = 0
        self._frozen: bool = False

    # -- Crash counter -----------------------------------------------------

    def _tick(self) -> None:
        """Called AFTER each ticking op. Models power loss between syscalls.

        Raises RuntimeError if the VFS is frozen (post-crash). A real crash
        kills the process instantly — finally/except blocks never get to do
        IO. The freeze catches code that would silently rely on unwinding.
        """
        if self._frozen:
            msg = (
                "IO op after crash — process would be dead. "
                "A finally/except block is doing IO that would not "
                "survive a real kill -9. Call vfs.thaw() only after "
                "catching CrashError in the test."
            )
            raise RuntimeError(msg)
        self._op_count += 1
        if self._crash_after is not None:
            self._crash_after -= 1
            if self._crash_after <= 0:
                self.crash()
                raise CrashError

    def arm(self, ops: int) -> None:
        """Arm the crash counter. Crash after *ops* ticking operations."""
        self._crash_after = ops

    def disarm(self) -> None:
        """Disable the crash counter."""
        self._crash_after = None

    def crash(self) -> None:
        """Simulate power loss: discard volatile state, freeze IO."""
        self._cache.clear()
        self._fd_table.clear()
        self._crash_after = None
        self._frozen = True

    def thaw(self) -> None:
        """Unfreeze after crash. The "reboot" — allows IO for recovery."""
        self._frozen = False

    @contextmanager
    def count_ops(self) -> Iterator[list[int]]:
        """Count ticking IO ops within a block.

        Yields a 1-element list; on exit, [0] = ops performed inside.
        """
        start = self._op_count
        result = [0]
        yield result
        result[0] = self._op_count - start

    # -- Helpers -----------------------------------------------------------

    def _norm(self, path: str | Path) -> str:
        """Normalize a path string."""
        return str(PurePosixPath(path))

    def _visible(self, path: str) -> bytes | None:
        """Return visible content: cache first, then disk."""
        if path in self._cache:
            return self._cache[path]
        if path in self._disk:
            return self._disk[path]
        return None

    def _alloc_fd(self) -> int:
        fd = self._next_fd
        self._next_fd += 1
        self._all_fds.add(fd)
        return fd

    def _get_fd(self, fd: int) -> FdState:
        if fd not in self._fd_table:
            raise OSError(9, "Bad file descriptor")
        return self._fd_table[fd]

    def _ensure_parent(self, path: str) -> None:
        """Verify parent directory exists."""
        parent = str(PurePosixPath(path).parent)
        if parent != path and parent not in self._dirs:
            raise FileNotFoundError(2, "No such file or directory", path)

    # -- OS-level ops (fd-based) -------------------------------------------

    def os_open(self, path: str | Path, flags: int, mode: int = 0o644) -> int:
        """Emulate os.open(). Non-ticking."""
        p = self._norm(path)
        # Directory open.
        if p in self._dirs:
            fd = self._alloc_fd()
            self._fd_table[fd] = FdState(path=p, flags=flags, is_dir=True)
            return fd
        self._ensure_parent(p)
        exists = p in self._cache or p in self._disk
        if (flags & os.O_CREAT) and not exists:
            self._cache[p] = b""
        elif not (flags & os.O_CREAT) and not exists:
            raise FileNotFoundError(2, "No such file or directory", p)
        if flags & os.O_TRUNC:
            self._cache[p] = b""
        fd = self._alloc_fd()
        self._fd_table[fd] = FdState(path=p, flags=flags)
        return fd

    def os_close(self, fd: int) -> None:
        """Emulate os.close(). Non-ticking.

        Silently ignores fds that were cleared by crash() — this matches
        the "process died and restarted" model where stale fds are gone.
        """
        if fd in self._fd_table:
            del self._fd_table[fd]
        elif fd not in self._all_fds:
            raise OSError(9, "Bad file descriptor")

    def os_write(self, fd: int, data: bytes) -> int:
        """Emulate os.write(). Goes to page cache. Ticks."""
        fds = self._get_fd(fd)
        if fds.is_dir:
            raise OSError(21, "Is a directory")
        current = self._cache.get(fds.path, self._disk.get(fds.path, b""))
        if fds.flags & os.O_APPEND:
            self._cache[fds.path] = current + data
        else:
            self._cache[fds.path] = current + data
        self._tick()
        return len(data)

    def os_fsync(self, fd: int) -> None:
        """Emulate os.fsync(). Flushes file from cache to disk. Ticks."""
        fds = self._get_fd(fd)
        if fds.is_dir:
            # Dir fsync — no-op for file content.
            self._tick()
            return
        if fds.path in self._cache:
            self._disk[fds.path] = self._cache[fds.path]
        self._tick()

    def os_ftruncate(self, fd: int, length: int) -> None:
        """Emulate os.ftruncate(). Truncates in cache. Ticks."""
        fds = self._get_fd(fd)
        current = self._cache.get(fds.path, self._disk.get(fds.path, b""))
        self._cache[fds.path] = current[:length]
        self._tick()

    def os_replace(self, src: str | Path, dst: str | Path) -> None:
        """Emulate os.replace(). Atomic rename. Immediately durable. Ticks."""
        s, d = self._norm(src), self._norm(dst)
        # Promote src from disk to cache if needed so we can move it.
        if s not in self._disk and s not in self._cache:
            raise FileNotFoundError(2, "No such file or directory", s)
        # Compute final content: cache wins if present.
        content = self._cache.pop(s, None)
        disk_content = self._disk.pop(s, None)
        if content is None:
            content = disk_content
        elif disk_content is None:
            pass  # content already set from cache.
        # Replace is immediately durable for both tiers.
        self._disk[d] = content if content is not None else b""
        self._cache[d] = self._disk[d]
        self._tick()

    def os_unlink(self, path: str | Path) -> None:
        """Emulate os.unlink(). Immediately durable. Ticks."""
        p = self._norm(path)
        removed = False
        if p in self._disk:
            del self._disk[p]
            removed = True
        if p in self._cache:
            del self._cache[p]
            removed = True
        if not removed:
            raise FileNotFoundError(2, "No such file or directory", p)
        self._tick()

    # -- Path-level ops (for pathlib patching) -----------------------------

    def exists(self, path: str | Path) -> bool:
        p = self._norm(path)
        return p in self._cache or p in self._disk or p in self._dirs

    def read_bytes(self, path: str | Path) -> bytes:
        p = self._norm(path)
        v = self._visible(p)
        if v is None:
            raise FileNotFoundError(2, "No such file or directory", p)
        return v

    def stat_size(self, path: str | Path) -> int:
        p = self._norm(path)
        v = self._visible(p)
        if v is None:
            raise FileNotFoundError(2, "No such file or directory", p)
        return len(v)

    def open_rb(self, path: str | Path) -> BinaryIO:
        return io.BytesIO(self.read_bytes(path))

    def mkdir(
        self, path: str | Path, parents: bool = False, exist_ok: bool = False
    ) -> None:
        p = self._norm(path)
        if p in self._dirs:
            if exist_ok:
                return
            raise FileExistsError(17, "File exists", p)
        parent = str(PurePosixPath(p).parent)
        if parent != p and parent not in self._dirs:
            if not parents:
                raise FileNotFoundError(2, "No such file or directory", p)
            self.mkdir(parent, parents=True, exist_ok=True)
        self._dirs.add(p)

    def is_dir(self, path: str | Path) -> bool:
        return self._norm(path) in self._dirs

    def is_file(self, path: str | Path) -> bool:
        p = self._norm(path)
        return p in self._cache or p in self._disk

    def iterdir(self, path: str | Path) -> list[str]:
        """Return direct children (files and dirs) of path."""
        p = self._norm(path)
        if p not in self._dirs:
            raise FileNotFoundError(2, "No such file or directory", p)
        prefix = p + "/"
        children: set[str] = set()
        for k in list(self._cache) + list(self._disk):
            if k.startswith(prefix):
                rest = k[len(prefix) :]
                children.add(rest.split("/")[0])
        for d in self._dirs:
            if d.startswith(prefix):
                rest = d[len(prefix) :]
                if rest and "/" not in rest:
                    children.add(rest)
        return sorted(children)


# -- Monkey-patching -------------------------------------------------------


@contextmanager
def patch_io(vfs: VirtualFS) -> Iterator[VirtualFS]:
    """Selectively monkey-patch os.* and pathlib.Path.* for virtual paths.

    Only intercepts paths under vfs.root. Everything else passes through
    to the real OS functions.
    """
    real_os_open = os.open
    real_os_close = os.close
    real_os_write = os.write
    real_os_fsync = os.fsync
    real_os_replace = os.replace
    real_os_unlink = os.unlink
    real_os_ftruncate = os.ftruncate

    real_path_exists = Path.exists
    real_path_read_bytes = Path.read_bytes
    real_path_stat = Path.stat
    real_path_open = Path.open
    real_path_mkdir = Path.mkdir
    real_path_is_dir = Path.is_dir
    real_path_is_file = Path.is_file
    real_path_iterdir = Path.iterdir

    def _is_virtual_path(p: str | Path) -> bool:
        return str(p).startswith(vfs.root)

    def _is_virtual_fd(fd: int) -> bool:
        return fd in vfs._all_fds

    # -- os.* wrappers ----------------------------------------------------

    def patched_os_open(
        path: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any
    ) -> int:
        if _is_virtual_path(path):
            return vfs.os_open(path, flags, mode)
        return real_os_open(path, flags, mode, *args, **kwargs)

    def patched_os_close(fd: int) -> None:
        if _is_virtual_fd(fd):
            return vfs.os_close(fd)
        return real_os_close(fd)

    def patched_os_write(fd: int, data: bytes) -> int:
        if _is_virtual_fd(fd):
            return vfs.os_write(fd, data)
        return real_os_write(fd, data)

    def patched_os_fsync(fd: int) -> None:
        if _is_virtual_fd(fd):
            return vfs.os_fsync(fd)
        return real_os_fsync(fd)

    def patched_os_replace(src: Any, dst: Any) -> None:
        if _is_virtual_path(src) or _is_virtual_path(dst):
            return vfs.os_replace(src, dst)
        return real_os_replace(src, dst)

    def patched_os_unlink(path: Any) -> None:
        if _is_virtual_path(path):
            return vfs.os_unlink(path)
        return real_os_unlink(path)

    def patched_os_ftruncate(fd: int, length: int) -> None:
        if _is_virtual_fd(fd):
            return vfs.os_ftruncate(fd, length)
        return real_os_ftruncate(fd, length)

    # -- pathlib.Path wrappers --------------------------------------------

    def patched_exists(self: Path, *args: Any, **kwargs: Any) -> bool:
        if _is_virtual_path(self):
            return vfs.exists(self)
        return real_path_exists(self, *args, **kwargs)

    def patched_read_bytes(self: Path) -> bytes:
        if _is_virtual_path(self):
            return vfs.read_bytes(self)
        return real_path_read_bytes(self)

    @dataclass
    class FakeStat:
        """Minimal stat result for virtual files."""

        st_size: int
        st_mode: int = stat.S_IFREG | 0o644

    def patched_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if _is_virtual_path(self):
            return FakeStat(st_size=vfs.stat_size(self))
        return real_path_stat(self, *args, **kwargs)

    def patched_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if _is_virtual_path(self):
            if "b" in mode and "r" in mode:
                return vfs.open_rb(self)
            raise NotImplementedError(f"VFS only supports 'rb' mode, got {mode!r}")
        return real_path_open(self, mode, *args, **kwargs)

    def patched_mkdir(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if _is_virtual_path(self):
            return vfs.mkdir(self, parents=parents, exist_ok=exist_ok)
        return real_path_mkdir(self, mode, parents, exist_ok)

    def patched_is_dir(self: Path, *args: Any, **kwargs: Any) -> bool:
        if _is_virtual_path(self):
            return vfs.is_dir(self)
        return real_path_is_dir(self, *args, **kwargs)

    def patched_is_file(self: Path, *args: Any, **kwargs: Any) -> bool:
        if _is_virtual_path(self):
            return vfs.is_file(self)
        return real_path_is_file(self, *args, **kwargs)

    def patched_iterdir(self: Path) -> Iterator[Path]:
        if _is_virtual_path(self):
            for name in vfs.iterdir(self):
                yield self / name
            return
        yield from real_path_iterdir(self)

    all_patches: list[Any] = [
        patch("os.open", patched_os_open),
        patch("os.close", patched_os_close),
        patch("os.write", patched_os_write),
        patch("os.fsync", patched_os_fsync),
        patch("os.replace", patched_os_replace),
        patch("os.unlink", patched_os_unlink),
        patch("os.ftruncate", patched_os_ftruncate),
        patch.object(Path, "exists", patched_exists),
        patch.object(Path, "read_bytes", patched_read_bytes),
        patch.object(Path, "stat", patched_stat),
        patch.object(Path, "open", patched_open),
        patch.object(Path, "mkdir", patched_mkdir),
        patch.object(Path, "is_dir", patched_is_dir),
        patch.object(Path, "is_file", patched_is_file),
        patch.object(Path, "iterdir", patched_iterdir),
    ]

    for p in all_patches:
        p.start()
    try:
        yield vfs
    finally:
        for p in reversed(all_patches):
            p.stop()


__all__ = ["CrashError", "FdState", "VirtualFS", "patch_io"]
