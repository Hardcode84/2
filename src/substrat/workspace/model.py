# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Workspace data model. Stateless resources — no state machine."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from substrat import now_iso
from substrat.model import LinkSpec

__all__ = ["LinkSpec", "Workspace"]


@dataclass
class Workspace:
    """A sandboxed filesystem environment. Knows nothing about agents."""

    name: str  # Local name (unique within scope).
    scope: UUID  # Creator agent/user ID. Frozen.
    root_path: Path  # Host-side backing directory.
    network_access: bool = False
    links: list[LinkSpec] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
