# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the CLI — typer commands with mocked RPC."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from substrat.cli.app import _format_event, app
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


# -- Bug regression tests ------------------------------------------------------


def test_json_decode_error_displayed() -> None:
    """JSONDecodeError prints 'invalid response' and exits 1."""
    import json

    with patch("substrat.cli.app.sync_call") as mock:
        mock.side_effect = json.JSONDecodeError("bad", "", 0)
        result = runner.invoke(app, ["agent", "list"])
    assert result.exit_code == 1
    assert "invalid response" in result.output


def test_agent_create_with_workspace() -> None:
    """agent create passes workspace option."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"agent_id": "abc", "name": "w"}
        result = runner.invoke(
            app,
            ["agent", "create", "w", "--workspace", "my-ws"],
        )
    assert result.exit_code == 0
    call_params = mock.call_args[0][2]
    assert call_params["workspace"] == "my-ws"


# -- agent attach --------------------------------------------------------------


def test_agent_attach_streams_output() -> None:
    """attach streams chunks and prints them."""
    with (
        patch("substrat.cli.app.sync_call") as mock_call,
        patch("substrat.cli.app.sync_stream") as mock_stream,
        patch("builtins.input", side_effect=["hello", ""]),
    ):
        mock_call.return_value = {
            "name": "a",
            "state": "idle",
            "children": [],
            "inbox": [],
        }
        mock_stream.return_value = iter(["Hello ", "world"])
        result = runner.invoke(app, ["agent", "attach", "abc123"])
    assert result.exit_code == 0
    assert "Hello world" in result.output
    assert "detached" in result.output


def test_agent_attach_daemon_not_running() -> None:
    """attach exits 1 when daemon is not running."""
    with patch("substrat.cli.app.sync_call") as mock_call:
        mock_call.side_effect = ConnectionRefusedError("nope")
        result = runner.invoke(app, ["agent", "attach", "abc123"])
    assert result.exit_code == 1
    assert "daemon not running" in result.output


def test_agent_attach_rpc_error_continues() -> None:
    """attach prints RPC errors and continues the REPL loop."""
    with (
        patch("substrat.cli.app.sync_call") as mock_call,
        patch("substrat.cli.app.sync_stream") as mock_stream,
        patch("builtins.input", side_effect=["hello", ""]),
    ):
        mock_call.return_value = {
            "name": "a",
            "state": "idle",
            "children": [],
            "inbox": [],
        }
        mock_stream.side_effect = RpcError(1, "agent busy")
        result = runner.invoke(app, ["agent", "attach", "abc123"])
    assert result.exit_code == 0
    assert "agent busy" in result.output
    assert "detached" in result.output


def test_agent_attach_ctrl_d() -> None:
    """attach detaches on Ctrl-D (EOFError)."""
    with (
        patch("substrat.cli.app.sync_call") as mock_call,
        patch("builtins.input", side_effect=EOFError),
    ):
        mock_call.return_value = {
            "name": "a",
            "state": "idle",
            "children": [],
            "inbox": [],
        }
        result = runner.invoke(app, ["agent", "attach", "abc123"])
    assert result.exit_code == 0
    assert "detached" in result.output


# -- workspace commands --------------------------------------------------------


def test_workspace_create() -> None:
    """workspace create calls workspace.create and prints scope/name."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"scope": "aabbccdd", "name": "dev"}
        result = runner.invoke(app, ["workspace", "create", "dev"])
    assert result.exit_code == 0
    assert "aabbccdd/dev" in result.output


def test_workspace_create_with_scope() -> None:
    """workspace create passes scope option."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"scope": "deadbeef", "name": "env"}
        result = runner.invoke(
            app,
            ["workspace", "create", "env", "--scope", "deadbeef"],
        )
    assert result.exit_code == 0
    call_params = mock.call_args[0][2]
    assert call_params["scope"] == "deadbeef"


def test_workspace_create_with_network() -> None:
    """workspace create passes network option."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"scope": "aabb", "name": "net"}
        result = runner.invoke(
            app,
            ["workspace", "create", "net", "--network"],
        )
    assert result.exit_code == 0
    call_params = mock.call_args[0][2]
    assert call_params["network_access"] is True


