# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Persist shell state (env vars, cwd) across bwrap invocations.

Each agent tool call is a separate bwrap process — env vars and cwd die
with it.  The wrapper script saves an env delta + cwd after each command
and restores them before the next one.  State files live inside the
workspace root (bind-mounted RW), so they survive across bwrap calls.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

# The wrapper script runs inside bwrap around the real command.
#
# Flow:
#   1. Capture baseline env (before restoring saved state).
#   2. Restore: source .substrat/env exports, cd to saved cwd.
#   3. eval "$1" — runs command in wrapper's shell so env/cwd changes
#      are captured.  Binary commands (cursor-agent) are eval'd too,
#      which just launches them as subprocesses (harmless).
#   4. Save: env delta via comm against baseline → .substrat/env.
#      pwd → .substrat/cwd.  Filter internal _substrat_* vars.
#
# The delta approach means only agent-set vars are persisted — inherited
# daemon/bwrap vars stay out of the snapshot.
WRAPPER_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail

# Absolute path to state dir — survives cd during restore/eval.
_substrat_dir="$(pwd)/.substrat"
_substrat_env="${_substrat_dir}/env"
_substrat_cwd="${_substrat_dir}/cwd"

# --- baseline snapshot (before restore) ---
_substrat_baseline="$(mktemp)"
env | sort > "$_substrat_baseline"

# --- restore saved state ---
if [[ -f "$_substrat_env" ]]; then
    # shellcheck disable=SC1090
    source "$_substrat_env" || true
fi
if [[ -f "$_substrat_cwd" ]]; then
    _substrat_saved_cwd="$(cat "$_substrat_cwd")"
    if [[ -d "$_substrat_saved_cwd" ]]; then
        cd "$_substrat_saved_cwd"
    fi
fi

# --- run the actual command ---
# eval in current shell so env changes (export, cd, source) are captured.
set +e
eval "$1"
_substrat_rc=$?
set -e

# --- save state ---
mkdir -p "$_substrat_dir"

_substrat_current="$(mktemp)"
env | sort > "$_substrat_current"

# Env delta: lines in current but not in baseline.
# Output as export statements, skipping wrapper-internal vars.
{
    comm -13 "$_substrat_baseline" "$_substrat_current" \
        | { grep -v '^_substrat_' || true; } \
        | while IFS='=' read -r _substrat_key _substrat_val; do
            [[ -n "$_substrat_key" ]] \
                && printf 'export %s=%q\n' \
                    "$_substrat_key" "$_substrat_val"
        done
} > "$_substrat_env"

pwd > "$_substrat_cwd"

rm -f "$_substrat_baseline" "$_substrat_current"
exit "$_substrat_rc"
"""


def ensure_wrapper(ws_root: Path) -> None:
    """Write ``.substrat/wrap.sh`` if missing or outdated."""
    dest = ws_root / ".substrat" / "wrap.sh"
    if dest.exists() and dest.read_text() == WRAPPER_SCRIPT:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(WRAPPER_SCRIPT)
    dest.chmod(0o755)


def wrap_command(cmd: Sequence[str]) -> list[str]:
    """Wrap a command argv for shell state persistence.

    The command is shlex-joined into a single string and passed as ``$1``
    to the wrapper, which ``eval``s it.  This lets env/cwd changes from
    shell builtins (export, cd, source) propagate across calls.
    """
    return ["bash", ".substrat/wrap.sh", shlex.join(cmd)]
