#!/usr/bin/env bash
# Create a project workspace linked to a repo and tell the root agent about it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATES="$(dirname "$SCRIPT_DIR")"

root="${1:?usage: init-project.sh <root-agent> <project-name> <repo-path>}"
project="${2:?usage: init-project.sh <root-agent> <project-name> <repo-path>}"
repo="${3:?usage: init-project.sh <root-agent> <project-name> <repo-path>}"

# Resolve to absolute path.
repo="$(cd "$repo" && pwd)"

# Ensure beads is initialised in the repo.
if [ ! -d "$repo/.beads" ]; then
    echo "initialising beads in $repo"
    (cd "$repo" && br init)
fi

# Create workspace with repo linked RW.
# Defaults to USER scope — visible to root agents via "../<name>".
ws="${project}-ws"
substrat workspace create "$ws" --network
substrat workspace link "$ws" USER --source "$repo" --target /repo --mode rw
substrat workspace link "$ws" USER \
    --source "$TEMPLATES" --target /templates --mode ro

echo "workspace '$ws' ready (repo: $repo, templates at /templates)"
echo "send a task: substrat agent send $root \"<task> in $project\""
