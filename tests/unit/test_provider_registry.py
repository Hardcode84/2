# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for default_providers registry function."""

from substrat.provider import AgentProvider, default_providers

# --- default_providers returns expected structure ---


def test_returns_dict_with_cursor_agent_key() -> None:
    result = default_providers()
    assert "cursor-agent" in result


def test_returned_provider_satisfies_protocol() -> None:
    result = default_providers()
    assert isinstance(result["cursor-agent"], AgentProvider)


def test_returned_provider_has_mcp_disabled() -> None:
    result = default_providers()
    provider = result["cursor-agent"]
    assert provider._use_mcp is False  # type: ignore[attr-defined]
