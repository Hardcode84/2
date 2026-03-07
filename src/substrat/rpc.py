# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""UDS wire protocol — shared by daemon, CLI, and MCP server."""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
from collections.abc import Iterator
from typing import Any


class RpcError(Exception):
    """Remote procedure call returned an error envelope."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_counter = itertools.count(1)


def _make_request(method: str, params: dict[str, Any]) -> bytes:
    """Build a newline-delimited JSON request."""
    req = {"id": f"req-{next(_counter)}", "method": method, "params": params}
    return json.dumps(req).encode() + b"\n"


def _parse_response(data: bytes) -> dict[str, Any]:
    """Parse response, raise RpcError on error envelope."""
    resp = json.loads(data)
    if "error" in resp:
        err = resp["error"]
        raise RpcError(err["code"], err["message"])
    return resp.get("result", {})  # type: ignore[no-any-return]


_DEFAULT_TIMEOUT = 120.0  # Seconds.


def sync_call(
    sock_path: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Synchronous UDS client. One request, one response, close."""
    request = _make_request(method, params)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    return _parse_response(b"".join(chunks))


def sync_stream(
    sock_path: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Iterator[str]:
    """Synchronous streaming UDS client. Yields chunk strings."""
    request = _make_request(method, params)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            data = sock.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                frame = json.loads(line)
                if "error" in frame:
                    err = frame["error"]
                    raise RpcError(err["code"], err["message"])
                if "done" in frame:
                    return
                if "chunk" in frame:
                    yield frame["chunk"]
    finally:
        sock.close()


async def async_call(
    sock_path: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Async UDS client. For integration tests and async callers."""
    request = _make_request(method, params)
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(request)
        writer.write_eof()
        data = await asyncio.wait_for(reader.read(), timeout=timeout)
    finally:
        writer.close()
        await writer.wait_closed()
    return _parse_response(data)
