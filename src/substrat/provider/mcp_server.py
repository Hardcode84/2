# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP stdio server — generic JSON-RPC 2.0 over stdio.

Tool-agnostic: callers pass ToolDef objects and a dispatch callable at
construction. The server serializes schemas to MCP JSON internally,
handles protocol framing, error surfacing, and the readline loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO

from substrat.model import ToolDef

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


# -- Schema serialization ------------------------------------------------


def _tool_to_schema(tool: ToolDef) -> dict[str, Any]:
    """Convert a ToolDef to MCP tool JSON schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in tool.parameters:
        prop: dict[str, Any] = {"type": p.type, "description": p.description}
        if p.has_default:
            prop["default"] = p.default
        properties[p.name] = prop
        if p.required:
            required.append(p.name)
    schema: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
        },
    }
    if required:
        schema["inputSchema"]["required"] = required
    return schema


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
    """Generic MCP stdio server. Knows nothing about specific tools."""

    def __init__(
        self,
        tools: Sequence[ToolDef],
        dispatch: ToolDispatch,
        *,
        name: str = _SERVER_NAME,
        version: str = _SERVER_VERSION,
    ) -> None:
        self._tools = [_tool_to_schema(t) for t in tools]
        self._tool_names: frozenset[str] = frozenset(t.name for t in tools)
        self._dispatch = dispatch
        self._name = name
        self._version = version

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single JSON-RPC request. Returns None for notifications."""
        req_id = request.get("id")
        if req_id is None:
            return None

        method = request.get("method", "")

        if method == "initialize":
            return _rpc_result(
                req_id,
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self._name, "version": self._version},
                },
            )

        if method == "tools/list":
            return _rpc_result(req_id, {"tools": self._tools})

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
        if tool_name not in self._tool_names:
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
        return _rpc_result(
            req_id,
            {
                "content": [{"type": "text", "text": text}],
            },
        )


# -- Dispatch factories --------------------------------------------------


def direct_dispatch(
    methods: Mapping[str, Callable[..., dict[str, Any]]],
) -> ToolDispatch:
    """In-process dispatch from a name→callable mapping."""

    def dispatch(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        method = methods.get(tool_name)
        if method is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        return method(**arguments)

    return dispatch


def daemon_dispatch(socket_path: str, agent_id: str) -> ToolDispatch:
    """UDS dispatch to the Substrat daemon. Stub until daemon RPC exists.

    Intended wire format::

        → {"method": "tool.call", "params": {"agent_id": "...",
           "tool": "...", "arguments": {...}}}
        ← {"result": {...}}
    """
    raise NotImplementedError(
        f"daemon_dispatch not yet implemented (socket={socket_path}, agent={agent_id})"
    )


# -- Entry point ---------------------------------------------------------


def main() -> None:
    """``python -m substrat.provider.mcp_server --agent-id <uuid>``."""
    from substrat.agent.tools import AGENT_TOOLS

    parser = argparse.ArgumentParser(description="Substrat MCP tool server")
    parser.add_argument("--agent-id", required=True, help="Agent UUID.")
    args = parser.parse_args()

    socket_path = os.environ.get("SUBSTRAT_SOCKET")
    if not socket_path:
        raise SystemExit("SUBSTRAT_SOCKET not set — cannot connect to daemon")

    dispatch = daemon_dispatch(socket_path, args.agent_id)
    McpServer(AGENT_TOOLS, dispatch).run()


if __name__ == "__main__":
    main()
