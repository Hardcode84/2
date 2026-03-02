# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP stdio server — JSON-RPC 2.0 bridge between agent providers and Substrat tools.

Cursor-agent (or any MCP-aware provider) spawns this as a subprocess.
The server reads JSON-RPC requests from stdin, dispatches tool calls
through a pluggable callable, and writes responses to stdout.

Sync, single-threaded, stdlib only. Logging belongs in ToolHandler,
not here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from typing import Any, TextIO

from substrat.agent.tools import ToolHandler

# -- Type alias for the dispatch callable --------------------------------

ToolDispatch = Callable[[str, dict[str, Any]], dict[str, Any]]

# -- Protocol constants --------------------------------------------------

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "substrat-tools"
_SERVER_VERSION = "0.1.0"

# JSON-RPC error codes.
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

# -- Tool catalog --------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": "Send a message to a reachable agent (parent, child, or sibling).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Agent name."},
                "text": {"type": "string", "description": "Message body."},
                "sync": {
                    "type": "boolean",
                    "description": "Request synchronous reply delivery.",
                    "default": True,
                },
            },
            "required": ["recipient", "text"],
        },
    },
    {
        "name": "broadcast",
        "description": "Multicast a message to all siblings in the team.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message body."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "check_inbox",
        "description": "Retrieve pending async messages.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "spawn_agent",
        "description": "Create a child agent. Returns immediately; actual session creation is deferred.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Child agent name."},
                "instructions": {
                    "type": "string",
                    "description": "System prompt / task description.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace name or spec.",
                },
            },
            "required": ["name", "instructions"],
        },
    },
    {
        "name": "inspect_agent",
        "description": "View a subordinate's state and recent activity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Child agent name."},
            },
            "required": ["name"],
        },
    },
]

_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in _TOOLS)

# -- JSON-RPC helpers ----------------------------------------------------


def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


# -- Server class --------------------------------------------------------


class McpServer:
    """MCP stdio server. Stateless beyond the dispatch callable."""

    def __init__(self, dispatch: ToolDispatch) -> None:
        self._dispatch = dispatch

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single JSON-RPC request. Returns None for notifications."""
        req_id = request.get("id")
        if req_id is None:
            return None

        method = request.get("method", "")

        if method == "initialize":
            return _rpc_result(req_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
            })

        if method == "tools/list":
            return _rpc_result(req_id, {"tools": _TOOLS})

        if method == "tools/call":
            return self._handle_tool_call(req_id, request.get("params", {}))

        return _rpc_error(req_id, _METHOD_NOT_FOUND, f"Unknown method: {method}")

    def run(
        self,
        *,
        input: TextIO = sys.stdin,
        output: TextIO = sys.stdout,
    ) -> None:
        """Read JSON-RPC from *input*, write responses to *output*."""
        for line in input:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(request)
            if response is not None:
                output.write(json.dumps(response) + "\n")
                output.flush()

    # -- Private ---------------------------------------------------------

    def _handle_tool_call(
        self,
        req_id: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch a tools/call request."""
        tool_name = params.get("name", "")
        if tool_name not in _TOOL_NAMES:
            return _rpc_error(
                req_id,
                _INVALID_PARAMS,
                f"Unknown tool: {tool_name}",
            )
        arguments = params.get("arguments", {})
        try:
            result = self._dispatch(tool_name, arguments)
        except TypeError as exc:
            return _rpc_error(req_id, _INVALID_PARAMS, str(exc))
        except Exception as exc:
            return _rpc_error(req_id, _INTERNAL_ERROR, str(exc))
        # Tool results use MCP content format — even tool-level errors.
        text = json.dumps(result)
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": text}],
        })


# -- Dispatch factories --------------------------------------------------


def direct_dispatch(handler: ToolHandler) -> ToolDispatch:
    """In-process dispatch wrapping a ToolHandler. For testing."""
    _methods: dict[str, Callable[..., dict[str, Any]]] = {
        "send_message": handler.send_message,
        "broadcast": handler.broadcast,
        "check_inbox": handler.check_inbox,
        "spawn_agent": handler.spawn_agent,
        "inspect_agent": handler.inspect_agent,
    }

    def dispatch(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        method = _methods.get(tool_name)
        if method is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        # Rename workspace -> workspace_subdir for spawn_agent.
        if tool_name == "spawn_agent" and "workspace" in arguments:
            arguments = dict(arguments)
            arguments["workspace_subdir"] = arguments.pop("workspace")
        return method(**arguments)

    return dispatch


def daemon_dispatch(socket_path: str, agent_id: str) -> ToolDispatch:
    """UDS dispatch to the Substrat daemon. Stub until daemon RPC exists.

    Intended wire format::

        → {"method": "tool.call", "params": {"agent_id": "...", "tool": "...", "arguments": {...}}}
        ← {"result": {...}}
    """
    raise NotImplementedError(
        f"daemon_dispatch not yet implemented (socket={socket_path}, agent={agent_id})"
    )


# -- Entry point ---------------------------------------------------------


def main() -> None:
    """``python -m substrat.provider.mcp_server --agent-id <uuid>``."""
    parser = argparse.ArgumentParser(description="Substrat MCP tool server")
    parser.add_argument("--agent-id", required=True, help="Agent UUID.")
    parser.parse_args()

    socket_path = os.environ.get("SUBSTRAT_SOCKET")
    if not socket_path:
        raise SystemExit("SUBSTRAT_SOCKET not set — cannot connect to daemon")

    dispatch = daemon_dispatch(socket_path, parser.parse_args().agent_id)
    McpServer(dispatch).run()


if __name__ == "__main__":
    main()
