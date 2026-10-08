from decimal import ROUND_HALF_UP, Decimal

import pytest
from awx_common import CurrencyMismatch, Money
from hypothesis import given
from hypothesis import strategies as st


def test_of_quantises_to_currency_scale():
    assert str(Money.of("100", "AUD")) == "100.00 AUD"
    assert Money.of("100", "AUD") == Money.of("100.00", "AUD")
    assert str(Money.of(5, "JPY")) == "5 JPY"
    assert Money.of("1.234", "KWD").to_minor() == 1234


def test_too_many_places_raises_unless_rounded_on_purpose():
    with pytest.raises(ValueError, match="decimal places"):
        Money.of("1.005", "AUD")
    assert Money.of("1.005", "AUD", rounding=ROUND_HALF_UP) == Money.of("1.01", "AUD")


@pytest.mark.parametrize("bad", [1.5, True])
def test_float_and_bool_refused(bad):
    with pytest.raises(TypeError):
        Money.of(bad, "AUD")


@pytest.mark.parametrize("bad", ["aud", "AU", "AUDX", "A1D", 123])
def test_bad_currency_refused(bad):
    with pytest.raises(ValueError, match="currency"):
        Money.of("1", bad)


def test_negative_zero_normalised():
    assert Money.of("-0.00", "AUD").to_dict() == {"amount": "0.00", "currency": "AUD"}


def test_minor_units_round_trip():
    assert Money.from_minor(1234, "AUD") == Money.of("12.34", "AUD")
    assert Money.from_minor(-5, "USD").to_minor() == -5
    assert Money.from_minor(700, "KRW") == Money.of("700", "KRW")


def test_arithmetic_and_comparison_need_same_currency():
    a, b = Money.of("10.00", "AUD"), Money.of("2.50", "AUD")
    assert a - b == Money.of("7.50", "AUD")
    assert a + b > a
    assert -b == Money.of("-2.50", "AUD")
    with pytest.raises(CurrencyMismatch):
        a + Money.of("1", "USD")
    with pytest.raises(CurrencyMismatch):
        _ = a < Money.of("1", "USD")


def test_scale_rounds_explicitly():
    fee = Money.of("1000.00", "AUD").scale(Decimal("0.0035"), rounding=ROUND_HALF_UP)
    assert fee == Money.of("3.50", "AUD")
    with pytest.raises(TypeError):
        Money.of("1", "AUD").scale(0.5, rounding=ROUND_HALF_UP)


def test_allocate_ties_go_to_earlier_weight():
    parts = Money.of("0.10", "AUD").allocate([1, 1, 1])
    assert [p.to_minor() for p in parts] == [4, 3, 3]


def test_allocate_rejects_bad_weights():
    with pytest.raises(ValueError, match="at least one"):
        Money.of("1", "AUD").allocate([])
    with pytest.raises(ValueError, match="not all zero"):
        Money.of("1", "AUD").allocate([0, 0])
    with pytest.raises(TypeError):
        Money.of("1", "AUD").allocate([0.5, 0.5])


@given(
    minor=st.integers(min_value=-(10**12), max_value=10**12),
    weights=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=12).filter(
        lambda ws: sum(ws) > 0
    ),
)
def test_allocate_always_sums_exactly(minor, weights):
    total = Money.from_minor(minor, "AUD")
    parts = total.allocate(weights)
    assert len(parts) == len(weights)
    assert sum(p.to_minor() for p in parts) == minor
    for part, weight in zip(parts, weights, strict=True):
        if weight == 0:
            assert part.is_zero()