def test_workspace_list_empty() -> None:
    """workspace list prints 'no workspaces' when empty."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"workspaces": []}
        result = runner.invoke(app, ["workspace", "list"])
    assert result.exit_code == 0
    assert "no workspaces" in result.output


def test_workspace_list_with_workspaces() -> None:
    """workspace list prints scope/name lines."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "workspaces": [
                {"scope": "aaa", "name": "dev", "network_access": False},
                {"scope": "bbb", "name": "prod", "network_access": True},
            ]
        }
        result = runner.invoke(app, ["workspace", "list"])
    assert result.exit_code == 0
    assert "aaa/dev" in result.output
    assert "bbb/prod" in result.output
    assert "[net]" in result.output


def test_workspace_delete() -> None:
    """workspace delete calls workspace.delete and prints confirmation."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"status": "deleted", "scope": "aaa", "name": "dev"}
        result = runner.invoke(app, ["workspace", "delete", "dev", "aaa"])
    assert result.exit_code == 0
    assert "deleted aaa/dev" in result.output


# -- Bug regression tests ------------------------------------------------------


# -- workspace link ------------------------------------------------------------


def test_workspace_link() -> None:
    """workspace link calls workspace.link and prints confirmation."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"status": "linked", "scope": "aaa", "name": "dev"}
        result = runner.invoke(
            app,
            [
                "workspace",
                "link",
                "dev",
                "aaa",
                "--source",
                "/opt/data",
                "--target",
                "/mnt/data",
            ],
        )
    assert result.exit_code == 0
    assert "linked aaa/dev" in result.output
    assert "/opt/data -> /mnt/data" in result.output
    assert "(ro)" in result.output


def test_workspace_link_rw_mode() -> None:
    """workspace link passes mode option."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"status": "linked", "scope": "bbb", "name": "ws"}
        result = runner.invoke(
            app,
            [
                "workspace",
                "link",
                "ws",
                "bbb",
                "--source",
                "/src",
                "--target",
                "/src",
                "--mode",
                "rw",
            ],
        )
    assert result.exit_code == 0
    assert "(rw)" in result.output
    call_params = mock.call_args[0][2]
    assert call_params["mode"] == "rw"


# -- workspace unlink ----------------------------------------------------------


def test_workspace_unlink() -> None:
    """workspace unlink calls workspace.unlink and prints confirmation."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"status": "unlinked", "scope": "aaa", "name": "dev"}
        result = runner.invoke(
            app,
            ["workspace", "unlink", "dev", "aaa", "--target", "/mnt/data"],
        )
    assert result.exit_code == 0
    assert "unlinked /mnt/data from aaa/dev" in result.output


# -- workspace view ------------------------------------------------------------


def test_workspace_view() -> None:
    """workspace view creates workspace and links source root."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.side_effect = [
            # workspace.inspect on source.
            {"name": "src", "scope": "aaa", "root_path": "/ws/root", "links": []},
            # workspace.create for the view.
            {"scope": "bbb", "name": "my-view"},
            # workspace.link to bind source into view.
            {"status": "linked", "scope": "bbb", "name": "my-view"},
        ]
        result = runner.invoke(
            app,
            [
                "workspace",
                "view",
                "src",
                "aaa",
                "--name",
                "my-view",
            ],
        )
    assert result.exit_code == 0
    assert "bbb/my-view" in result.output
    # Verify the three RPC calls.
    assert mock.call_count == 3


def test_workspace_view_with_subdir() -> None:
    """workspace view --subdir appends to source root_path."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.side_effect = [
            {"name": "src", "scope": "aaa", "root_path": "/ws/root", "links": []},
            {"scope": "ccc", "name": "sub-view"},
            {"status": "linked", "scope": "ccc", "name": "sub-view"},
        ]
        result = runner.invoke(
            app,
            [
                "workspace",
                "view",
                "src",
                "aaa",
                "--name",
                "sub-view",
                "--subdir",
                "data/stuff",
            ],
        )
    assert result.exit_code == 0
    # Third call is workspace.link — check host_path includes subdir.
    link_params = mock.call_args_list[2][0][2]
    assert link_params["host_path"] == "/ws/root/data/stuff"


