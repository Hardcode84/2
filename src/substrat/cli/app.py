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

from substrat.rpc import RpcError, sync_call, sync_stream

app = typer.Typer(name="substrat", no_args_is_help=True)
daemon_app = typer.Typer(help="Daemon lifecycle commands.")
agent_app = typer.Typer(help="Agent management commands.")
workspace_app = typer.Typer(help="Workspace management commands.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(agent_app, name="agent")
app.add_typer(workspace_app, name="workspace")

_DEFAULT_ROOT = Path(os.environ.get("SUBSTRAT_ROOT", Path.home() / ".substrat"))
_ROOT_OPT = typer.Option(_DEFAULT_ROOT, help="Substrat root directory.")


_TRUNCATE_LEN = 80


def _format_event(entry: dict[str, Any]) -> str:
    """Format a single event log entry as a human-readable line."""
    ts = entry.get("ts", "")
    # Extract HH:MM:SS from ISO timestamp.
    if "T" in ts:
        time_part = ts.split("T")[1]
        hms = time_part[:8]
    else:
        hms = ts[:8] if len(ts) >= 8 else ts

    sid = entry.get("session_id", "????")[:4]
    event = entry.get("event", "?")

    # Build key details from data dict.
    data = entry.get("data", {})
    details: list[str] = []
    for k, v in data.items():
        s = str(v)
        if len(s) > _TRUNCATE_LEN:
            s = s[:_TRUNCATE_LEN] + "..."
        details.append(f"{k}={s}")
    detail_str = "  " + " ".join(details) if details else ""

    return f"{hms}  {sid}  {event}{detail_str}"


def _sock_path(root: Path) -> str:
    return str(root / "daemon.sock")


def _pid_path(root: Path) -> Path:
    return root / "daemon.pid"


def _call(
    root: Path,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Wrap sync_call with CLI error handling."""
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        return sync_call(_sock_path(root), method, params, **kwargs)
    except RpcError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(1) from exc
    except json.JSONDecodeError:
        typer.echo("error: invalid response from daemon", err=True)
        raise typer.Exit(1) from None
    except TimeoutError:
        typer.echo("error: request timed out", err=True)
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

        daemon = Daemon(
            root,
            default_provider="cursor-agent",
            default_model=model,
            max_slots=max_slots,
        )
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
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "daemon.log"
    log_file = open(log_path, "a")  # noqa: SIM115
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=log_file,
        stderr=log_file,
    )
    log_file.close()

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


@daemon_app.command()
def watch(
    root: Path = _ROOT_OPT,
    agent_id: str | None = typer.Option(
        None, help="Filter by agent name, path, or UUID."
    ),
) -> None:
    """Tail event logs from all sessions (or one agent)."""
    agents_dir = root / "agents"

    # If filtering by agent, resolve to its session directory.
    session_filter: str | None = None
    if agent_id is not None:
        result = _call(root, "agent.inspect", {"agent_id": agent_id})
        session_filter = result["session_id"]

    # Track file sizes to detect new bytes.
    offsets: dict[Path, int] = {}

    def _scan_logs() -> list[Path]:
        """Find all event log files, optionally filtered."""
        if not agents_dir.exists():
            return []
        if session_filter is not None:
            p = agents_dir / session_filter / "events.jsonl"
            return [p] if p.exists() else []
        return sorted(agents_dir.glob("*/events.jsonl"))

    # Seed offsets at current end-of-file so we only print new events.
    for path in _scan_logs():
        offsets[path] = path.stat().st_size

    try:
        while True:
            for path in _scan_logs():
                if path not in offsets:
                    offsets[path] = 0  # New session — show all events.
                size = path.stat().st_size
                if size <= offsets[path]:
                    continue
                # Read new bytes.
                with path.open("rb") as f:
                    f.seek(offsets[path])
                    new_data = f.read()
                offsets[path] = offsets[path] + len(new_data)
                for line in new_data.split(b"\n"):
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    typer.echo(_format_event(entry))
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass


@daemon_app.command()
def log(
    agent_id: str = typer.Argument(help="Agent name, path, or UUID."),
    root: Path = _ROOT_OPT,
) -> None:
    """Print an agent's event log, human-readable."""
    result = _call(root, "agent.inspect", {"agent_id": agent_id})
    session_id = result["session_id"]
    log_path = root / "agents" / session_id / "events.jsonl"
    if not log_path.exists():
        typer.echo("no events")
        return
    for line in log_path.read_bytes().split(b"\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        typer.echo(_format_event(entry))


# -- inbox commands ------------------------------------------------------------


@app.command("inbox")
def inbox(
    root: Path = _ROOT_OPT,
) -> None:
    """Read messages from agents to the user."""
    result = _call(root, "inbox.list", {})
    messages = result.get("messages", [])
    if not messages:
        typer.echo("no messages")
        return
    for m in messages:
        ts = m.get("timestamp", "")
        hms = ts.split("T")[1][:8] if "T" in ts else ts[:8]
        typer.echo(f"{hms}  from={m['from']}  {m['text']}")


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
        typer.echo(f"{a['path']}  [{a['state']}]")


@agent_app.command("send")
def agent_send(
    agent_id: str = typer.Argument(help="Agent name, path, or UUID."),
    message: str = typer.Argument(help="Message to send."),
    root: Path = _ROOT_OPT,
) -> None:
    """Send a message to an agent and print the response."""
    # Agent turns can take minutes — use a generous timeout.
    result = _call(
        root, "agent.send", {"agent_id": agent_id, "message": message}, timeout=600.0
    )
    typer.echo(result.get("response", ""))


@agent_app.command("inspect")
def agent_inspect(
    agent_id: str = typer.Argument(help="Agent name, path, or UUID."),
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


@agent_app.command("attach")
def agent_attach(
    agent_id: str = typer.Argument(help="Agent name, path, or UUID."),
    root: Path = _ROOT_OPT,
) -> None:
    """Attach to an agent — interactive streaming REPL."""
    # Verify agent exists.
    _call(root, "agent.inspect", {"agent_id": agent_id})
    sock = _sock_path(root)
    typer.echo(f"attached to {agent_id} (empty line or Ctrl-D to detach)")
    while True:
        try:
            prompt = input("> ")
        except (EOFError, KeyboardInterrupt):
            typer.echo("\ndetached")
            return
        if not prompt:
            typer.echo("detached")
            return
        try:
            for chunk in sync_stream(
                sock,
                "agent.stream",
                {"agent_id": agent_id, "message": prompt},
                timeout=600.0,
            ):
                typer.echo(chunk, nl=False)
            typer.echo()  # Newline after response.
        except RpcError as exc:
            typer.echo(f"\nerror: {exc.message}", err=True)
        except TimeoutError:
            typer.echo("\nerror: request timed out", err=True)
        except OSError:
            typer.echo("\nerror: daemon not running", err=True)
            raise typer.Exit(1) from None


@agent_app.command("terminate")
def agent_terminate(
    agent_id: str = typer.Argument(help="Agent name, path, or UUID."),
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
        None, help="Scope: agent name, USER, or UUID. Defaults to USER."
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
        label = ws.get("scope_label", ws["scope"])
        typer.echo(f"{label}/{ws['name']}{net}")


@workspace_app.command("delete")
def workspace_delete(
    name: str = typer.Argument(help="Workspace name."),
    scope: str = typer.Argument(help="Scope: agent name, USER, or UUID."),
    root: Path = _ROOT_OPT,
) -> None:
    """Delete a workspace."""
    _call(root, "workspace.delete", {"scope": scope, "name": name})
    typer.echo(f"deleted {scope}/{name}")


@workspace_app.command("link")
def workspace_link(
    name: str = typer.Argument(help="Workspace name."),
    scope: str = typer.Argument(help="Scope: agent name, USER, or UUID."),
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
    scope: str = typer.Argument(help="Scope: agent name, USER, or UUID."),
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
    source_scope: str = typer.Argument(help="Source scope: agent name, USER, or UUID."),
    name: str = typer.Option(..., help="Name for the view workspace."),
    scope: str | None = typer.Option(
        None, help="Scope for the view. Defaults to USER."
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
    scope: str = typer.Argument(help="Scope: agent name, USER, or UUID."),
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
