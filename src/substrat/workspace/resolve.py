# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Workspace name resolution and scope-based access predicates."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

from substrat.model import USER

_DOT_SEGMENTS = frozenset({".", ".."})


def _reject_dots(segment: str, ref: str) -> None:
    """Reject '.' and '..' where they don't belong."""
    if segment in _DOT_SEGMENTS:
        raise ValueError(f"invalid path segment {segment!r} in {ref!r}")


def resolve(
    caller_id: UUID,
    ref: str,
    *,
    parent_id: UUID | None,
    child_lookup: Callable[[str], UUID],
) -> tuple[UUID, str]:
    """Resolve a workspace reference to (scope, local_name).

    Three forms:
      "my-ws"          -> own scope.
      "../shared"       -> parent scope (USER for roots).
      "worker/output"  -> child scope.

    Raises ValueError on malformed references, KeyError if the named
    child does not exist.
    """
    if not ref:
        raise ValueError("empty workspace reference")

    if "/" not in ref and not ref.startswith(".."):
        _reject_dots(ref, ref)
        return (caller_id, ref)

    parts = ref.split("/")

    if len(parts) != 2:
        raise ValueError(f"malformed workspace reference {ref!r}")

    head, name = parts

    if not name:
        raise ValueError(f"missing workspace name in {ref!r}")
    _reject_dots(name, ref)

    if head == "..":
        scope = parent_id if parent_id is not None else USER
        return (scope, name)

    _reject_dots(head, ref)
    # Child reference.
    child_id = child_lookup(head)
    return (child_id, name)


def visible_scopes(
    caller_id: UUID,
    children: Sequence[UUID],
    parent_id: UUID | None,
) -> set[UUID]:
    """Scopes the caller can read workspaces from.

    Own + children + parent (or USER for roots).
    """
    scopes: set[UUID] = {caller_id}
    scopes.update(children)
    scopes.add(parent_id if parent_id is not None else USER)
    return scopes


def mutable_scopes(
    caller_id: UUID,
    children: Sequence[UUID],
) -> set[UUID]:
    """Scopes the caller can create/modify/delete workspaces in.

    Own + children. Parent scope is visible but read-only.
    """
    scopes: set[UUID] = {caller_id}
    scopes.update(children)
    return scopes
