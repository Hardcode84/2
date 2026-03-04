# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Daemon process — composes the full stack, serves UDS requests."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from substrat.agent.node import AgentStateError
from substrat.agent.tools import AGENT_TOOLS
from substrat.model import CommandWrapper, LinkSpec
from substrat.orchestrator import Orchestrator
from substrat.provider import default_providers
from substrat.provider.base import AgentProvider
from substrat.scheduler import TurnScheduler
from substrat.session.multiplexer import SessionMultiplexer
from substrat.session.store import SessionStore
from substrat.workspace import bwrap
from substrat.workspace.handler import WORKSPACE_TOOLS
from substrat.workspace.mapping import WorkspaceMapping
from substrat.workspace.model import Workspace
from substrat.workspace.store import WorkspaceStore

_ALL_TOOLS = AGENT_TOOLS + WORKSPACE_TOOLS
_WS_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in WORKSPACE_TOOLS)

# Source root for the substrat package. For editable installs this lives
# outside sys.prefix and must be bind-mounted into bwrap sandboxes.
_SUBSTRAT_SRC_DIR = Path(__file__).resolve().parent.parent

_log = logging.getLogger(__name__)

# -- Error codes ---------------------------------------------------------------

ERR_NOT_FOUND = 1
ERR_INVALID = 2
ERR_INTERNAL = 3
ERR_METHOD = 4


