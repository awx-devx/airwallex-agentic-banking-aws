from decimal import Decimal

import pytest
from awx_common import Money
from awx_k8_settlement.reserve import (
    DEFAULT_POLICY,
    ReservePolicy,
    incident_target,
    is_material_increase,
    rate_target,
    reserve_rate,
    tenure_floor,
)
from hypothesis import given
from hypothesis import strategies as st

P = DEFAULT_POLICY


def usd(x):
    return Money.of(x, "USD")


def test_tenure_floors_give_the_newest_seller_the_highest_floor():
    assert tenure_floor(P, 0) == Decimal("0.20")
    assert tenure_floor(P, 89) == Decimal("0.20")
    assert tenure_floor(P, 90) == Decimal("0.10")
    assert tenure_floor(P, 365) == Decimal("0.05")


def test_rate_uses_refund_history_above_the_floor_and_is_capped():
    assert reserve_rate(P, Decimal("0.04"), 900) == Decimal("0.08")
    assert reserve_rate(P, Decimal("0.01"), 900) == Decimal("0.05")
    assert reserve_rate(P, Decimal("0.9"), 900) == Decimal("0.30")


@pytest.mark.parametrize("bad", [Decimal("-0.01"), Decimal("1.01")])
def test_rate_refuses_out_of_range_refund_rate(bad):
    with pytest.raises(ValueError, match="trailing"):
        reserve_rate(P, bad, 10)


def test_rate_refuses_float():
    with pytest.raises(TypeError):
        reserve_rate(P, 0.02, 10)


def test_rate_target_rounds_up():
    assert rate_target(usd("100.01"), Decimal("0.05")) == usd("5.01")


def test_incident_target_never_lowers_a_reserve():
    assert incident_target(usd(800), usd(3000)) == usd(3000)
    assert incident_target(usd(800), usd(100)) == usd(800)


def test_materiality():
    assert not is_material_increase(P, usd(1000), usd(1000))
    assert not is_material_increase(P, usd(1000), usd(900))
    assert not is_material_increase(P, usd(1000), usd(1200))  # 200 <= 500 and <= 25%
    assert is_material_increase(P, usd(1000), usd(1501))  # > 500
    assert is_material_increase(P, usd(400), usd(600))  # > 25%
    assert is_material_increase(P, usd(0), usd(1))


def test_policy_validation():
    with pytest.raises(ValueError, match="base_floor"):
        ReservePolicy(base_floor=Decimal("0.5"), max_rate=Decimal("0.3"))
    with pytest.raises(TypeError):
        ReservePolicy(max_rate=0.3)


rates = st.decimals(min_value=0, max_value=1, places=4)


@given(rates, st.integers(0, 5000), rates, st.integers(0, 5000))
def test_rate_always_within_bounds_and_newer_never_lower_at_equal_history(r1, d1, r2, d2):
    rate = reserve_rate(P, r1, d1)
    assert P.base_floor <= rate <= P.max_rate
    if r1 == r2 and d1 <= d2:
        assert reserve_rate(P, r1, d1) >= reserve_rate(P, r2, d2)
