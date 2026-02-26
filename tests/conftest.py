"""Shared test configuration."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run e2e tests that hit real APIs.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-e2e"):
        return
    skip = pytest.mark.skip(reason="needs --run-e2e flag")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)
