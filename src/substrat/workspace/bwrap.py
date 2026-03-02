# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Build bwrap command lines from workspace specs. Pure function, no I/O."""

from collections.abc import Sequence

from substrat.workspace.model import LinkSpec, Workspace

SYSTEM_RO_BINDS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/sbin",
    "/etc",
)


def build_command(
    workspace: Workspace,
    binds: Sequence[LinkSpec] = (),
    *,
    command: Sequence[str],
    system_ro_binds: Sequence[str] = SYSTEM_RO_BINDS,
) -> list[str]:
    """Translate workspace + extra binds into a bwrap argv.

    Produces a deterministic command line. Caller is responsible for path
    validation and subprocess execution — this function touches nothing.
    """
    cmd: list[str] = ["bwrap", "--die-with-parent"]

    # Namespace isolation (no --unshare-user: uid mapping not worth it for v1).
    cmd += ["--unshare-pid", "--unshare-uts", "--unshare-ipc"]
    if not workspace.network_access:
        cmd.append("--unshare-net")

    # Pseudo-filesystems.
    cmd += ["--proc", "/proc", "--dev", "/dev"]

    # System directories, read-only at their own paths.
    for path in system_ro_binds:
        cmd += ["--ro-bind", path, path]

    # Workspace root, read-write.
    root = str(workspace.root_path)
    cmd += ["--bind", root, root]

    # Workspace links — mount_path is relative, resolved against root.
    for link in workspace.links:
        flag = "--bind" if link.mode == "rw" else "--ro-bind"
        dest = str(workspace.root_path / link.mount_path)
        cmd += [flag, str(link.host_path), dest]

    # Additional binds — mount_path is absolute, used as-is.
    for link in binds:
        flag = "--bind" if link.mode == "rw" else "--ro-bind"
        cmd += [flag, str(link.host_path), str(link.mount_path)]

    # Working directory and command.
    cmd += ["--chdir", root, "--"]
    cmd.extend(command)
    return cmd
