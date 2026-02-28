# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared test configuration."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run e2e tests that hit real APIs.",
    )
    parser.addoption(
        "--run-stress",
        action="store_true",
        default=False,
        help="Run long-running stress tests.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    skip_e2e = not config.getoption("--run-e2e")
    skip_stress = not config.getoption("--run-stress")
    for item in items:
        if skip_e2e and "e2e" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="needs --run-e2e flag"))
        if skip_stress and "stress" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="needs --run-stress flag"))
