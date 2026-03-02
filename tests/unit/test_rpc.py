# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the UDS wire protocol."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from substrat.rpc import RpcError, async_call, sync_call

# -- Echo server ---------------------------------------------------------------


async def _echo_server(
    sock_path: str,
    handler: Any = None,
) -> asyncio.AbstractServer:
    """Start a UDS server that echoes requests back as results."""

    async def _handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        data = await reader.read()
        req = json.loads(data)
        if handler is not None:
            resp = handler(req)
        else:
            resp = {"id": req["id"], "result": req.get("params", {})}
        writer.write(json.dumps(resp).encode() + b"\n")
        writer.write_eof()
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(_handle, sock_path)
    return server


# -- sync_call / async_call against echo server --------------------------------


async def test_async_call_round_trip(tmp_path: Path) -> None:
    """async_call sends request, receives result dict."""
    sock = str(tmp_path / "test.sock")
    server = await _echo_server(sock)
    try:
        result = await async_call(sock, "test.method", {"key": "value"})
        assert result == {"key": "value"}
    finally:
        server.close()
        await server.wait_closed()


async def test_sync_call_round_trip(tmp_path: Path) -> None:
    """sync_call sends request, receives result dict."""
    sock = str(tmp_path / "test.sock")
    server = await _echo_server(sock)
    try:
        result = await asyncio.to_thread(sync_call, sock, "test.method", {"foo": 42})
        assert result == {"foo": 42}
    finally:
        server.close()
        await server.wait_closed()


# -- Error envelope ------------------------------------------------------------


async def test_async_call_error_envelope(tmp_path: Path) -> None:
    """Error envelope raises RpcError with code and message."""
    sock = str(tmp_path / "test.sock")

    def error_handler(req: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": req["id"],
            "error": {"code": 42, "message": "something broke"},
        }

    server = await _echo_server(sock, handler=error_handler)
    try:
        with pytest.raises(RpcError) as exc_info:
            await async_call(sock, "whatever", {})
        assert exc_info.value.code == 42
        assert exc_info.value.message == "something broke"
    finally:
        server.close()
        await server.wait_closed()


async def test_sync_call_error_envelope(tmp_path: Path) -> None:
    """sync_call also raises RpcError on error envelope."""
    sock = str(tmp_path / "test.sock")

    def error_handler(req: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": req["id"],
            "error": {"code": 1, "message": "nope"},
        }

    server = await _echo_server(sock, handler=error_handler)
    try:
        with pytest.raises(RpcError) as exc_info:
            await asyncio.to_thread(sync_call, sock, "whatever", {})
        assert exc_info.value.code == 1
    finally:
        server.close()
        await server.wait_closed()


# -- Connection errors ---------------------------------------------------------


async def test_sync_call_missing_socket(tmp_path: Path) -> None:
    """Missing socket raises ConnectionError (or subclass)."""
    with pytest.raises(OSError):
        sync_call(str(tmp_path / "nonexistent.sock"), "m", {})


async def test_async_call_missing_socket(tmp_path: Path) -> None:
    """Missing socket raises ConnectionError (or subclass)."""
    with pytest.raises(OSError):
        await async_call(str(tmp_path / "nonexistent.sock"), "m", {})


# -- daemon_dispatch -----------------------------------------------------------


async def test_daemon_dispatch_calls_sync_call() -> None:
    """daemon_dispatch returns a ToolDispatch that calls sync_call."""
    from substrat.provider.mcp_server import daemon_dispatch

    captured: list[tuple[str, str, dict[str, Any]]] = []

    def fake_sync_call(
        sock: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        captured.append((sock, method, params))
        return {"status": "ok"}

    with patch("substrat.rpc.sync_call", fake_sync_call):
        dispatch = daemon_dispatch("/tmp/test.sock", "agent-123")
        result = dispatch("send_message", {"recipient": "bob", "text": "hi"})

    assert result == {"status": "ok"}
    assert len(captured) == 1
    sock, method, params = captured[0]
    assert sock == "/tmp/test.sock"
    assert method == "tool.call"
    assert params["agent_id"] == "agent-123"
    assert params["tool"] == "send_message"
    assert params["arguments"] == {"recipient": "bob", "text": "hi"}
