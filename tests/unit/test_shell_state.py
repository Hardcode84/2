# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for shell state persistence across bwrap calls."""

import os
import subprocess
from pathlib import Path

from substrat.workspace.shell_state import WRAPPER_SCRIPT, ensure_wrapper, wrap_command

# --- ensure_wrapper ---


def test_ensure_wrapper_creates(tmp_path: Path) -> None:
    ensure_wrapper(tmp_path)
    script = tmp_path / ".substrat" / "wrap.sh"
    assert script.exists()
    assert script.read_text() == WRAPPER_SCRIPT
    assert os.access(script, os.X_OK)


def test_ensure_wrapper_idempotent(tmp_path: Path) -> None:
    ensure_wrapper(tmp_path)
    script = tmp_path / ".substrat" / "wrap.sh"
    mtime_before = script.stat().st_mtime_ns
    ensure_wrapper(tmp_path)
    assert script.stat().st_mtime_ns == mtime_before


def test_ensure_wrapper_overwrites_outdated(tmp_path: Path) -> None:
    dest = tmp_path / ".substrat" / "wrap.sh"
    dest.parent.mkdir(parents=True)
    dest.write_text("old junk")
    ensure_wrapper(tmp_path)
    assert dest.read_text() == WRAPPER_SCRIPT


# --- wrap_command ---


def test_wrap_command_basic() -> None:
    result = wrap_command(["python", "-c", "print(1)"])
    assert result == ["bash", ".substrat/wrap.sh", "python -c 'print(1)'"]


def test_wrap_command_single() -> None:
    assert wrap_command(["ls"]) == ["bash", ".substrat/wrap.sh", "ls"]


def test_wrap_command_shell_snippet() -> None:
    result = wrap_command(["echo", "hello world"])
    assert result == ["bash", ".substrat/wrap.sh", "echo 'hello world'"]


# --- bash integration ---


def _run_in_wrapper(ws_root: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a shell snippet through the wrapper script inside ws_root."""
    return subprocess.run(
        ["bash", str(ws_root / ".substrat" / "wrap.sh"), snippet],
        capture_output=True,
        text=True,
        cwd=str(ws_root),
        timeout=10,
    )


def test_env_persists_across_calls(tmp_path: Path) -> None:
    """Env var set by first call is visible in second call."""
    ensure_wrapper(tmp_path)

    r1 = _run_in_wrapper(tmp_path, "export MY_VAR=hello")
    assert r1.returncode == 0

    r2 = _run_in_wrapper(tmp_path, 'echo "$MY_VAR"')
    assert r2.returncode == 0
    assert r2.stdout.strip() == "hello"


def test_cwd_persists_across_calls(tmp_path: Path) -> None:
    """Working directory change survives to the next call."""
    ensure_wrapper(tmp_path)
    subdir = tmp_path / "deep" / "nested"
    subdir.mkdir(parents=True)

    r1 = _run_in_wrapper(tmp_path, f"cd {subdir}")
    assert r1.returncode == 0

    r2 = _run_in_wrapper(tmp_path, "pwd")
    assert r2.returncode == 0
    assert r2.stdout.strip() == str(subdir)


def test_exit_code_preserved(tmp_path: Path) -> None:
    """Wrapper forwards the wrapped command's exit code."""
    ensure_wrapper(tmp_path)
    r = _run_in_wrapper(tmp_path, "exit 42")
    assert r.returncode == 42


def test_env_delta_only(tmp_path: Path) -> None:
    """Only agent-set vars are persisted, not baseline vars."""
    ensure_wrapper(tmp_path)
    _run_in_wrapper(tmp_path, "export AGENT_SET=yes")

    env_file = tmp_path / ".substrat" / "env"
    content = env_file.read_text()
    assert "AGENT_SET" in content


def test_internal_vars_filtered(tmp_path: Path) -> None:
    """Wrapper's own _substrat_* vars are not persisted."""
    ensure_wrapper(tmp_path)
    _run_in_wrapper(tmp_path, "export REAL_VAR=keep")

    env_file = tmp_path / ".substrat" / "env"
    content = env_file.read_text()
    assert "_substrat_" not in content
    assert "REAL_VAR" in content


def test_path_modification_persists(tmp_path: Path) -> None:
    """PATH prepend (typical venv activation) survives across calls."""
    ensure_wrapper(tmp_path)
    fake_bin = tmp_path / "venv" / "bin"
    fake_bin.mkdir(parents=True)

    r1 = _run_in_wrapper(tmp_path, f"export PATH={fake_bin}:$PATH")
    assert r1.returncode == 0

    r2 = _run_in_wrapper(tmp_path, 'echo "$PATH"')
    assert r2.returncode == 0
    assert r2.stdout.strip().startswith(str(fake_bin))


def test_no_command_no_crash(tmp_path: Path) -> None:
    """Empty snippet doesn't crash the wrapper."""
    ensure_wrapper(tmp_path)
    r = _run_in_wrapper(tmp_path, "true")
    assert r.returncode == 0


def test_multiple_env_vars(tmp_path: Path) -> None:
    """Multiple env vars set in one call all persist."""
    ensure_wrapper(tmp_path)
    _run_in_wrapper(tmp_path, "export A=1 B=2 C=3")

    r = _run_in_wrapper(tmp_path, 'echo "$A $B $C"')
    assert r.stdout.strip() == "1 2 3"
