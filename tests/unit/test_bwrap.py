# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the bwrap command builder."""

from pathlib import Path
from uuid import uuid4

from substrat.workspace import LinkSpec, Workspace, build_command


def _make_workspace(**overrides: object) -> Workspace:
    defaults: dict[str, object] = {
        "name": "test-ws",
        "scope": uuid4(),
        "root_path": Path("/tmp/ws"),
    }
    defaults.update(overrides)
    return Workspace(**defaults)  # type: ignore[arg-type]


# --- basic structure ---


def test_basic_structure() -> None:
    ws = _make_workspace()
    cmd = build_command(ws, command=["bash"], system_ro_binds=())
    assert cmd[0] == "bwrap"
    assert "--die-with-parent" in cmd
    assert "--unshare-pid" in cmd
    assert "--unshare-uts" in cmd
    assert "--unshare-ipc" in cmd
    # --proc /proc and --dev /dev present.
    proc_idx = cmd.index("--proc")
    assert cmd[proc_idx + 1] == "/proc"
    dev_idx = cmd.index("--dev")
    assert cmd[dev_idx + 1] == "/dev"
    # Separator and command at the end.
    sep_idx = cmd.index("--")
    assert cmd[sep_idx + 1] == "bash"


# --- network ---


def test_network_blocked() -> None:
    ws = _make_workspace(network_access=False)
    cmd = build_command(ws, command=["true"], system_ro_binds=())
    assert "--unshare-net" in cmd


def test_network_allowed() -> None:
    ws = _make_workspace(network_access=True)
    cmd = build_command(ws, command=["true"], system_ro_binds=())
    assert "--unshare-net" not in cmd


# --- workspace ---


def test_workspace_root_bind() -> None:
    ws = _make_workspace(root_path=Path("/srv/sandbox"))
    cmd = build_command(ws, command=["true"], system_ro_binds=())
    # Root is rw --bind, not --ro-bind.
    idx = cmd.index("--bind")
    assert cmd[idx + 1] == "/srv/sandbox"
    assert cmd[idx + 2] == "/srv/sandbox"


def test_workspace_links() -> None:
    ws = _make_workspace(
        root_path=Path("/tmp/ws"),
        links=[
            LinkSpec(host_path=Path("/data/src"), mount_path=Path("src"), mode="ro"),
            LinkSpec(host_path=Path("/data/out"), mount_path=Path("out"), mode="rw"),
        ],
    )
    cmd = build_command(ws, command=["true"], system_ro_binds=())
    joined = " ".join(cmd)
    # Read-only link resolved relative to root.
    assert "--ro-bind /data/src /tmp/ws/src" in joined
    # Read-write link resolved relative to root.
    assert "--bind /data/out /tmp/ws/out" in joined


# --- extra binds ---


def test_additional_binds() -> None:
    ws = _make_workspace()
    extra = [
        LinkSpec(
            host_path=Path("/home/u/.cursor/chats"),
            mount_path=Path("/home/u/.cursor/chats"),
            mode="rw",
        ),
        LinkSpec(host_path=Path("/etc/ssl"), mount_path=Path("/etc/ssl"), mode="ro"),
    ]
    cmd = build_command(ws, extra, command=["true"], system_ro_binds=())
    joined = " ".join(cmd)
    assert "--bind /home/u/.cursor/chats /home/u/.cursor/chats" in joined
    assert "--ro-bind /etc/ssl /etc/ssl" in joined


# --- system binds ---


def test_system_ro_binds() -> None:
    ws = _make_workspace()
    cmd = build_command(ws, command=["true"], system_ro_binds=("/usr", "/lib"))
    joined = " ".join(cmd)
    assert "--ro-bind /usr /usr" in joined
    assert "--ro-bind /lib /lib" in joined


def test_no_system_binds() -> None:
    ws = _make_workspace()
    cmd = build_command(ws, command=["true"], system_ro_binds=())
    # No --ro-bind should appear at all (no links either).
    assert "--ro-bind" not in cmd


# --- command ---


def test_command_after_separator() -> None:
    ws = _make_workspace()
    cmd = build_command(ws, command=["python", "-c", "print(1)"], system_ro_binds=())
    sep_idx = cmd.index("--")
    assert cmd[sep_idx + 1 :] == ["python", "-c", "print(1)"]
