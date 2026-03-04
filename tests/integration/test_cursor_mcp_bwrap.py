# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""E2e test: MCP tool call via cursor-agent inside a bwrap sandbox."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from substrat.workspace.bwrap import build_command, check_available
from substrat.workspace.model import LinkSpec, Workspace

pytestmark = pytest.mark.e2e

_SKIP_NO_BWRAP = pytest.mark.skipif(
    check_available() is None, reason="bwrap unavailable"
)
_SKIP_NO_CURSOR = pytest.mark.skipif(
    shutil.which("cursor-agent") is None, reason="cursor-agent not in PATH"
)

_MCP_SERVER = Path(__file__).resolve().parent.parent / "fixtures" / "mcp_add_server.py"


def _cursor_binds(cache_dir: Path) -> list[LinkSpec]:
    """Bind mounts needed to run cursor-agent inside bwrap."""
    home = Path.home()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return [
        LinkSpec(home / ".local", home / ".local", "ro"),
        LinkSpec(home / ".cursor", home / ".cursor", "rw"),
        LinkSpec(home / ".config" / "cursor", home / ".config" / "cursor", "ro"),
        LinkSpec(cache_dir, home / ".cache" / "cursor-compile-cache", "rw"),
    ]


def _setup_mcp(workspace_root: Path) -> None:
    """Copy the MCP test server into the workspace and write .cursor/mcp.json."""
    server_dest = workspace_root / "mcp_add_server.py"
    shutil.copy2(_MCP_SERVER, server_dest)
    server_dest.chmod(0o755)

    cursor_dir = workspace_root / ".cursor"
    cursor_dir.mkdir(exist_ok=True)
    config = {
        "mcpServers": {
            "add-server": {
                "command": "python3",
                "args": [str(server_dest)],
            }
        }
    }
    (cursor_dir / "mcp.json").write_text(json.dumps(config))


@_SKIP_NO_BWRAP
@_SKIP_NO_CURSOR
@pytest.mark.asyncio
async def test_mcp_tool_call_inside_bwrap(tmp_path: Path) -> None:
    home = Path.home()
    workspace = Workspace(
        name="mcp-bwrap-e2e",
        scope=__import__("uuid").uuid4(),
        root_path=tmp_path / "root",
        network_access=True,
    )
    workspace.root_path.mkdir()

    _setup_mcp(workspace.root_path)

    binds = _cursor_binds(tmp_path / ".cache")
    inner_cmd = [
        "cursor-agent",
        "--print",
        "--output-format",
        "stream-json",
        "--trust",
        "--approve-mcps",
        "--model",
        "sonnet-4.6",
        "--workspace",
        str(workspace.root_path),
        "Use the add tool to compute 2 + 3. Report the exact result.",
    ]
    cmd = build_command(workspace, binds, command=inner_cmd, env={"HOME": str(home)})

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None

    # Collect final assistant text blocks from stream-json output.
    text_parts: list[str] = []
    async for line in proc.stdout:
        decoded = line.decode().strip()
        if not decoded:
            continue
        try:
            event = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "assistant" and "timestamp_ms" not in event:
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])
        if etype == "result" and event.get("is_error"):
            pytest.fail(f"cursor-agent error: {event.get('result')}")

    await proc.wait()
    response = "".join(text_parts)
    assert "5" in response, f"expected '5' in response: {response!r}"
