# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the daemon — handler dispatch and lifecycle."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from substrat.daemon import ERR_INVALID, ERR_METHOD, ERR_NOT_FOUND, Daemon
from substrat.model import LinkSpec
from substrat.rpc import RpcError, async_call
from substrat.workspace.model import Workspace

# Reuse FakeProvider from test_orchestrator.
from tests.unit.test_orchestrator import FakeProvider

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture()
def daemon(tmp_path: Path, provider: FakeProvider) -> Daemon:
    return Daemon(
        tmp_path,
        default_provider="fake",
        default_model="test-model",
        providers={"fake": provider},
    )


# -- Handler tests (direct method calls) --------------------------------------


async def test_agent_create(daemon: Daemon) -> None:
    """agent.create returns agent_id and name."""
    await daemon.start()
    try:
        result = await daemon._handle_agent_create(
            {"name": "alpha", "instructions": "do stuff"}
        )
        assert "agent_id" in result
        assert result["name"] == "alpha"
    finally:
        await daemon.stop()


async def test_agent_list_empty(daemon: Daemon) -> None:
    """agent.list returns empty list when no agents exist."""
    await daemon.start()
    try:
        result = await daemon._handle_agent_list({})
        assert result == {"agents": []}
    finally:
        await daemon.stop()


async def test_agent_list_with_agents(daemon: Daemon) -> None:
    """agent.list returns all agents."""
    await daemon.start()
    try:
        await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        await daemon._handle_agent_create({"name": "b", "instructions": "j"})
        result = await daemon._handle_agent_list({})
        names = [a["name"] for a in result["agents"]]
        assert sorted(names) == ["a", "b"]
    finally:
        await daemon.stop()


async def test_agent_send(daemon: Daemon) -> None:
    """agent.send returns provider response."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        result = await daemon._handle_agent_send(
            {"agent_id": created["agent_id"], "message": "hello"}
        )
        assert result["response"] == "ok"
    finally:
        await daemon.stop()


async def test_agent_inspect(daemon: Daemon) -> None:
    """agent.inspect returns state, children, inbox."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        result = await daemon._handle_agent_inspect({"agent_id": created["agent_id"]})
        assert result["name"] == "a"
        assert result["state"] == "idle"
        assert result["children"] == []
        assert result["inbox"] == []
    finally:
        await daemon.stop()


async def test_agent_terminate(daemon: Daemon) -> None:
    """agent.terminate removes agent from tree."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create(
            {"name": "doomed", "instructions": "i"}
        )
        await daemon._handle_agent_terminate({"agent_id": created["agent_id"]})
        result = await daemon._handle_agent_list({})
        assert result["agents"] == []
    finally:
        await daemon.stop()


async def test_tool_call_dispatch(daemon: Daemon) -> None:
    """tool.call dispatches to the agent's ToolHandler method."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        result = await daemon._handle_tool_call(
            {
                "agent_id": created["agent_id"],
                "tool": "check_inbox",
                "arguments": {},
            }
        )
        assert "messages" in result
    finally:
        await daemon.stop()


# -- Full lifecycle ------------------------------------------------------------


async def test_full_lifecycle(daemon: Daemon) -> None:
    """create → list → send → inspect → terminate → list(empty)."""
    await daemon.start()
    try:
        # Create.
        created = await daemon._handle_agent_create(
            {"name": "worker", "instructions": "work hard"}
        )
        aid = created["agent_id"]

        # List.
        agents = (await daemon._handle_agent_list({}))["agents"]
        assert len(agents) == 1
        assert agents[0]["name"] == "worker"

        # Send.
        resp = await daemon._handle_agent_send({"agent_id": aid, "message": "go"})
        assert resp["response"] == "ok"

        # Inspect.
        info = await daemon._handle_agent_inspect({"agent_id": aid})
        assert info["state"] == "idle"

        # Terminate.
        await daemon._handle_agent_terminate({"agent_id": aid})

        # List (empty).
        agents = (await daemon._handle_agent_list({}))["agents"]
        assert agents == []
    finally:
        await daemon.stop()


# -- Error cases ---------------------------------------------------------------


