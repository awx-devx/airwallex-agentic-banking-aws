"""Keep the live sandbox out of a plain `pytest` run.

Tests here carry the `sandbox` marker and call the real Airwallex sandbox. The root pyproject
has no marker filter, so `uv run pytest tests/contract` would hit the sandbox. They run only when
the marker expression asks for them (`make sandbox-test` passes `-m sandbox`) or AWX_SANDBOX=1.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    expr = config.getoption("-m") or ""
    if os.environ.get("AWX_SANDBOX") == "1" or ("sandbox" in expr and "not sandbox" not in expr):
        return
    skip = pytest.mark.skip(reason="live sandbox: run with make sandbox-test or AWX_SANDBOX=1")
    for item in items:
        if item.get_closest_marker("sandbox"):
            item.add_marker(skip)
