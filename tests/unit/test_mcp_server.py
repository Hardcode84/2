# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MCP stdio server."""

from __future__ import annotations

import io
import json
from typing import Any
from uuid import uuid4

import pytest

from substrat.agent import (
    AGENT_TOOLS,
    AgentNode,
    AgentTree,
    Inbox,
    InboxRegistry,
    ToolHandler,
)
from substrat.provider.mcp_server import (
    _PROTOCOL_VERSION,
    _SERVER_NAME,
    _SERVER_VERSION,
    McpServer,
    direct_dispatch,
)

# -- Helpers -------------------------------------------------------------


def _handler_methods(handler: ToolHandler) -> dict[str, Any]:
    """Build a name→callable dict from a ToolHandler."""
    return {
        "send_message": handler.send_message,
        "broadcast": handler.broadcast,
        "check_inbox": handler.check_inbox,
        "spawn_agent": handler.spawn_agent,
        "inspect_agent": handler.inspect_agent,
    }


# -- Fixtures ------------------------------------------------------------


@pytest.fixture()
def server() -> McpServer:
    """McpServer backed by a real ToolHandler. Tree: root -> {alice, bob}."""
    tree = AgentTree()
    inboxes: InboxRegistry = {}
    root = AgentNode(session_id=uuid4(), name="root")
    alice = AgentNode(session_id=uuid4(), name="alice", parent_id=root.id)
    bob = AgentNode(session_id=uuid4(), name="bob", parent_id=root.id)
    for n in (root, alice, bob):
        tree.add(n)
        inboxes[n.id] = Inbox()
    handler = ToolHandler(tree, inboxes, alice.id)
    return McpServer(AGENT_TOOLS, direct_dispatch(_handler_methods(handler)))


@pytest.fixture()
def echo_server() -> McpServer:
    """McpServer with trivial echo dispatch — returns arguments as-is."""

    def echo(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"tool": tool_name, **arguments}

    return McpServer(AGENT_TOOLS, echo)


# -- initialize ----------------------------------------------------------


def test_initialize_returns_capabilities(echo_server: McpServer) -> None:
    resp = echo_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp is not None
    result = resp["result"]
    assert "capabilities" in result
    assert "serverInfo" in result
    assert result["serverInfo"]["name"] == _SERVER_NAME
    assert result["serverInfo"]["version"] == _SERVER_VERSION


def test_initialize_protocol_version(echo_server: McpServer) -> None:
    resp = echo_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp is not None
    assert resp["result"]["protocolVersion"] == _PROTOCOL_VERSION


# -- notifications -------------------------------------------------------


def test_notification_returns_none(echo_server: McpServer) -> None:
    resp = echo_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None


def test_any_request_without_id_returns_none(echo_server: McpServer) -> None:
    resp = echo_server.handle({"jsonrpc": "2.0", "method": "tools/list"})
    assert resp is None


# -- tools/list ----------------------------------------------------------


def test_tools_list_returns_five_tools(echo_server: McpServer) -> None:
    resp = echo_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp is not None
    tools = resp["result"]["tools"]
    assert len(tools) == 5


def test_tools_list_schema_fields(echo_server: McpServer) -> None:
    resp = echo_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp is not None
    for tool in resp["result"]["tools"]:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


# -- tools/call: dispatch ------------------------------------------------


def test_call_send_message(server: McpServer) -> None:
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "send_message",
                "arguments": {"recipient": "bob", "text": "hello"},
            },
        }
    )
    assert resp is not None
    content = resp["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert payload["status"] == "sent"


def test_call_broadcast(server: McpServer) -> None:
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "broadcast", "arguments": {"text": "all hands"}},
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "sent"
    assert payload["recipient_count"] == 1  # Only bob (alice is sender).


def test_call_check_inbox_empty(server: McpServer) -> None:
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "check_inbox", "arguments": {}},
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload == {"messages": []}


def test_call_check_inbox_no_arguments_key(server: McpServer) -> None:
    """params without arguments key — check_inbox takes none, should work."""
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "check_inbox"},
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload == {"messages": []}


def test_call_spawn_agent(server: McpServer) -> None:
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "spawn_agent",
                "arguments": {"name": "child1", "instructions": "do stuff"},
            },
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "accepted"
    assert payload["name"] == "child1"


def test_call_inspect_agent_error(server: McpServer) -> None:
    """inspect_agent on a non-existent child — returns tool-level error."""
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "inspect_agent",
                "arguments": {"name": "ghost"},
            },
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" in payload


