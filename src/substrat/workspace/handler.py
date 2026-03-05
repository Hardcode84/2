# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Workspace tool logic — lives in the workspace layer, no agent imports."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from substrat.model import ToolDef, ToolParam, tool_error
from substrat.workspace.mapping import WorkspaceMapping
from substrat.workspace.model import LinkSpec, Workspace
from substrat.workspace.resolve import (
    mutable_scopes,
    resolve,
    visible_scopes,
)
from substrat.workspace.store import WorkspaceStore, validate_name, view_tree

# -- Workspace tool catalog -------------------------------------------------

WORKSPACE_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        "list_workspaces",
        "List visible workspaces (own, children's, parent's scopes).",
    ),
    ToolDef(
        "create_workspace",
        "Create a workspace in the calling agent's scope.",
        (
            ToolParam("name", "string", "Workspace name."),
            ToolParam(
                "network_access",
                "boolean",
                "Allow network access inside the sandbox.",
                required=False,
                default=False,
            ),
            ToolParam(
                "view_of",
                "string",
                "Source workspace ref for live view.",
                required=False,
            ),
            ToolParam(
                "subdir",
                "string",
                "Subfolder within source (view_of only).",
                required=False,
                default=".",
            ),
            ToolParam(
                "mode",
                "string",
                "View mode: ro or rw (view_of only).",
                required=False,
                default="ro",
            ),
        ),
    ),
    ToolDef(
        "delete_workspace",
        "Delete a workspace. Must be in a mutable scope.",
        (ToolParam("name", "string", "Workspace ref (scoped)."),),
    ),
    ToolDef(
        "link_dir",
        "Link a directory into a workspace.",
        (
            ToolParam("workspace", "string", "Target workspace ref (scoped)."),
            ToolParam("source", "string", "Path inside caller's own workspace."),
            ToolParam("target", "string", "Mount path inside target workspace."),
            ToolParam(
                "mode",
                "string",
                "Bind mode: ro or rw.",
                required=False,
                default="ro",
            ),
        ),
    ),
    ToolDef(
        "unlink_dir",
        "Remove a linked directory from a workspace.",
        (
            ToolParam("workspace", "string", "Workspace ref (scoped)."),
            ToolParam("target", "string", "Mount path to remove."),
        ),
    ),
)

# Fresh-read callback: returns (parent_id, children, child_lookup).
ResolveCtx = Callable[[], tuple[UUID | None, Sequence[UUID], Callable[[str], UUID]]]

# Maps a scope UUID to a display label ("self", "parent", child name).
ScopeNamer = Callable[[UUID], str]


