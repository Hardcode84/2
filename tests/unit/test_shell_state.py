# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for shell state persistence across bwrap calls."""

import os
import subprocess
from pathlib import Path

from substrat.workspace.shell_state import (
    CAPTURE_SCRIPT,
    WRAPPER_SCRIPT,
    ensure_wrapper,
    wrap_command,
)

# --- ensure_wrapper ---


def test_ensure_wrapper_creates_both_scripts(tmp_path: Path) -> None:
    ensure_wrapper(tmp_path)
    wrap = tmp_path / ".substrat" / "wrap.sh"
    capture = tmp_path / ".substrat" / "capture_env.sh"
    assert wrap.read_text() == WRAPPER_SCRIPT
    assert capture.read_text() == CAPTURE_SCRIPT
    assert os.access(wrap, os.X_OK)
    assert os.access(capture, os.X_OK)


def test_ensure_wrapper_idempotent(tmp_path: Path) -> None:
    ensure_wrapper(tmp_path)
    wrap = tmp_path / ".substrat" / "wrap.sh"
    capture = tmp_path / ".substrat" / "capture_env.sh"
    mtime_wrap = wrap.stat().st_mtime_ns
    mtime_capture = capture.stat().st_mtime_ns
    ensure_wrapper(tmp_path)
    assert wrap.stat().st_mtime_ns == mtime_wrap
    assert capture.stat().st_mtime_ns == mtime_capture


def test_ensure_wrapper_overwrites_outdated(tmp_path: Path) -> None:
    dest_dir = tmp_path / ".substrat"
    dest_dir.mkdir(parents=True)
    (dest_dir / "wrap.sh").write_text("old junk")
    (dest_dir / "capture_env.sh").write_text("also old")
    ensure_wrapper(tmp_path)
    assert (dest_dir / "wrap.sh").read_text() == WRAPPER_SCRIPT
    assert (dest_dir / "capture_env.sh").read_text() == CAPTURE_SCRIPT


# --- wrap_command ---


def test_wrap_command_basic() -> None:
    result = wrap_command(["python", "-c", "print(1)"])
    assert result == ["bash", ".substrat/wrap.sh", "python -c 'print(1)'"]


def test_wrap_command_single() -> None:
    assert wrap_command(["ls"]) == ["bash", ".substrat/wrap.sh", "ls"]


def test_wrap_command_shell_snippet() -> None:
    result = wrap_command(["echo", "hello world"])
    assert result == ["bash", ".substrat/wrap.sh", "echo 'hello world'"]


# --- bash integration: wrapper ---


def _run_in_wrapper(ws_root: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a shell snippet through the wrapper script inside ws_root."""
    return subprocess.run(
        ["bash", str(ws_root / ".substrat" / "wrap.sh"), snippet],
        capture_output=True,
        text=True,
        cwd=str(ws_root),
        timeout=10,
    )


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


def test_no_command_no_crash(tmp_path: Path) -> None:
    """Empty snippet doesn't crash the wrapper."""
    ensure_wrapper(tmp_path)
    r = _run_in_wrapper(tmp_path, "true")
    assert r.returncode == 0


def test_wrapper_saves_baseline(tmp_path: Path) -> None:
    """Wrapper writes baseline_env for capture_env.sh to diff against."""
    ensure_wrapper(tmp_path)
    _run_in_wrapper(tmp_path, "true")
    assert (tmp_path / ".substrat" / "baseline_env").exists()


# --- bash integration: capture_env.sh ---


def _run_capture(ws_root: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet that ends with capture_env.sh through the wrapper."""
    capture = str(ws_root / ".substrat" / "capture_env.sh")
    full = f"{snippet} && {capture}"
    return _run_in_wrapper(ws_root, full)


def test_env_persists_via_capture(tmp_path: Path) -> None:
    """Env var captured explicitly is visible in the next call."""
    ensure_wrapper(tmp_path)

    r1 = _run_capture(tmp_path, "export MY_VAR=hello")
    assert r1.returncode == 0

    r2 = _run_in_wrapper(tmp_path, 'echo "$MY_VAR"')
    assert r2.returncode == 0
    assert r2.stdout.strip() == "hello"


def test_capture_writes_delta_only(tmp_path: Path) -> None:
    """Only agent-set vars end up in .substrat/env, not baseline vars."""
    ensure_wrapper(tmp_path)
    _run_capture(tmp_path, "export AGENT_SET=yes")

    env_file = tmp_path / ".substrat" / "env"
    content = env_file.read_text()
    assert "AGENT_SET" in content
    # HOME is a baseline var — should not be captured.
    assert "\nexport HOME=" not in content


def test_capture_filters_internal_vars(tmp_path: Path) -> None:
    """Capture script filters out _substrat_* vars."""
    ensure_wrapper(tmp_path)
    _run_capture(tmp_path, "export REAL_VAR=keep")

    env_file = tmp_path / ".substrat" / "env"
    content = env_file.read_text()
    assert "_substrat_" not in content
    assert "REAL_VAR" in content


def test_path_modification_persists(tmp_path: Path) -> None:
    """PATH prepend (typical venv activation) survives across calls."""
    ensure_wrapper(tmp_path)
    fake_bin = tmp_path / "venv" / "bin"
    fake_bin.mkdir(parents=True)

    r1 = _run_capture(tmp_path, f"export PATH={fake_bin}:$PATH")
    assert r1.returncode == 0

    r2 = _run_in_wrapper(tmp_path, 'echo "$PATH"')
    assert r2.returncode == 0
    assert r2.stdout.strip().startswith(str(fake_bin))


def test_multiple_env_vars(tmp_path: Path) -> None:
    """Multiple env vars set in one call all persist via capture."""
    ensure_wrapper(tmp_path)
    _run_capture(tmp_path, "export A=1 B=2 C=3")

    r = _run_in_wrapper(tmp_path, 'echo "$A $B $C"')
    assert r.stdout.strip() == "1 2 3"


def test_capture_without_baseline_fails(tmp_path: Path) -> None:
    """capture_env.sh fails if no baseline exists (not run inside wrapper)."""
    ensure_wrapper(tmp_path)
    # Run capture_env.sh directly, not through the wrapper.
    r = subprocess.run(
        ["bash", str(tmp_path / ".substrat" / "capture_env.sh")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=10,
    )
    assert r.returncode != 0
    assert "no baseline" in r.stderr


def test_env_without_capture_does_not_persist(tmp_path: Path) -> None:
    """Env vars set without calling capture_env.sh are lost."""
    ensure_wrapper(tmp_path)

    # Set var but don't capture.
    _run_in_wrapper(tmp_path, "export EPHEMERAL=gone")

    r = _run_in_wrapper(tmp_path, 'echo "${EPHEMERAL:-missing}"')
    assert r.stdout.strip() == "missing"
