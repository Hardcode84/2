# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Persist shell state (env vars, cwd) across bwrap invocations.

Each agent tool call is a separate bwrap process — env vars and cwd die
with it.  Two scripts collaborate to fix this:

- ``wrap.sh`` runs around every command: saves a baseline env snapshot,
  restores previously captured state, runs the command, saves cwd.
- ``capture_env.sh`` is called explicitly by the agent after env-modifying
  commands (``source activate && .substrat/capture_env.sh``).  It diffs
  current env against the baseline and writes the delta to ``.substrat/env``.

Both live in the workspace root under ``.substrat/`` (bind-mounted RW).
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

# wrap.sh — runs inside bwrap around the real command.
#
# 1. Save baseline env to .substrat/baseline_env (before restore).
# 2. Restore: source .substrat/env, cd to .substrat/cwd.
# 3. eval "$1".
# 4. Save cwd.
#
# Env delta capture is NOT done here — it's the capture script's job.
# The baseline file is left on disk for capture_env.sh to diff against.
WRAPPER_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail

# Absolute path — survives cd during restore/eval.
_substrat_dir="$(pwd)/.substrat"
_substrat_env="${_substrat_dir}/env"
_substrat_cwd="${_substrat_dir}/cwd"
_substrat_baseline="${_substrat_dir}/baseline_env"

# --- baseline snapshot (before restore) ---
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
set +e
eval "$1"
_substrat_rc=$?
set -e

# --- save cwd ---
mkdir -p "$_substrat_dir"
pwd > "$_substrat_cwd"

exit "$_substrat_rc"
"""

# capture_env.sh — called explicitly by the agent.
#
# Diffs current env against the baseline saved by wrap.sh, writes the
# delta as export statements to .substrat/env.  Also saves cwd.
#
# Usage:  source .venv/bin/activate && .substrat/capture_env.sh
#         cd /project && .substrat/capture_env.sh
CAPTURE_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail

_substrat_dir="$(dirname "$(readlink -f "$0")")"
_substrat_baseline="${_substrat_dir}/baseline_env"
_substrat_env="${_substrat_dir}/env"

if [[ ! -f "$_substrat_baseline" ]]; then
    echo "error: no baseline — run inside bwrap wrapper first" >&2
    exit 1
fi

_substrat_current="$(mktemp)"
env | sort > "$_substrat_current"

{
    comm -13 "$_substrat_baseline" "$_substrat_current" \
        | { grep -v '^_substrat_\|^_=\|^SHLVL=' || true; } \
        | while IFS='=' read -r _k _v; do
            [[ -n "$_k" ]] \
                && printf 'export %s=%q\n' "$_k" "$_v"
        done
} > "$_substrat_env"

pwd > "${_substrat_dir}/cwd"

rm -f "$_substrat_current"
"""


_SCRIPTS: dict[str, str] = {
    "wrap.sh": WRAPPER_SCRIPT,
    "capture_env.sh": CAPTURE_SCRIPT,
}


def ensure_wrapper(ws_root: Path) -> None:
    """Write ``.substrat/wrap.sh`` and ``capture_env.sh`` if outdated."""
    dest_dir = ws_root / ".substrat"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, content in _SCRIPTS.items():
        dest = dest_dir / name
        if dest.exists() and dest.read_text() == content:
            continue
        dest.write_text(content)
        dest.chmod(0o755)


def wrap_command(cmd: Sequence[str]) -> list[str]:
    """Wrap a command argv for shell state persistence.

    The command is shlex-joined into a single string and passed as ``$1``
    to the wrapper, which ``eval``s it.
    """
    return ["bash", ".substrat/wrap.sh", shlex.join(cmd)]
