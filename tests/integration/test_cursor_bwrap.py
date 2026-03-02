# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""E2e test: cursor-agent running inside a bwrap sandbox."""

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


def _cursor_binds(cache_dir: Path) -> list[LinkSpec]:
    """Bind mounts needed to run cursor-agent inside bwrap."""
    home = Path.home()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return [
        LinkSpec(home / ".local", Path("~/.local"), "ro"),
        LinkSpec(home / ".cursor", Path("~/.cursor"), "rw"),
        LinkSpec(home / ".config/cursor", Path("~/.config/cursor"), "ro"),
        LinkSpec(cache_dir, Path("~/.cache/cursor-compile-cache"), "rw"),
    ]


@_SKIP_NO_BWRAP
@_SKIP_NO_CURSOR
@pytest.mark.asyncio
async def test_send_inside_bwrap(tmp_path: Path) -> None:
    home = Path.home()
    workspace = Workspace(
        name="bwrap-e2e",
        scope=__import__("uuid").uuid4(),
        root_path=tmp_path / "root",
        network_access=True,
    )
    workspace.root_path.mkdir()

    binds = _cursor_binds(tmp_path / ".cache")
    inner_cmd = [
        "cursor-agent",
        "--print",
        "--output-format",
        "stream-json",
        "--trust",
        "--model",
        "sonnet-4.6",
        "--workspace",
        str(workspace.root_path),
        "Say exactly: sandboxed",
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
    assert "sandboxed" in response.lower(), f"unexpected response: {response!r}"