def test_workspace_view_with_scope_and_mode() -> None:
    """workspace view passes explicit scope and mode."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.side_effect = [
            {"name": "src", "scope": "aaa", "root_path": "/r", "links": []},
            {"scope": "ddd", "name": "v"},
            {"status": "linked", "scope": "ddd", "name": "v"},
        ]
        result = runner.invoke(
            app,
            [
                "workspace",
                "view",
                "src",
                "aaa",
                "--name",
                "v",
                "--scope",
                "ddd",
                "--mode",
                "rw",
            ],
        )
    assert result.exit_code == 0
    create_params = mock.call_args_list[1][0][2]
    assert create_params["scope"] == "ddd"
    link_params = mock.call_args_list[2][0][2]
    assert link_params["mode"] == "rw"


# -- workspace inspect ---------------------------------------------------------


def test_workspace_inspect() -> None:
    """workspace inspect prints workspace details."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "name": "dev",
            "scope": "aabbcc",
            "root_path": "/ws/dev/root",
            "network_access": True,
            "created_at": "2026-01-01T00:00:00Z",
            "links": [],
        }
        result = runner.invoke(app, ["workspace", "inspect", "dev", "aabbcc"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "aabbcc" in result.output
    assert "/ws/dev/root" in result.output
    assert "True" in result.output
    assert "2026-01-01" in result.output


def test_workspace_inspect_with_links() -> None:
    """workspace inspect prints link table."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "name": "lk",
            "scope": "fff",
            "root_path": "/r",
            "network_access": False,
            "created_at": "2026-03-03T00:00:00Z",
            "links": [
                {"host_path": "/a", "mount_path": "/b", "mode": "ro"},
                {"host_path": "/c", "mount_path": "/d", "mode": "rw"},
            ],
        }
        result = runner.invoke(app, ["workspace", "inspect", "lk", "fff"])
    assert result.exit_code == 0
    assert "links:" in result.output
    assert "/a -> /b (ro)" in result.output
    assert "/c -> /d (rw)" in result.output


# -- Bug regression tests ------------------------------------------------------


# -- daemon watch / _format_event ----------------------------------------------


def test_format_event_basic() -> None:
    """_format_event produces HH:MM:SS sid_short event details."""
    entry = {
        "session_id": "a3f8bcd1234567890",
        "ts": "2026-03-04T14:02:31.123456+00:00",
        "event": "session.created",
        "data": {"provider": "cursor-agent", "model": "claude-sonnet-4-5-20250514"},
    }
    line = _format_event(entry)
    assert line.startswith("14:02:31")
    assert "a3f8" in line
    assert "session.created" in line
    assert "provider=cursor-agent" in line
    assert "model=claude-sonnet-4-5-20250514" in line


def test_format_event_no_data() -> None:
    """_format_event works without a data field."""
    entry = {
        "session_id": "deadbeef",
        "ts": "2026-01-01T00:00:00+00:00",
        "event": "bare.event",
    }
    line = _format_event(entry)
    assert "00:00:00" in line
    assert "dead" in line
    assert "bare.event" in line


def test_format_event_truncates_long_values() -> None:
    """Long data values are truncated."""
    entry = {
        "session_id": "abcd",
        "ts": "2026-01-01T12:00:00+00:00",
        "event": "turn.complete",
        "data": {"response": "x" * 200},
    }
    line = _format_event(entry)
    assert "..." in line
    # Should not contain the full 200-char string.
    assert "x" * 200 not in line


def test_format_event_missing_fields() -> None:
    """Handles missing ts and session_id gracefully."""
    entry = {"event": "mystery"}
    line = _format_event(entry)
    assert "????" in line
    assert "mystery" in line


# -- Bug regression tests ------------------------------------------------------


def test_daemon_start_permission_error(tmp_path: Path) -> None:
    """PID owned by another user — PermissionError means 'already running'."""
    from unittest.mock import patch as _patch

    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("12345")
    with _patch("substrat.cli.app.os.kill", side_effect=PermissionError("nope")):
        result = runner.invoke(app, ["daemon", "start", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "already running" in result.output


# -- CLI edge cases (formerly missing coverage) --------------------------------


def test_daemon_start_socket_timeout_warning(tmp_path: Path) -> None:
    """daemon start warns when socket never appears."""
    with (
        patch("substrat.cli.app.subprocess.Popen"),
        patch("substrat.cli.app.time.sleep"),
    ):
        # No socket created — loop times out.
        result = runner.invoke(app, ["daemon", "start", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "may not have started" in result.output


def test_daemon_status_stale_pid(tmp_path: Path) -> None:
    """daemon status with stale PID file prints 'stopped'."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("99999999")  # Almost certainly dead.
    result = runner.invoke(app, ["daemon", "status", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "stopped" in result.output


def test_daemon_status_live_pid_no_socket(tmp_path: Path) -> None:
    """daemon status with live PID but missing socket says 'socket missing'."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()))
    # No socket file.
    result = runner.invoke(app, ["daemon", "status", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "socket missing" in result.output


def test_agent_list_shows_parent() -> None:
    """agent list displays parent_id for child agents."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "agents": [
                {
                    "agent_id": "aaa",
                    "name": "parent",
                    "state": "idle",
                    "parent_id": None,
                },
                {
                    "agent_id": "bbb",
                    "name": "child",
                    "state": "idle",
                    "parent_id": "aaa",
                },
            ]
        }
        result = runner.invoke(app, ["agent", "list"])
    assert result.exit_code == 0
    assert "parent=aaa" in result.output


def test_agent_inspect_shows_inbox() -> None:
    """agent inspect displays inbox messages."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "name": "bob",
            "state": "waiting",
            "children": [],
            "inbox": [
                {
                    "from": "aaa111",
                    "text": "hello from alice, how are you doing today?",
                },
            ],
        }
        result = runner.invoke(app, ["agent", "inspect", "bbb222"])
    assert result.exit_code == 0
    assert "inbox:" in result.output
    assert "from=aaa111" in result.output
    assert "hello from alice" in result.output


# -- inbox command -------------------------------------------------------------


def test_inbox_no_messages() -> None:
    """inbox with empty inbox prints 'no messages'."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {"messages": []}
        result = runner.invoke(app, ["inbox"])
    assert result.exit_code == 0
    assert "no messages" in result.output


def test_inbox_with_messages() -> None:
    """inbox prints messages from agents."""
    with patch("substrat.cli.app.sync_call") as mock:
        mock.return_value = {
            "messages": [
                {
                    "from": "root",
                    "text": "feature X done",
                    "message_id": "aaa",
                    "timestamp": "2026-01-01T12:34:56Z",
                },
            ]
        }
        result = runner.invoke(app, ["inbox"])
    assert result.exit_code == 0
    assert "root" in result.output
    assert "feature X done" in result.output
    assert "12:34:56" in result.output


# -- daemon start popen --------------------------------------------------------


def test_daemon_start_popen_args(tmp_path: Path) -> None:
    """daemon start passes correct arguments to Popen."""
    sock = tmp_path / "daemon.sock"
    sock.touch()
    with patch("substrat.cli.app.subprocess.Popen") as mock_popen:
        runner.invoke(
            app,
            [
                "daemon",
                "start",
                "--root",
                str(tmp_path),
                "--model",
                "gpt-5",
                "--max-slots",
                "8",
            ],
        )
    cmd = mock_popen.call_args[0][0]
    assert "--model" in cmd
    assert "gpt-5" in cmd
    assert "--max-slots" in cmd
    assert "8" in cmd
    assert "--root" in cmd
