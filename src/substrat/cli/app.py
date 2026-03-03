# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI — thin typer client for the Substrat daemon."""

from __future__ import annotations

import asyncio
import json
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
workspace_app = typer.Typer(help="Workspace management commands.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(agent_app, name="agent")
app.add_typer(workspace_app, name="workspace")

_DEFAULT_ROOT = Path(os.environ.get("SUBSTRAT_ROOT", Path.home() / ".substrat"))
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
    except json.JSONDecodeError:
        typer.echo("error: invalid response from daemon", err=True)
        raise typer.Exit(1) from None
    except OSError:
        typer.echo("error: daemon not running", err=True)
        raise typer.Exit(1) from None


# -- daemon commands -----------------------------------------------------------


@daemon_app.command()
def start(
    root: Path = _ROOT_OPT,
    model: str | None = typer.Option(None, help="Default model."),
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
        except (ValueError, ProcessLookupError):
            pass  # PID garbage or dead — proceed to start.
        except PermissionError:
            # Process exists but different user — treat as running.
            typer.echo(f"daemon already running (pid {pid})")
            return
        else:
            typer.echo(f"daemon already running (pid {pid})")
            return

    # Spawn daemon as background process.
    cmd = [
        sys.executable,
        "-m",
        "substrat.daemon",
        "--root",
        str(root),
        "--max-slots",
        str(max_slots),
    ]
    if model is not None:
        cmd.extend(["--model", model])
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
    workspace: str | None = typer.Option(None, help="Workspace name."),
    root: Path = _ROOT_OPT,
) -> None:
    """Create a root agent."""
    params: dict[str, Any] = {"name": name, "instructions": instructions}
    if provider is not None:
        params["provider"] = provider
    if model is not None:
        params["model"] = model
    if workspace is not None:
        params["workspace"] = workspace
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


# -- workspace commands --------------------------------------------------------


@workspace_app.command("create")
def workspace_create(
    name: str = typer.Argument(help="Workspace name."),
    scope: str | None = typer.Option(
        None, help="Scope UUID (hex). Auto-generated if omitted."
    ),
    network: bool = typer.Option(False, help="Allow network access."),
    root: Path = _ROOT_OPT,
) -> None:
    """Create a workspace."""
    params: dict[str, Any] = {"name": name, "network_access": network}
    if scope is not None:
        params["scope"] = scope
    result = _call(root, "workspace.create", params)
    typer.echo(f"{result['scope']}/{result['name']}")


@workspace_app.command("list")
def workspace_list(
    root: Path = _ROOT_OPT,
) -> None:
    """List all workspaces."""
    result = _call(root, "workspace.list", {})
    workspaces = result.get("workspaces", [])
    if not workspaces:
        typer.echo("no workspaces")
        return
    for ws in workspaces:
        net = "  [net]" if ws.get("network_access") else ""
        typer.echo(f"{ws['scope']}/{ws['name']}{net}")


@workspace_app.command("delete")
def workspace_delete(
    name: str = typer.Argument(help="Workspace name."),
    scope: str = typer.Argument(help="Scope UUID (hex)."),
    root: Path = _ROOT_OPT,
) -> None:
    """Delete a workspace."""
    _call(root, "workspace.delete", {"scope": scope, "name": name})
    typer.echo(f"deleted {scope}/{name}")


@workspace_app.command("link")
def workspace_link(
    name: str = typer.Argument(help="Workspace name."),
    scope: str = typer.Argument(help="Scope UUID (hex)."),
    source: str = typer.Option(..., help="Host path to bind."),
    target: str = typer.Option(..., help="Mount path inside sandbox."),
    mode: str = typer.Option("ro", help="Mount mode (ro or rw)."),
    root: Path = _ROOT_OPT,
) -> None:
    """Add a bind-mount link to a workspace."""
    _call(
        root,
        "workspace.link",
        {
            "scope": scope,
            "name": name,
            "host_path": source,
            "mount_path": target,
            "mode": mode,
        },
    )
    typer.echo(f"linked {scope}/{name} {source} -> {target} ({mode})")


@workspace_app.command("unlink")
def workspace_unlink(
    name: str = typer.Argument(help="Workspace name."),
    scope: str = typer.Argument(help="Scope UUID (hex)."),
    target: str = typer.Option(..., help="Mount path to remove."),
    root: Path = _ROOT_OPT,
) -> None:
    """Remove a bind-mount link from a workspace."""
    _call(
        root,
        "workspace.unlink",
        {"scope": scope, "name": name, "mount_path": target},
    )
    typer.echo(f"unlinked {target} from {scope}/{name}")


@workspace_app.command("view")
def workspace_view(
    source_name: str = typer.Argument(help="Source workspace name."),
    source_scope: str = typer.Argument(help="Source workspace scope (hex)."),
    name: str = typer.Option(..., help="Name for the view workspace."),
    scope: str | None = typer.Option(
        None, help="Scope for the view (auto if omitted)."
    ),
    subdir: str | None = typer.Option(None, help="Source subdirectory to expose."),
    mode: str = typer.Option("ro", help="Mount mode (ro or rw)."),
    root: Path = _ROOT_OPT,
) -> None:
    """Create a view workspace linked into another workspace's root."""
    # Resolve source root path.
    source = _call(
        root,
        "workspace.inspect",
        {"scope": source_scope, "name": source_name},
    )
    host_path = source["root_path"]
    if subdir is not None:
        host_path = str(Path(host_path) / subdir)

    # Create the view workspace.
    create_params: dict[str, Any] = {"name": name}
    if scope is not None:
        create_params["scope"] = scope
    created = _call(root, "workspace.create", create_params)

    # Link source into the view.
    _call(
        root,
        "workspace.link",
        {
            "scope": created["scope"],
            "name": name,
            "host_path": host_path,
            "mount_path": host_path,
            "mode": mode,
        },
    )
    typer.echo(f"{created['scope']}/{name}")


@workspace_app.command("inspect")
def workspace_inspect(
    name: str = typer.Argument(help="Workspace name."),
    scope: str = typer.Argument(help="Scope UUID (hex)."),
    root: Path = _ROOT_OPT,
) -> None:
    """Inspect a workspace's details."""
    result = _call(root, "workspace.inspect", {"scope": scope, "name": name})
    typer.echo(f"name:     {result['name']}")
    typer.echo(f"scope:    {result['scope']}")
    typer.echo(f"root:     {result['root_path']}")
    typer.echo(f"network:  {result['network_access']}")
    typer.echo(f"created:  {result['created_at']}")
    links = result.get("links", [])
    if links:
        typer.echo("links:")
        for lk in links:
            typer.echo(f"  {lk['host_path']} -> {lk['mount_path']} ({lk['mode']})")


def main() -> None:
    """Entry point for ``substrat`` CLI."""
    app()
