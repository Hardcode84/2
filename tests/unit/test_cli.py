# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the CLI — typer commands with mocked RPC."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from substrat.cli.app import app
from substrat.rpc import RpcError

runner = CliRunner()


# -- agent create --------------------------------------------------------------


def test_agent_create() -> None:
    """agent create calls agent.create and prints result."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"agent_id": "abc123", "name": "worker"}
        result = runner.invoke(app, ["agent", "create", "worker"])
    assert result.exit_code == 0
    assert "abc123" in result.output
    assert "worker" in result.output


def test_agent_create_with_options() -> None:
    """agent create passes provider and model options."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"agent_id": "abc", "name": "w"}
        result = runner.invoke(
            app,
            [
                "agent",
                "create",
                "w",
                "--provider",
                "claude-cli",
                "--model",
                "opus",
            ],
        )
    assert result.exit_code == 0
    call_params = mock.call_args[0][2]
    assert call_params["provider"] == "claude-cli"
    assert call_params["model"] == "opus"


# -- agent list ----------------------------------------------------------------


def test_agent_list_empty() -> None:
    """agent list prints 'no agents' when empty."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"agents": []}
        result = runner.invoke(app, ["agent", "list"])
    assert result.exit_code == 0
    assert "no agents" in result.output


def test_agent_list_with_agents() -> None:
    """agent list prints agent table."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "agents": [
                {
                    "agent_id": "aaa",
                    "name": "alpha",
                    "state": "idle",
                    "parent_id": None,
                },
                {
                    "agent_id": "bbb",
                    "name": "beta",
                    "state": "busy",
                    "parent_id": "aaa",
                },
            ]
        }
        result = runner.invoke(app, ["agent", "list"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "[busy]" in result.output


# -- agent send ----------------------------------------------------------------


def test_agent_send() -> None:
    """agent send prints response."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"response": "done, boss"}
        result = runner.invoke(app, ["agent", "send", "abc123", "do the thing"])
    assert result.exit_code == 0
    assert "done, boss" in result.output


# -- agent inspect -------------------------------------------------------------


def test_agent_inspect() -> None:
    """agent inspect prints state and children."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "name": "alpha",
            "state": "idle",
            "children": [
                {"agent_id": "ccc", "name": "child1", "state": "busy"},
            ],
            "inbox": [],
        }
        result = runner.invoke(app, ["agent", "inspect", "abc123"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "idle" in result.output
    assert "child1" in result.output


# -- agent terminate -----------------------------------------------------------


def test_agent_terminate() -> None:
    """agent terminate prints confirmation."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"status": "terminated", "agent_id": "abc123"}
        result = runner.invoke(app, ["agent", "terminate", "abc123"])
    assert result.exit_code == 0
    assert "terminated" in result.output


# -- Error handling ------------------------------------------------------------


def test_rpc_error_displayed() -> None:
    """RpcError is printed to stderr and exits 1."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.side_effect = RpcError(1, "agent not found")
        result = runner.invoke(app, ["agent", "send", "bad", "msg"])
    assert result.exit_code == 1
    assert "agent not found" in result.output


def test_connection_error_displayed() -> None:
    """ConnectionError prints 'daemon not running'."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.side_effect = ConnectionRefusedError("nope")
        result = runner.invoke(app, ["agent", "list"])
    assert result.exit_code == 1
    assert "daemon not running" in result.output


# -- daemon start --------------------------------------------------------------


def test_daemon_start_spawns_subprocess(tmp_path: Path) -> None:
    """daemon start spawns a background process."""
    sock = tmp_path / "daemon.sock"
    # Pre-create socket so the wait loop succeeds immediately.
    sock.touch()
    with patch("substrat.cli.app.subprocess.Popen") as mock_popen:
        result = runner.invoke(app, ["daemon", "start", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert mock_popen.called
    assert "started" in result.output


def test_daemon_start_already_running(tmp_path: Path) -> None:
    """daemon start detects already running daemon."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()))
    result = runner.invoke(app, ["daemon", "start", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "already running" in result.output


# -- daemon stop ---------------------------------------------------------------


def test_daemon_stop_not_running(tmp_path: Path) -> None:
    """daemon stop when not running prints message."""
    result = runner.invoke(app, ["daemon", "stop", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "not running" in result.output


# -- daemon status -------------------------------------------------------------


def test_daemon_status_stopped(tmp_path: Path) -> None:
    """daemon status prints 'stopped' when no PID file."""
    result = runner.invoke(app, ["daemon", "status", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "stopped" in result.output


def test_daemon_status_running(tmp_path: Path) -> None:
    """daemon status prints 'running' when PID file points to live process."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()))
    sock = tmp_path / "daemon.sock"
    sock.touch()
    result = runner.invoke(app, ["daemon", "status", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "running" in result.output
