# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Stress test hooks — torture mode settings override."""

import pytest
from hypothesis import settings

# Overnight settings: crank everything up.
_TORTURE_SETTINGS = settings(
    max_examples=50_000,
    stateful_step_count=200,
    deadline=None,
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if not config.getoption("--run-torture"):
        return
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None:
            continue
        current = getattr(cls, "settings", None)
        if isinstance(current, settings):
            cls.settings = _TORTURE_SETTINGS