class WorkspaceToolHandler:
    """Per-agent workspace tool handler.

    All five workspace CRUD methods plus spawn-time validation.
    No agent-layer imports — tree access is via injected closures.
    """

    def __init__(
        self,
        store: WorkspaceStore,
        mapping: WorkspaceMapping,
        caller_id: UUID,
        resolve_ctx: ResolveCtx,
        scope_namer: ScopeNamer,
    ) -> None:
        self._store = store
        self._mapping = mapping
        self._caller_id = caller_id
        self._resolve_ctx = resolve_ctx
        self._scope_namer = scope_namer

    # --- Public tools ---

    def list_workspaces(self) -> dict[str, Any]:
        """List workspaces visible to the caller."""
        parent_id, children, _child_lookup = self._resolve_ctx()
        vis = visible_scopes(self._caller_id, children, parent_id)
        mut = mutable_scopes(self._caller_id, children)
        workspaces = [
            {
                "name": ws.name,
                "scope": self._scope_namer(ws.scope),
                "mutable": ws.scope in mut,
            }
            for ws in self._store.scan()
            if ws.scope in vis
        ]
        return {"workspaces": workspaces}

    def create_workspace(
        self,
        name: str,
        *,
        network_access: bool = False,
        view_of: str | None = None,
        subdir: str = ".",
        mode: Literal["ro", "rw"] = "ro",
    ) -> dict[str, Any]:
        """Create a workspace in the caller's own scope."""
        try:
            validate_name(name)
        except ValueError as exc:
            return tool_error(str(exc))
        parent_id, children, child_lookup = self._resolve_ctx()
        scope = self._caller_id
        if self._store.exists(scope, name):
            return tool_error(f"workspace {name!r} already exists in own scope")
        links: list[LinkSpec] = []
        if view_of is not None:
            try:
                src_scope, src_name = resolve(
                    self._caller_id,
                    view_of,
                    parent_id=parent_id,
                    child_lookup=child_lookup,
                )
            except (ValueError, KeyError) as exc:
                return tool_error(str(exc))
            vis = visible_scopes(self._caller_id, children, parent_id)
            if src_scope not in vis:
                return tool_error(f"workspace {view_of!r} not visible")
            try:
                src_ws = self._store.load(src_scope, src_name)
            except FileNotFoundError:
                return tool_error(f"workspace {view_of!r} not found")
            host_path = src_ws.root_path / subdir
            links.append(LinkSpec(host_path=host_path, mount_path=Path("."), mode=mode))
        ws_dir = self._store.workspace_dir(scope, name) / "root"
        ws = Workspace(
            name=name,
            scope=scope,
            root_path=ws_dir,
            network_access=network_access,
            links=links,
        )
        self._store.save(ws)
        return {"status": "created", "name": name}

    def delete_workspace(self, name: str) -> dict[str, Any]:
        """Delete a workspace and its entire view tree.

        Fails if any workspace in the tree has assigned agents.
        """
        parent_id, _children, child_lookup = self._resolve_ctx()
        try:
            scope, local_name = resolve(
                self._caller_id,
                name,
                parent_id=parent_id,
                child_lookup=child_lookup,
            )
        except (ValueError, KeyError) as exc:
            return tool_error(str(exc))
        _, children, _ = self._resolve_ctx()
        mut = mutable_scopes(self._caller_id, children)
        if scope not in mut:
            return tool_error(f"workspace {name!r} is not in a mutable scope")
        if not self._store.exists(scope, local_name):
            return tool_error(f"workspace {name!r} not found")
        # Collect the full view tree (transitive views).
        views = view_tree(scope, local_name, self._store)
        # Check agents on root + all views before deleting anything.
        all_targets = [(scope, local_name)] + [(v.scope, v.name) for v in views]
        for s, n in all_targets:
            agents = self._mapping.agents_in(s, n)
            if agents:
                label = f"{s.hex[:8]}/{n}" if (s, n) != (scope, local_name) else name
                return tool_error(
                    f"workspace {label!r} has {len(agents)} assigned agent(s)"
                )
        # Delete views first (leaves), then root.
        for v in reversed(views):
            self._store.delete(v.scope, v.name)
        self._store.delete(scope, local_name)
        return {"status": "deleted"}

    def link_dir(
        self,
        workspace: str,
        source: str,
        target: str,
        *,
        mode: Literal["ro", "rw"] = "ro",
    ) -> dict[str, Any]:
        """Link a directory from caller's workspace into a target workspace."""
        parent_id, children, child_lookup = self._resolve_ctx()
        # Caller must have a workspace assigned.
        caller_ws_key = self._mapping.get(self._caller_id)
        if caller_ws_key is None:
            return tool_error("caller has no workspace assigned")
        caller_ws = self._store.load(*caller_ws_key)
        host_path = caller_ws.root_path / source
        if not host_path.exists():
            return tool_error(f"source path {source!r} does not exist")
        # Resolve target workspace.
        try:
            scope, local_name = resolve(
                self._caller_id,
                workspace,
                parent_id=parent_id,
                child_lookup=child_lookup,
            )
        except (ValueError, KeyError) as exc:
            return tool_error(str(exc))
        mut = mutable_scopes(self._caller_id, children)
        if scope not in mut:
            return tool_error(f"workspace {workspace!r} is not in a mutable scope")
        try:
            target_ws = self._store.load(scope, local_name)
        except FileNotFoundError:
            return tool_error(f"workspace {workspace!r} not found")
        target_ws.links.append(
            LinkSpec(host_path=host_path, mount_path=Path(target), mode=mode)
        )
        self._store.save(target_ws)
        return {"status": "linked"}

    def unlink_dir(self, workspace: str, target: str) -> dict[str, Any]:
        """Remove a linked directory from a workspace."""
        parent_id, children, child_lookup = self._resolve_ctx()
        try:
            scope, local_name = resolve(
                self._caller_id,
                workspace,
                parent_id=parent_id,
                child_lookup=child_lookup,
            )
        except (ValueError, KeyError) as exc:
            return tool_error(str(exc))
        mut = mutable_scopes(self._caller_id, children)
        if scope not in mut:
            return tool_error(f"workspace {workspace!r} is not in a mutable scope")
        try:
            ws = self._store.load(scope, local_name)
        except FileNotFoundError:
            return tool_error(f"workspace {workspace!r} not found")
        mount = Path(target)
        for i, link in enumerate(ws.links):
            if link.mount_path == mount:
                ws.links.pop(i)
                self._store.save(ws)
                return {"status": "unlinked"}
        return tool_error(f"no link at {target!r}")

    def validate_ref(self, ref: str) -> tuple[UUID, str]:
        """Resolve a workspace ref, check visibility + existence.

        Returns (scope, ws_name) on success. Raises ValueError/KeyError on
        failure — caller converts to error dict.
        """
        parent_id, children, child_lookup = self._resolve_ctx()
        scope, ws_name = resolve(
            self._caller_id,
            ref,
            parent_id=parent_id,
            child_lookup=child_lookup,
        )
        vis = visible_scopes(self._caller_id, children, parent_id)
        if scope not in vis:
            raise ValueError(f"workspace {ref!r} not visible")
        if not self._store.exists(scope, ws_name):
            raise KeyError(f"workspace {ref!r} not found")
        return (scope, ws_name)
