"""Puts this directory on sys.path so tests can import the kit 6 stub and sample data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest


@pytest.fixture(autouse=True)
def _fake_charges_the_sized_swift_fee(monkeypatch):
    """The live sandbox charged more than EUR 6.21 on a 2,000 EUR SWIFT payout and the
    guide documents EUR 12.85 for 100 to 4,000 EUR, so kit 6 sizes at 12.85 (pricing.py). The
    fake charges the same figure in kit 6's tests; a test that wants another fee sets it."""
    from decimal import Decimal

    from awx_spine import fake as fake_module

    monkeypatch.setitem(fake_module.SWIFT_FEE, "EUR", Decimal("12.85"))
