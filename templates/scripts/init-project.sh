#!/usr/bin/env bash
# Create a project workspace linked to a repo and tell the root agent about it.
set -euo pipefail

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
ws="${project}-ws"
substrat workspace create "$ws" --network
scope="$(substrat workspace list | grep "$ws" | awk '{print $1}' | cut -d/ -f1)"
substrat workspace link "$ws" "$scope" --source "$repo" --target /repo --mode rw

echo "workspace '$ws' ready (repo: $repo)"
echo "send a task: substrat agent send $root \"<task> in $project\""