class Daemon:
    """Substrat daemon. Owns the full composition stack, serves UDS."""

    def __init__(
        self,
        root: Path,
        *,
        default_provider: str = "cursor-agent",
        default_model: str | None = None,
        max_slots: int = 4,
        providers: dict[str, AgentProvider] | None = None,
    ) -> None:
        self._root = root
        self._sock_path = root / "daemon.sock"
        self._pid_path = root / "daemon.pid"
        self._server: asyncio.AbstractServer | None = None

        # Build composition stack.
        store = SessionStore(root / "agents")
        mux = SessionMultiplexer(store, max_slots=max_slots)
        if providers is None:
            providers = default_providers(tools=_ALL_TOOLS)
        scheduler = TurnScheduler(
            providers,
            mux,
            store,
            log_root=root / "agents",
            daemon_socket=str(self._sock_path),
        )
        self._ws_store = WorkspaceStore(root / "workspaces")
        ws_mapping = WorkspaceMapping()
        self._orch = Orchestrator(
            scheduler,
            default_provider=default_provider,
            default_model=default_model,
            ws_store=self._ws_store,
            ws_mapping=ws_mapping,
            wrap_command_factory=self._make_wrap_command,
        )

        self._handlers: dict[str, Any] = {
            "agent.create": self._handle_agent_create,
            "agent.list": self._handle_agent_list,
            "agent.send": self._handle_agent_send,
            "agent.inspect": self._handle_agent_inspect,
            "agent.terminate": self._handle_agent_terminate,
            "tool.call": self._handle_tool_call,
            "workspace.create": self._handle_workspace_create,
            "workspace.list": self._handle_workspace_list,
            "workspace.delete": self._handle_workspace_delete,
            "workspace.link": self._handle_workspace_link,
            "workspace.unlink": self._handle_workspace_unlink,
            "workspace.inspect": self._handle_workspace_inspect,
        }

    @property
    def orchestrator(self) -> Orchestrator:
        """Public access for testing."""
        return self._orch

    @property
    def socket_path(self) -> Path:
        return self._sock_path

    # -- Lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Start the daemon: cleanup stale state, recover, serve."""
        self._root.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale()
        await self._orch.recover()
        self._orch.start_wake_loop()
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._sock_path),
        )
        self._pid_path.write_text(str(os.getpid()))
        _log.info("daemon started, socket=%s pid=%d", self._sock_path, os.getpid())

    async def stop(self) -> None:
        """Stop the daemon: close server, remove socket and PID file."""
        await self._orch.stop_wake_loop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._sock_path.exists():
            self._sock_path.unlink()
        if self._pid_path.exists():
            self._pid_path.unlink()
        _log.info("daemon stopped")

    def _cleanup_stale(self) -> None:
        """Remove leftover socket/PID from a dead daemon."""
        if not self._pid_path.exists():
            # No PID file — clean up any orphaned socket.
            if self._sock_path.exists():
                self._sock_path.unlink()
            return
        try:
            pid = int(self._pid_path.read_text().strip())
        except ValueError:
            # PID file is garbage.
            if self._sock_path.exists():
                self._sock_path.unlink()
            self._pid_path.unlink()
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # Process is dead.
            if self._sock_path.exists():
                self._sock_path.unlink()
            self._pid_path.unlink()
            return
        except PermissionError:
            pass  # Different user but alive — fall through.
        # Process exists (PermissionError means different user but alive).
        raise RuntimeError(
            f"daemon already running (pid {pid}, socket {self._sock_path})"
        )

    # -- Connection handler ----------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one request per connection."""
        try:
            data = await reader.read()
            if not data:
                return

            try:
                req = json.loads(data)
            except json.JSONDecodeError as exc:
                resp = _error_envelope(None, ERR_INVALID, f"malformed JSON: {exc}")
                writer.write(json.dumps(resp).encode() + b"\n")
                await writer.drain()
                return

            req_id = req.get("id")
            method = req.get("method", "")
            params = req.get("params", {})

            handler = self._handlers.get(method)
            if handler is None:
                resp = _error_envelope(req_id, ERR_METHOD, f"unknown method: {method}")
            else:
                try:
                    result = await handler(params)
                    resp = {"id": req_id, "result": result}
                except KeyError as exc:
                    resp = _error_envelope(req_id, ERR_NOT_FOUND, str(exc))
                except (ValueError, TypeError, AgentStateError) as exc:
                    resp = _error_envelope(req_id, ERR_INVALID, str(exc))
                except Exception as exc:
                    _log.exception("handler %s failed", method)
                    resp = _error_envelope(req_id, ERR_INTERNAL, str(exc))

            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except Exception:
            _log.exception("connection handler crashed")
        finally:
            writer.close()
            await writer.wait_closed()

    # -- Wrap-command factory --------------------------------------------------

    def _make_wrap_command(self, scope: UUID, ws_name: str) -> CommandWrapper:
        """Build a per-agent closure that sandboxes commands via bwrap.

        Re-reads workspace from store on each invocation so link_dir /
        unlink_dir changes are picked up without session restart.
        """
        sock = self._sock_path
        ws_store = self._ws_store

        # Write socket path outside the sandbox root so _write_mcp_config
        # can embed it in the MCP config env — cursor-agent does not
        # propagate env to MCP server subprocesses.
        ws = ws_store.load(scope, ws_name)
        ws.root_path.parent.mkdir(parents=True, exist_ok=True)
        (ws.root_path.parent / ".substrat_socket").write_text(str(sock))

        # Collect python prefix binds so the MCP server can import substrat.
        py_binds: list[LinkSpec] = []
        prefix = Path(sys.prefix)
        py_binds.append(LinkSpec(prefix, prefix, "ro"))
        if sys.prefix != sys.base_prefix:
            base = Path(sys.base_prefix)
            py_binds.append(LinkSpec(base, base, "ro"))
        # Editable installs: source tree lives outside sys.prefix.
        if not _SUBSTRAT_SRC_DIR.is_relative_to(prefix):
            py_binds.append(LinkSpec(_SUBSTRAT_SRC_DIR, _SUBSTRAT_SRC_DIR, "ro"))

        def wrapper(
            cmd: Sequence[str],
            binds: Sequence[LinkSpec],
            env: Mapping[str, str],
        ) -> Sequence[str]:
            workspace = ws_store.load(scope, ws_name)
            all_binds = [
                *binds,
                *py_binds,
                LinkSpec(sock, sock, "ro"),
            ]
            all_env = {**env, "SUBSTRAT_SOCKET": str(sock)}
            return bwrap.build_command(
                workspace,
                all_binds,
                command=cmd,
                env=all_env,
            )

        return wrapper

    # -- RPC handlers ----------------------------------------------------------

    async def _handle_agent_create(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name") or ""
        instructions = params.get("instructions") or ""
        provider = params.get("provider")
        model = params.get("model")
        ws_key = self._resolve_ws_param(params.get("workspace"))
        node = await self._orch.create_root_agent(
            name,
            instructions,
            provider=provider,
            model=model,
            workspace=ws_key,
        )
        return {"agent_id": node.id.hex, "name": node.name}

    def _resolve_ws_param(self, raw: Any) -> tuple[UUID, str] | None:
        """Resolve workspace param to (scope, name) key.

        Accepts None, a dict with scope+name, or a plain name string
        (scans the store for the first match).
        """
        if raw is None:
            return None
        if isinstance(raw, dict):
            return UUID(raw["scope"]), raw["name"]
        # Plain name — scan for first match.
        ws_name = str(raw)
        for ws in self._ws_store.scan():
            if ws.name == ws_name:
                return ws.scope, ws.name
        raise ValueError(f"workspace not found: {ws_name}")

    async def _handle_agent_list(self, params: dict[str, Any]) -> dict[str, Any]:
        nodes = []
        for node in self._orch.tree.roots():
            nodes.extend(self._walk_tree(node))
        return {"agents": nodes}

    async def _handle_agent_send(self, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = UUID(params["agent_id"])
        message = params.get("message", "")
        response = await self._orch.run_turn(agent_id, message)
        return {"response": response}

    async def _handle_agent_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = UUID(params["agent_id"])
        node = self._orch.tree.get(agent_id)
        children = self._orch.tree.children(agent_id)
        inbox = self._orch.inboxes.get(agent_id)
        pending = inbox.peek() if inbox is not None else []
        return {
            "agent_id": node.id.hex,
            "name": node.name,
            "state": node.state.value,
            "children": [
                {"agent_id": c.id.hex, "name": c.name, "state": c.state.value}
                for c in children
            ],
            "inbox": [
                {"from": m.sender.hex, "text": m.payload, "message_id": m.id.hex}
                for m in pending
            ],
        }

    async def _handle_agent_terminate(self, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = UUID(params["agent_id"])
        await self._orch.terminate_agent(agent_id)
        return {"status": "terminated", "agent_id": agent_id.hex}

    _TOOL_NAMES: frozenset[str] = frozenset(t.name for t in _ALL_TOOLS)

    async def _handle_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = UUID(params["agent_id"])
        tool_name = params["tool"]
        arguments = params.get("arguments", {})
        if tool_name not in self._TOOL_NAMES:
            raise ValueError(f"unknown tool: {tool_name}")
        if tool_name in _WS_TOOL_NAMES:
            ws_handler = self._orch.get_ws_handler(agent_id)
            if ws_handler is None:
                return {"error": "workspace tools not available"}
            method = getattr(ws_handler, tool_name)
        else:
            handler = self._orch.get_handler(agent_id)
            method = getattr(handler, tool_name)
        return method(**arguments)  # type: ignore[no-any-return]

    # -- Workspace RPC handlers ------------------------------------------------

    async def _handle_workspace_create(self, params: dict[str, Any]) -> dict[str, Any]:
        from uuid import uuid4

        ws_name = params["name"]
        scope_hex = params.get("scope")
        scope = UUID(scope_hex) if scope_hex else uuid4()
        network = params.get("network_access", False)
        ws = Workspace(
            name=ws_name,
            scope=scope,
            root_path=self._ws_store.workspace_dir(scope, ws_name) / "root",
            network_access=network,
        )
        self._ws_store.save(ws)
        return {"scope": scope.hex, "name": ws_name}

    async def _handle_workspace_list(self, params: dict[str, Any]) -> dict[str, Any]:
        workspaces = self._ws_store.scan()
        return {
            "workspaces": [
                {
                    "scope": ws.scope.hex,
                    "name": ws.name,
                    "network_access": ws.network_access,
                    "root_path": str(ws.root_path),
                }
                for ws in workspaces
            ]
        }

    async def _handle_workspace_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = UUID(params["scope"])
        ws_name = params["name"]
        self._ws_store.delete(scope, ws_name)
        return {"status": "deleted", "scope": scope.hex, "name": ws_name}

    async def _handle_workspace_link(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = UUID(params["scope"])
        ws_name = params["name"]
        try:
            ws = self._ws_store.load(scope, ws_name)
        except FileNotFoundError:
            raise KeyError(f"workspace not found: {scope.hex}/{ws_name}") from None
        link = LinkSpec(
            host_path=Path(params["host_path"]),
            mount_path=Path(params["mount_path"]),
            mode=params.get("mode", "ro"),
        )
        ws.links.append(link)
        self._ws_store.save(ws)
        return {"status": "linked", "scope": scope.hex, "name": ws_name}

    async def _handle_workspace_unlink(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = UUID(params["scope"])
        ws_name = params["name"]
        mount_path = Path(params["mount_path"])
        try:
            ws = self._ws_store.load(scope, ws_name)
        except FileNotFoundError:
            raise KeyError(f"workspace not found: {scope.hex}/{ws_name}") from None
        before = len(ws.links)
        ws.links = [lk for lk in ws.links if lk.mount_path != mount_path]
        if len(ws.links) == before:
            raise KeyError(
                f"no link at mount_path {mount_path} in {scope.hex}/{ws_name}"
            ) from None
        self._ws_store.save(ws)
        return {"status": "unlinked", "scope": scope.hex, "name": ws_name}

    async def _handle_workspace_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = UUID(params["scope"])
        ws_name = params["name"]
        try:
            ws = self._ws_store.load(scope, ws_name)
        except FileNotFoundError:
            raise KeyError(f"workspace not found: {scope.hex}/{ws_name}") from None
        return {
            "name": ws.name,
            "scope": ws.scope.hex,
            "root_path": str(ws.root_path),
            "network_access": ws.network_access,
            "created_at": ws.created_at,
            "links": [
                {
                    "host_path": str(lk.host_path),
                    "mount_path": str(lk.mount_path),
                    "mode": lk.mode,
                }
                for lk in ws.links
            ],
        }

    # -- Helpers ---------------------------------------------------------------

    def _walk_tree(self, node: Any) -> list[dict[str, Any]]:
        """Flatten a subtree into a list of dicts."""
        result: list[dict[str, Any]] = [
            {
                "agent_id": node.id.hex,
                "name": node.name,
                "state": node.state.value,
                "parent_id": node.parent_id.hex if node.parent_id else None,
            }
        ]
        for child in self._orch.tree.children(node.id):
            result.extend(self._walk_tree(child))
        return result


def _error_envelope(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"id": req_id, "error": {"code": code, "message": message}}


# -- Entry point ---------------------------------------------------------------


async def _run(daemon: Daemon) -> None:
    """Run daemon until interrupted."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await daemon.start()
    await stop_event.wait()
    await daemon.stop()


def main() -> None:
    """``python -m substrat.daemon``."""
    parser = argparse.ArgumentParser(description="Substrat daemon")
    _default_root = os.environ.get("SUBSTRAT_ROOT", str(Path.home() / ".substrat"))
    parser.add_argument("--root", type=Path, default=_default_root)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-slots", type=int, default=4)
    args = parser.parse_args()

    daemon = Daemon(
        args.root,
        default_model=args.model,
        max_slots=args.max_slots,
    )
    asyncio.run(_run(daemon))


if __name__ == "__main__":
    main()
