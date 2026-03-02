# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Build bwrap command lines from workspace specs and check availability."""

import shutil
import subprocess
from collections.abc import Mapping, Sequence

from substrat.workspace.model import LinkSpec, Workspace

SYSTEM_RO_BINDS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/sbin",
    "/etc",
    "/run",
)


def check_available() -> str | None:
    """Return bwrap version string, or None if unusable.

    Runs /usr/bin/true inside a minimal sandbox to verify namespace
    creation actually works — catches missing suid bits, broken
    installs, and kernels that refuse unprivileged namespaces.
    """
    if shutil.which("bwrap") is None:
        return None
    try:
        # Smoke-test real sandboxing, not just the binary.
        probe = subprocess.run(
            [
                "bwrap",
                "--unshare-pid",
                "--ro-bind",
                "/usr",
                "/usr",
                "--",
                "/usr/bin/true",
            ],
            capture_output=True,
            timeout=5,
        )
        if probe.returncode != 0:
            return None
        # Sandbox works — grab version for callers who want to log it.
        version = subprocess.run(
            ["bwrap", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if version.returncode != 0:
        return None
    return version.stdout.strip()


def build_command(
    workspace: Workspace,
    binds: Sequence[LinkSpec] = (),
    *,
    command: Sequence[str],
    env: Mapping[str, str] = {},
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

    # Environment variables inside the sandbox.
    for key in sorted(env):
        cmd += ["--setenv", key, env[key]]

    # Working directory and command.
    cmd += ["--chdir", root, "--"]
    cmd.extend(command)
    return cmd