async def test_unknown_method_over_uds(daemon: Daemon) -> None:
    """Unknown RPC method returns ERR_METHOD."""
    await daemon.start()
    try:
        with pytest.raises(RpcError) as exc_info:
            await async_call(str(daemon.socket_path), "bogus.method", {})
        assert exc_info.value.code == ERR_METHOD
    finally:
        await daemon.stop()


async def test_unknown_agent_send(daemon: Daemon) -> None:
    """Send to nonexistent agent raises ERR_NOT_FOUND."""
    await daemon.start()
    try:
        from uuid import uuid4

        with pytest.raises(RpcError) as exc_info:
            await async_call(
                str(daemon.socket_path),
                "agent.send",
                {"agent_id": uuid4().hex, "message": "hi"},
            )
        assert exc_info.value.code == ERR_NOT_FOUND
    finally:
        await daemon.stop()


async def test_terminate_already_terminated(daemon: Daemon) -> None:
    """Terminate nonexistent agent returns error."""
    await daemon.start()
    try:
        from uuid import uuid4

        with pytest.raises(RpcError) as exc_info:
            await async_call(
                str(daemon.socket_path),
                "agent.terminate",
                {"agent_id": uuid4().hex},
            )
        assert exc_info.value.code == ERR_NOT_FOUND
    finally:
        await daemon.stop()


async def test_tool_call_unknown_tool(daemon: Daemon) -> None:
    """tool.call with unknown tool returns ERR_INVALID."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        with pytest.raises(RpcError) as exc_info:
            await async_call(
                str(daemon.socket_path),
                "tool.call",
                {
                    "agent_id": created["agent_id"],
                    "tool": "nonexistent_tool",
                    "arguments": {},
                },
            )
        assert exc_info.value.code == ERR_INVALID
    finally:
        await daemon.stop()


# -- Stale socket cleanup -----------------------------------------------------


async def test_stale_socket_cleanup(tmp_path: Path, provider: FakeProvider) -> None:
    """Dead PID file and orphaned socket are cleaned up on start."""
    root = tmp_path / "stale"
    root.mkdir()
    sock = root / "daemon.sock"
    pid = root / "daemon.pid"

    sock.write_text("stale")
    pid.write_text("999999999")  # Very unlikely to be a real PID.

    d = Daemon(root, providers={"fake": provider}, default_provider="fake")
    await d.start()
    try:
        assert d.socket_path.exists()
    finally:
        await d.stop()


async def test_already_running_raises(tmp_path: Path, provider: FakeProvider) -> None:
    """Starting a daemon when one is already running raises RuntimeError."""
    d1 = Daemon(tmp_path, providers={"fake": provider}, default_provider="fake")
    await d1.start()
    try:
        d2 = Daemon(tmp_path, providers={"fake": provider}, default_provider="fake")
        with pytest.raises(RuntimeError, match="already running"):
            await d2.start()
    finally:
        await d1.stop()


# -- Bug regression tests ------------------------------------------------------


async def test_workspace_tool_dispatch(daemon: Daemon) -> None:
    """Workspace tools are callable through tool.call dispatch."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        result = await daemon._handle_tool_call(
            {
                "agent_id": created["agent_id"],
                "tool": "list_workspaces",
                "arguments": {},
            }
        )
        assert "workspaces" in result
    finally:
        await daemon.stop()


