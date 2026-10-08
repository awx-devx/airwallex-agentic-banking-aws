"""Fixtures for kit 4 tests. Shared helpers live in k4_harness.py (unique name, so it
cannot collide with another package's test helpers under --import-mode=importlib)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from k4_harness import World


@pytest.fixture
def port() -> World:
    """The real spine over FakeAirwallex (named `port` for the scenario tests)."""
    return World()


@pytest.fixture
def strong_case(port: World) -> str:
    return port.add_dispute("dsp_strong", amount="900.00", reason={"code": "10.4"}, stage="RFI")


@pytest.fixture
def small_case(port: World) -> str:
    return port.add_dispute(
        "dsp_small", amount="15.00", reason={"code": "13.1"}, stage="CHARGEBACK"
    )


@pytest.fixture
def credit_case(port: World) -> str:
    return port.add_dispute("dsp_credit", amount="180.00", reason={"code": "13.6"}, stage="RFI")
