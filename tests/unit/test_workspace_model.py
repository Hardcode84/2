# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Workspace data model."""

from pathlib import Path
from uuid import uuid4

from substrat.workspace import LinkSpec, Workspace

# --- construction ---


def test_workspace_defaults() -> None:
    ws = Workspace(name="env", scope=uuid4(), root_path=Path("/tmp/ws"))
    assert ws.name == "env"
    assert ws.network_access is False
    assert ws.links == []
    assert ws.created_at != ""


def test_workspace_with_options() -> None:
    scope = uuid4()
    ws = Workspace(
        name="net-env",
        scope=scope,
        root_path=Path("/tmp/ws"),
        network_access=True,
    )
    assert ws.network_access is True
    assert ws.scope == scope


# --- links ---


def test_link_defaults() -> None:
    link = LinkSpec(host_path=Path("/src"), mount_path=Path("src"))
    assert link.mode == "ro"


def test_link_append_and_access() -> None:
    ws = Workspace(name="env", scope=uuid4(), root_path=Path("/tmp/ws"))
    ws.links.append(LinkSpec(host_path=Path("/a"), mount_path=Path("a"), mode="rw"))
    ws.links.append(LinkSpec(host_path=Path("/b"), mount_path=Path("b")))
    assert len(ws.links) == 2
    assert ws.links[0].mode == "rw"
    assert ws.links[1].mode == "ro"
    assert ws.links[0].host_path == Path("/a")