async def test_tool_call_rejects_non_tool_method(daemon: Daemon) -> None:
    """tool.call must reject methods not in ALL_TOOLS (e.g. drain_deferred)."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        with pytest.raises(ValueError, match="unknown tool"):
            await daemon._handle_tool_call(
                {
                    "agent_id": created["agent_id"],
                    "tool": "drain_deferred",
                    "arguments": {},
                }
            )
    finally:
        await daemon.stop()


async def test_malformed_json_returns_error(daemon: Daemon) -> None:
    """Malformed JSON on UDS returns an error envelope, not a silent close."""
    import asyncio
    import json

    await daemon.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
        writer.write(b"not valid json\n")
        writer.write_eof()
        data = await reader.read()
        resp = json.loads(data)
        assert "error" in resp
        assert resp["error"]["code"] == ERR_INVALID
    finally:
        await daemon.stop()


async def test_cleanup_stale_permission_error(
    tmp_path: Path, provider: FakeProvider
) -> None:
    """PermissionError during PID check means process is alive — raise RuntimeError."""
    import os
    from unittest.mock import patch

    root = tmp_path / "perm"
    root.mkdir()
    pid_file = root / "daemon.pid"
    pid_file.write_text("12345")

    d = Daemon(root, providers={"fake": provider}, default_provider="fake")
    with (
        patch.object(os, "kill", side_effect=PermissionError("not yours")),
        pytest.raises(RuntimeError, match="already running"),
    ):
        await d.start()


async def test_agent_state_error_returns_invalid(daemon: Daemon) -> None:
    """AgentStateError from concurrent send maps to ERR_INVALID, not ERR_INTERNAL."""
    await daemon.start()
    try:
        created = await daemon._handle_agent_create({"name": "a", "instructions": "i"})
        aid = created["agent_id"]
        # First send puts agent into BUSY; node.begin_turn() on second would fail.
        # Simulate by calling begin_turn directly before sending via UDS.
        node = daemon.orchestrator.tree.get(__import__("uuid").UUID(aid))
        node.begin_turn()  # IDLE → BUSY.
        with pytest.raises(RpcError) as exc_info:
            await async_call(
                str(daemon.socket_path),
                "agent.send",
                {"agent_id": aid, "message": "hi"},
            )
        assert exc_info.value.code == ERR_INVALID
        node.end_turn()  # Cleanup.
    finally:
        await daemon.stop()


# -- Wrap-command factory ------------------------------------------------------


def test_make_wrap_command_produces_bwrap(
    tmp_path: Path, provider: FakeProvider
) -> None:
    """_make_wrap_command closure produces valid bwrap argv."""
    d = Daemon(tmp_path, providers={"fake": provider}, default_provider="fake")
    scope = uuid4()
    ws = Workspace(
        name="test",
        scope=scope,
        root_path=d._ws_store.workspace_dir(scope, "test") / "root",
    )
    ws.root_path.mkdir(parents=True)
    d._ws_store.save(ws)
    wrapper = d._make_wrap_command(scope, "test")
    result = list(wrapper(["echo", "hi"], [], {}))

    # Should start with bwrap.
    assert result[0] == "bwrap"
    # Socket bind should be present.
    sock_str = str(d.socket_path)
    assert sock_str in result
    # SUBSTRAT_SOCKET env var set.
    env_pairs = []
    for i, tok in enumerate(result):
        if tok == "--setenv":
            env_pairs.append((result[i + 1], result[i + 2]))
    env_dict = dict(env_pairs)
    assert env_dict["SUBSTRAT_SOCKET"] == sock_str
    # Command at the end.
    assert result[-2:] == ["echo", "hi"]


def test_make_wrap_command_merges_extra_binds(
    tmp_path: Path, provider: FakeProvider
) -> None:
    """Extra binds from caller are included in the bwrap command."""
    d = Daemon(tmp_path, providers={"fake": provider}, default_provider="fake")
    scope = uuid4()
    ws = Workspace(
        name="t",
        scope=scope,
        root_path=d._ws_store.workspace_dir(scope, "t") / "root",
    )
    ws.root_path.mkdir(parents=True)
    d._ws_store.save(ws)
    wrapper = d._make_wrap_command(scope, "t")
    extra = [LinkSpec(Path("/opt/foo"), Path("/opt/foo"), "ro")]
    result = list(wrapper(["cmd"], extra, {"MY_VAR": "val"}))
    assert "/opt/foo" in result
    env_pairs = {}
    for i, tok in enumerate(result):
        if tok == "--setenv":
            env_pairs[result[i + 1]] = result[i + 2]
    assert env_pairs["MY_VAR"] == "val"


# -- Workspace RPC handlers ---------------------------------------------------


async def test_workspace_create(daemon: Daemon) -> None:
    """workspace.create persists workspace and returns scope/name."""
    await daemon.start()
    try:
        result = await daemon._handle_workspace_create({"name": "dev"})
        assert result["name"] == "dev"
        assert "scope" in result
        # Workspace appears in list.
        listed = await daemon._handle_workspace_list({})
        names = [ws["name"] for ws in listed["workspaces"]]
        assert "dev" in names
    finally:
        await daemon.stop()


async def test_workspace_create_with_scope(daemon: Daemon) -> None:
    """workspace.create accepts explicit scope."""
    await daemon.start()
    try:
        scope = uuid4().hex
        result = await daemon._handle_workspace_create({"name": "env", "scope": scope})
        assert result["scope"] == scope
        assert result["name"] == "env"
    finally:
        await daemon.stop()


async def test_workspace_list_empty(daemon: Daemon) -> None:
    """workspace.list returns empty when no workspaces exist."""
    await daemon.start()
    try:
        result = await daemon._handle_workspace_list({})
        assert result == {"workspaces": []}
    finally:
        await daemon.stop()


async def test_workspace_delete(daemon: Daemon) -> None:
    """workspace.delete removes workspace from store."""
    await daemon.start()
    try:
        created = await daemon._handle_workspace_create({"name": "doomed"})
        result = await daemon._handle_workspace_delete(
            {"scope": created["scope"], "name": "doomed"}
        )
        assert result["status"] == "deleted"
        # Verify it's gone.
        listed = await daemon._handle_workspace_list({})
        assert listed["workspaces"] == []
    finally:
        await daemon.stop()


async def test_workspace_create_over_uds(daemon: Daemon) -> None:
    """workspace.create works through full UDS path."""
    await daemon.start()
    try:
        result = await async_call(
            str(daemon.socket_path),
            "workspace.create",
            {"name": "uds-ws"},
        )
        assert result["name"] == "uds-ws"
        assert "scope" in result
    finally:
        await daemon.stop()


async def test_workspace_list_over_uds(daemon: Daemon) -> None:
    """workspace.list works through full UDS path."""
    await daemon.start()
    try:
        await async_call(
            str(daemon.socket_path),
            "workspace.create",
            {"name": "ws1"},
        )
        result = await async_call(
            str(daemon.socket_path),
            "workspace.list",
            {},
        )
        names = [ws["name"] for ws in result["workspaces"]]
        assert "ws1" in names
    finally:
        await daemon.stop()


# -- Wake loop lifecycle -------------------------------------------------------


async def test_daemon_start_initializes_wake_loop(daemon: Daemon) -> None:
    """daemon.start() starts the orchestrator wake loop."""
    await daemon.start()
    try:
        assert daemon.orchestrator._wake_task is not None
    finally:
        await daemon.stop()


async def test_daemon_stop_cancels_wake_loop(daemon: Daemon) -> None:
    """daemon.stop() cancels the wake loop task."""
    await daemon.start()
    await daemon.stop()
    assert daemon.orchestrator._wake_task is None


# -- Workspace link/unlink/inspect handlers -----------------------------------


async def test_workspace_link(daemon: Daemon) -> None:
    """workspace.link appends a bind mount to the workspace."""
    await daemon.start()
    try:
        created = await daemon._handle_workspace_create({"name": "dev"})
        scope = created["scope"]
        result = await daemon._handle_workspace_link(
            {
                "scope": scope,
                "name": "dev",
                "host_path": "/opt/data",
                "mount_path": "/mnt/data",
                "mode": "rw",
            }
        )
        assert result["status"] == "linked"
        # Verify link persisted.
        info = await daemon._handle_workspace_inspect({"scope": scope, "name": "dev"})
        assert len(info["links"]) == 1
        assert info["links"][0]["host_path"] == "/opt/data"
        assert info["links"][0]["mount_path"] == "/mnt/data"
        assert info["links"][0]["mode"] == "rw"
    finally:
        await daemon.stop()


async def test_workspace_link_default_mode(daemon: Daemon) -> None:
    """workspace.link defaults to read-only mode."""
    await daemon.start()
    try:
        created = await daemon._handle_workspace_create({"name": "ro"})
        await daemon._handle_workspace_link(
            {
                "scope": created["scope"],
                "name": "ro",
                "host_path": "/src",
                "mount_path": "/src",
            }
        )
        info = await daemon._handle_workspace_inspect(
            {"scope": created["scope"], "name": "ro"}
        )
        assert info["links"][0]["mode"] == "ro"
    finally:
        await daemon.stop()


async def test_workspace_link_not_found(daemon: Daemon) -> None:
    """workspace.link on nonexistent workspace raises KeyError."""
    await daemon.start()
    try:
        with pytest.raises(KeyError, match="workspace not found"):
            await daemon._handle_workspace_link(
                {
                    "scope": uuid4().hex,
                    "name": "ghost",
                    "host_path": "/a",
                    "mount_path": "/b",
                }
            )
    finally:
        await daemon.stop()


async def test_workspace_unlink(daemon: Daemon) -> None:
    """workspace.unlink removes a link by mount_path."""
    await daemon.start()
    try:
        created = await daemon._handle_workspace_create({"name": "ul"})
        scope = created["scope"]
        await daemon._handle_workspace_link(
            {
                "scope": scope,
                "name": "ul",
                "host_path": "/a",
                "mount_path": "/mnt/a",
            }
        )
        result = await daemon._handle_workspace_unlink(
            {"scope": scope, "name": "ul", "mount_path": "/mnt/a"}
        )
        assert result["status"] == "unlinked"
        info = await daemon._handle_workspace_inspect({"scope": scope, "name": "ul"})
        assert info["links"] == []
    finally:
        await daemon.stop()


async def test_workspace_unlink_no_match(daemon: Daemon) -> None:
    """workspace.unlink raises when mount_path not found."""
    await daemon.start()
    try:
        created = await daemon._handle_workspace_create({"name": "nm"})
        with pytest.raises(KeyError, match="no link at mount_path"):
            await daemon._handle_workspace_unlink(
                {
                    "scope": created["scope"],
                    "name": "nm",
                    "mount_path": "/nonexistent",
                }
            )
    finally:
        await daemon.stop()


async def test_workspace_unlink_not_found(daemon: Daemon) -> None:
    """workspace.unlink on nonexistent workspace raises KeyError."""
    await daemon.start()
    try:
        with pytest.raises(KeyError, match="workspace not found"):
            await daemon._handle_workspace_unlink(
                {
                    "scope": uuid4().hex,
                    "name": "nope",
                    "mount_path": "/x",
                }
            )
    finally:
        await daemon.stop()


async def test_workspace_inspect(daemon: Daemon) -> None:
    """workspace.inspect returns full workspace details."""
    await daemon.start()
    try:
        created = await daemon._handle_workspace_create({"name": "look"})
        scope = created["scope"]
        info = await daemon._handle_workspace_inspect({"scope": scope, "name": "look"})
        assert info["name"] == "look"
        assert info["scope"] == scope
        assert "root_path" in info
        assert isinstance(info["network_access"], bool)
        assert "created_at" in info
        assert info["links"] == []
    finally:
        await daemon.stop()


async def test_workspace_inspect_with_links(daemon: Daemon) -> None:
    """workspace.inspect includes link details."""
    await daemon.start()
    try:
        created = await daemon._handle_workspace_create({"name": "lk"})
        scope = created["scope"]
        await daemon._handle_workspace_link(
            {
                "scope": scope,
                "name": "lk",
                "host_path": "/x",
                "mount_path": "/y",
                "mode": "rw",
            }
        )
        await daemon._handle_workspace_link(
            {
                "scope": scope,
                "name": "lk",
                "host_path": "/p",
                "mount_path": "/q",
            }
        )
        info = await daemon._handle_workspace_inspect({"scope": scope, "name": "lk"})
        assert len(info["links"]) == 2
    finally:
        await daemon.stop()


async def test_workspace_inspect_not_found(daemon: Daemon) -> None:
    """workspace.inspect on nonexistent workspace raises KeyError."""
    await daemon.start()
    try:
        with pytest.raises(KeyError, match="workspace not found"):
            await daemon._handle_workspace_inspect(
                {"scope": uuid4().hex, "name": "nope"}
            )
    finally:
        await daemon.stop()
