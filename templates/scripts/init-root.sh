#!/usr/bin/env bash
# Create the root coordinator agent with a scratch workspace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATES="$(dirname "$SCRIPT_DIR")"

name="${1:?usage: init-root.sh <name>}"

substrat workspace create "${name}-scratch" --network
substrat workspace link "${name}-scratch" USER \
    --source "$TEMPLATES" --target /templates --mode ro
substrat agent create "$name" \
    --instructions "$(cat "$TEMPLATES/root.md")" \
    --workspace "${name}-scratch"

echo "root agent '$name' ready (templates linked at /templates)"
