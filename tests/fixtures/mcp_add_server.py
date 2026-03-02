#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Minimal MCP stdio server exposing a single `add(a, b)` tool.

JSON-RPC 2.0 over stdin/stdout, stdlib only.  Used as a test fixture
for verifying MCP tool calls inside bwrap sandboxes.
"""

import json
import sys

_TOOL = {
    "name": "add",
    "description": "Add two numbers.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "a": {"type": "number"},
            "b": {"type": "number"},
        },
        "required": ["a", "b"],
    },
}


def _respond(id: object, result: object) -> None:
    """Send a JSON-RPC success response."""
    msg = json.dumps({"jsonrpc": "2.0", "id": id, "result": result})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _error(id: object, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    msg = json.dumps(
        {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}
    )
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _handle(request: dict) -> None:
    method = request.get("method", "")
    rid = request.get("id")

    # Notifications have no id — swallow silently.
    if rid is None:
        return

    if method == "initialize":
        _respond(
            rid,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "add-server", "version": "0.1.0"},
            },
        )
    elif method == "tools/list":
        _respond(rid, {"tools": [_TOOL]})
    elif method == "tools/call":
        params = request.get("params", {})
        args = params.get("arguments", {})
        a, b = args.get("a", 0), args.get("b", 0)
        _respond(
            rid,
            {"content": [{"type": "text", "text": str(a + b)}]},
        )
    else:
        _error(rid, -32601, f"Unknown method: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(request)


if __name__ == "__main__":
    main()