def test_spawn_workspace_accepted(server: McpServer) -> None:
    """spawn_agent accepts optional workspace parameter."""
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "spawn_agent",
                "arguments": {
                    "name": "child_ws",
                    "instructions": "go",
                    "workspace": "shared",
                },
            },
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "accepted"


# -- tools/call: result format -------------------------------------------


def test_result_is_mcp_content_format(echo_server: McpServer) -> None:
    resp = echo_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "check_inbox", "arguments": {}},
        }
    )
    assert resp is not None
    content = resp["result"]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["type"] == "text"


def test_tool_error_is_result_not_rpc_error(server: McpServer) -> None:
    """ToolHandler {error: ...} is a tool result, not JSON-RPC error."""
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "inspect_agent",
                "arguments": {"name": "nonexistent"},
            },
        }
    )
    assert resp is not None
    # It's a result, not a protocol error.
    assert "result" in resp
    assert "error" not in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" in payload


# -- tools/call: error handling ------------------------------------------


def test_unknown_tool_rpc_error(echo_server: McpServer) -> None:
    resp = echo_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == -32602
    assert "nonexistent_tool" in resp["error"]["message"]


def test_bad_arguments_rpc_error(server: McpServer) -> None:
    """Passing wrong argument types should produce a -32602 error."""
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "send_message",
                # Missing required 'text' arg, passing unexpected kwarg.
                "arguments": {"recipient": "bob", "bogus": 42},
            },
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == -32602


def test_dispatch_exception_internal_error() -> None:
    """Generic exception in dispatch → -32603."""

    def boom(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    srv = McpServer(AGENT_TOOLS, boom)
    resp = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "check_inbox", "arguments": {}},
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == -32603
    assert "kaboom" in resp["error"]["message"]


# -- unknown method ------------------------------------------------------


def test_unknown_method_rpc_error(echo_server: McpServer) -> None:
    resp = echo_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "completions/complete",
        }
    )
    assert resp is not None
    assert resp["error"]["code"] == -32601


# -- run() ---------------------------------------------------------------


def test_run_stdio_loop(echo_server: McpServer) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    inp = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    out = io.StringIO()
    echo_server.run(input=inp, output=out)
    lines = out.getvalue().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == 1
    assert json.loads(lines[1])["id"] == 2


def test_run_malformed_json_skipped(echo_server: McpServer) -> None:
    inp = io.StringIO(
        "not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n"
    )
    out = io.StringIO()
    echo_server.run(input=inp, output=out)
    lines = out.getvalue().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == 1


def test_run_empty_lines_skipped(echo_server: McpServer) -> None:
    inp = io.StringIO(
        "\n\n"
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n\n"
    )
    out = io.StringIO()
    echo_server.run(input=inp, output=out)
    lines = [x for x in out.getvalue().split("\n") if x.strip()]
    assert len(lines) == 1


def test_run_notification_no_output(echo_server: McpServer) -> None:
    inp = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    out = io.StringIO()
    echo_server.run(input=inp, output=out)
    assert out.getvalue() == ""


# -- direct_dispatch -----------------------------------------------------


def test_direct_dispatch_routes_all_tools() -> None:
    """Every tool name in the catalog is handled by direct_dispatch."""
    tree = AgentTree()
    inboxes: InboxRegistry = {}
    root = AgentNode(session_id=uuid4(), name="root")
    agent = AgentNode(session_id=uuid4(), name="agent", parent_id=root.id)
    peer = AgentNode(session_id=uuid4(), name="peer", parent_id=root.id)
    for n in (root, agent, peer):
        tree.add(n)
        inboxes[n.id] = Inbox()
    handler = ToolHandler(tree, inboxes, agent.id)
    dispatch = direct_dispatch(_handler_methods(handler))

    # Each tool should return a dict without raising.
    assert "status" in dispatch("send_message", {"recipient": "peer", "text": "hi"})
    assert "status" in dispatch("broadcast", {"text": "yo"})
    assert "messages" in dispatch("check_inbox", {})
    assert "status" in dispatch("spawn_agent", {"name": "kid", "instructions": "go"})
    # inspect_agent on nonexistent returns error dict, not exception.
    assert "error" in dispatch("inspect_agent", {"name": "ghost"})


def test_direct_dispatch_unknown_tool() -> None:
    dispatch = direct_dispatch({"ping": lambda: {"pong": True}})

    with pytest.raises(ValueError, match="Unknown tool"):
        dispatch("nope", {})
