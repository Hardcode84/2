# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared data types used across layers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class LinkSpec:
    """A bind mount mapping a host directory into a sandbox."""

    host_path: Path
    mount_path: Path
    mode: Literal["ro", "rw"] = "ro"
