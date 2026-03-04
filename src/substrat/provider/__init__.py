# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from substrat.provider.base import AgentProvider, ProviderSession
from substrat.provider.mcp_server import McpServer, ToolDispatch, direct_dispatch

if TYPE_CHECKING:
    from substrat.model import ToolDef

__all__ = [
    "AgentProvider",
    "McpServer",
    "ProviderSession",
    "ToolDispatch",
    "default_providers",
    "direct_dispatch",
]


def default_providers(
    tools: Sequence[ToolDef] = (),
) -> dict[str, AgentProvider]:
    """Build the default provider dict for the daemon."""
    from substrat.provider.cursor_agent import CursorAgentProvider

    return {"cursor-agent": CursorAgentProvider(tools=tools, use_mcp=False)}
