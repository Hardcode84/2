# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared data types used across layers."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

# Daemon and CLI get deterministic UUIDs so they serialise cleanly.
SYSTEM: UUID = UUID(int=0)
USER: UUID = UUID(int=1)
_SENTINELS: frozenset[UUID] = frozenset({SYSTEM, USER})


def is_sentinel(agent_id: UUID) -> bool:
    """True for SYSTEM and USER pseudo-identities."""
    return agent_id in _SENTINELS


_SENTINEL_NAMES: dict[UUID, str] = {SYSTEM: "SYSTEM", USER: "USER"}


def sentinel_name(agent_id: UUID) -> str | None:
    """Human-readable name for sentinel UUIDs, or None for real agents."""
    return _SENTINEL_NAMES.get(agent_id)


def tool_error(message: str) -> dict[str, str]:
    """Standard error dict returned by tool handler methods."""
    return {"error": message}


# Sentinel for "no default value" in ToolParam.
_MISSING: Any = object()


@dataclass
class LinkSpec:
    """A bind mount mapping a host directory into a sandbox."""

    host_path: Path
    mount_path: Path
    mode: Literal["ro", "rw"] = "ro"


# Wraps a subprocess command with sandbox binds and environment.
type CommandWrapper = Callable[
    [Sequence[str], Sequence[LinkSpec], Mapping[str, str]],
    Sequence[str],
]


@dataclass(frozen=True)
class ToolParam:
    """One parameter in a tool's input schema."""

    name: str
    type: str
    description: str
    required: bool = True
    default: Any = _MISSING

    @property
    def has_default(self) -> bool:
        """True if an explicit default was provided."""
        return self.default is not _MISSING


@dataclass(frozen=True)
class ToolDef:
    """Structured tool definition. Transport layers convert to wire format."""

    name: str
    description: str
    parameters: tuple[ToolParam, ...] = ()
