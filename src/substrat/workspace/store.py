# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Persistent workspace store backed by per-scope JSON files."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from uuid import UUID

from substrat.persistence import atomic_write
from substrat.workspace.model import LinkSpec, Workspace

_META_FILE = "meta.json"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def validate_name(name: str) -> None:
    """Reject names that would escape the directory layout."""
    if not _NAME_RE.match(name):
        msg = (
            f"invalid workspace name {name!r}: "
            "must be alphanumeric, hyphens, underscores, "
            "and start with an alphanumeric character."
        )
        raise ValueError(msg)


class WorkspaceStore:
    """Thin I/O layer for workspace records. No in-memory cache."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def workspace_dir(self, scope: UUID, name: str) -> Path:
        """Return root/<scope-hex>/<name>/ for the given workspace."""
        return self._root / scope.hex / name

    def save(self, ws: Workspace) -> None:
        """Atomically write meta.json. Creates backing dir on first save."""
        validate_name(ws.name)
        d = self.workspace_dir(ws.scope, ws.name)
        atomic_write(d / _META_FILE, self._serialize(ws))
        backing = d / "root"
        backing.mkdir(parents=True, exist_ok=True)

    def load(self, scope: UUID, name: str) -> Workspace:
        """Load one workspace record. Raises FileNotFoundError if missing."""
        validate_name(name)
        path = self.workspace_dir(scope, name) / _META_FILE
        return self._deserialize(path.read_bytes())

    def scan(self) -> list[Workspace]:
        """Load all workspace records under root."""
        if not self._root.is_dir():
            return []
        workspaces: list[Workspace] = []
        for scope_dir in sorted(self._root.iterdir()):
            if not scope_dir.is_dir():
                continue
            for ws_dir in sorted(scope_dir.iterdir()):
                meta = ws_dir / _META_FILE
                if meta.is_file():
                    workspaces.append(self._deserialize(meta.read_bytes()))
        return workspaces

    def delete(self, scope: UUID, name: str) -> None:
        """Remove the entire workspace directory tree."""
        validate_name(name)
        d = self.workspace_dir(scope, name)
        if not d.is_dir():
            raise FileNotFoundError(d)
        shutil.rmtree(d)

    def exists(self, scope: UUID, name: str) -> bool:
        """Check whether a workspace's meta.json exists."""
        validate_name(name)
        return (self.workspace_dir(scope, name) / _META_FILE).is_file()

    @staticmethod
    def _serialize(ws: Workspace) -> bytes:
        """Workspace -> JSON bytes."""
        obj = {
            "name": ws.name,
            "scope": ws.scope.hex,
            "root_path": str(ws.root_path),
            "network_access": ws.network_access,
            "links": [
                {
                    "host_path": str(link.host_path),
                    "mount_path": str(link.mount_path),
                    "mode": link.mode,
                }
                for link in ws.links
            ],
            "created_at": ws.created_at,
        }
        return json.dumps(obj, indent=2).encode()

    @staticmethod
    def _deserialize(data: bytes) -> Workspace:
        """JSON bytes -> Workspace."""
        obj = json.loads(data)
        return Workspace(
            name=obj["name"],
            scope=UUID(obj["scope"]),
            root_path=Path(obj["root_path"]),
            network_access=obj["network_access"],
            links=[
                LinkSpec(
                    host_path=Path(link["host_path"]),
                    mount_path=Path(link["mount_path"]),
                    mode=link["mode"],
                )
                for link in obj["links"]
            ],
            created_at=obj["created_at"],
        )


def _is_view_of(candidate: Workspace, source: Workspace) -> bool:
    """True if any of candidate's links point into source's root_path."""
    src = source.root_path.resolve()
    for link in candidate.links:
        try:
            link.host_path.resolve().relative_to(src)
            return True
        except ValueError:
            continue
    return False


def view_tree(
    root_scope: UUID,
    root_name: str,
    store: WorkspaceStore,
) -> list[Workspace]:
    """Discover the full view tree rooted at (scope, name).

    Returns all workspaces that are (transitively) views of the root,
    NOT including the root itself. Uses BFS over all workspaces in
    the store.
    """
    root_ws = store.load(root_scope, root_name)
    all_ws = store.scan()

    # Index by key for dedup.
    found: dict[tuple[UUID, str], Workspace] = {}
    # BFS queue: workspaces whose dependents we haven't checked yet.
    queue = [root_ws]
    while queue:
        source = queue.pop(0)
        for ws in all_ws:
            key = (ws.scope, ws.name)
            if key == (root_scope, root_name):
                continue
            if key in found:
                continue
            if _is_view_of(ws, source):
                found[key] = ws
                queue.append(ws)
    return list(found.values())
