# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI — thin typer client for the Substrat daemon."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import typer

from substrat.rpc import RpcError, sync_call

app = typer.Typer(name="substrat", no_args_is_help=True)
daemon_app = typer.Typer(help="Daemon lifecycle commands.")
agent_app = typer.Typer(help="Agent management commands.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(agent_app, name="agent")

_DEFAULT_ROOT = Path.home() / ".substrat"
_ROOT_OPT = typer.Option(_DEFAULT_ROOT, help="Substrat root directory.")


def _sock_path(root: Path) -> str:
    return str(root / "daemon.sock")


def _pid_path(root: Path) -> Path:
    return root / "daemon.pid"


def _call(root: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Wrap sync_call with CLI error handling."""
    try:
        return sync_call(_sock_path(root), method, params)
    except RpcError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(1) from exc
    except OSError:
        typer.echo("error: daemon not running", err=True)
        raise typer.Exit(1) from None


# -- daemon commands -----------------------------------------------------------


@daemon_app.command()
def start(
    root: Path = _ROOT_OPT,
    model: str = typer.Option("claude-sonnet-4-6", help="Default model."),
    max_slots: int = typer.Option(4, help="Max concurrent sessions."),
    foreground: bool = typer.Option(False, help="Run in foreground."),
) -> None:
    """Start the Substrat daemon."""
    if foreground:
        from substrat.daemon import Daemon

        daemon = Daemon(root, default_model=model, max_slots=max_slots)
        from substrat.daemon import _run

        asyncio.run(_run(daemon))
        return

    # Check if already running.
    pid_file = _pid_path(root)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            typer.echo(f"daemon already running (pid {pid})")
            return
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    # Spawn daemon as background process.
    cmd = [
        sys.executable,
        "-m",
        "substrat.daemon",
        "--root",
        str(root),
        "--model",
        model,
        "--max-slots",
        str(max_slots),
    ]
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for socket to appear.
    sock = root / "daemon.sock"
    for _ in range(20):
        if sock.exists():
            typer.echo("daemon started")
            return
        time.sleep(0.1)
    typer.echo("warning: daemon may not have started (socket not found)", err=True)


@daemon_app.command()
def stop(
    root: Path = _ROOT_OPT,
) -> None:
    """Stop the Substrat daemon."""
    import signal

    pid_file = _pid_path(root)
    if not pid_file.exists():
        typer.echo("daemon not running")
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError):
        typer.echo("daemon not running (stale PID file)")
        pid_file.unlink(missing_ok=True)
        return

    # Wait for socket to disappear.
    sock = root / "daemon.sock"
    for _ in range(20):
        if not sock.exists():
            typer.echo("daemon stopped")
            return
        time.sleep(0.1)
    typer.echo("warning: daemon may not have stopped", err=True)


@daemon_app.command()
def status(
    root: Path = _ROOT_OPT,
) -> None:
    """Check daemon status."""
    pid_file = _pid_path(root)
    sock = root / "daemon.sock"

    if not pid_file.exists():
        typer.echo("stopped")
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        typer.echo("stopped (stale PID file)")
        return
    if sock.exists():
        typer.echo(f"running (pid {pid})")
    else:
        typer.echo(f"running (pid {pid}, socket missing)")


# -- agent commands ------------------------------------------------------------


@agent_app.command("create")
def agent_create(
    name: str = typer.Argument(help="Agent name."),
    instructions: str = typer.Option("", help="System prompt / task description."),
    provider: str | None = typer.Option(None, help="Provider override."),
    model: str | None = typer.Option(None, help="Model override."),
    root: Path = _ROOT_OPT,
) -> None:
    """Create a root agent."""
    params: dict[str, Any] = {"name": name, "instructions": instructions}
    if provider is not None:
        params["provider"] = provider
    if model is not None:
        params["model"] = model
    result = _call(root, "agent.create", params)
    typer.echo(f"{result['agent_id']}  {result['name']}")


@agent_app.command("list")
def agent_list(
    root: Path = _ROOT_OPT,
) -> None:
    """List all agents."""
    result = _call(root, "agent.list", {})
    agents = result.get("agents", [])
    if not agents:
        typer.echo("no agents")
        return
    for a in agents:
        parent = f"  parent={a['parent_id']}" if a.get("parent_id") else ""
        typer.echo(f"{a['agent_id']}  {a['name']}  [{a['state']}]{parent}")


@agent_app.command("send")
def agent_send(
    agent_id: str = typer.Argument(help="Agent UUID (hex)."),
    message: str = typer.Argument(help="Message to send."),
    root: Path = _ROOT_OPT,
) -> None:
    """Send a message to an agent and print the response."""
    result = _call(root, "agent.send", {"agent_id": agent_id, "message": message})
    typer.echo(result.get("response", ""))


@agent_app.command("inspect")
def agent_inspect(
    agent_id: str = typer.Argument(help="Agent UUID (hex)."),
    root: Path = _ROOT_OPT,
) -> None:
    """Inspect an agent's state."""
    result = _call(root, "agent.inspect", {"agent_id": agent_id})
    typer.echo(f"name:     {result['name']}")
    typer.echo(f"state:    {result['state']}")
    children = result.get("children", [])
    if children:
        typer.echo("children:")
        for c in children:
            typer.echo(f"  {c['agent_id']}  {c['name']}  [{c['state']}]")
    inbox = result.get("inbox", [])
    if inbox:
        typer.echo("inbox:")
        for m in inbox:
            typer.echo(f"  from={m['from']}  {m['text'][:80]}")


@agent_app.command("terminate")
def agent_terminate(
    agent_id: str = typer.Argument(help="Agent UUID (hex)."),
    root: Path = _ROOT_OPT,
) -> None:
    """Terminate an agent."""
    result = _call(root, "agent.terminate", {"agent_id": agent_id})
    typer.echo(f"terminated {result.get('agent_id', agent_id)}")


def main() -> None:
    """Entry point for ``substrat`` CLI."""
    app()
