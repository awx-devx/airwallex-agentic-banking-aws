from decimal import Decimal

import pytest
from awx_common import Money
from awx_k2_purchase.amounts import parse_money
from awx_k2_purchase.controls import (
    CardControls,
    DeclineReason,
    Interval,
    SpendLimit,
    SpendSoFar,
    control_violations,
)
from hypothesis import given
from hypothesis import strategies as st

MCC = ("5734", "7372")


def usd(x) -> Money:
    return Money.of(x, "USD")


def controls(per="100.00", monthly="100.00", all_time="300.00", **kw) -> CardControls:
    limits = [SpendLimit(Interval.PER_TRANSACTION, usd(per))]
    if monthly is not None:
        limits.append(SpendLimit(Interval.MONTHLY, usd(monthly)))
    if all_time is not None:
        limits.append(SpendLimit(Interval.ALL_TIME, usd(all_time)))
    return CardControls("USD", kw.pop("mccs", MCC), tuple(limits), **kw)


cents = st.integers(min_value=1, max_value=10_000_000)


def test_per_transaction_only_is_refused():
    with pytest.raises(ValueError, match="cumulative MONTHLY or ALL_TIME"):
        controls(monthly=None, all_time=None)


def test_monthly_alone_or_all_time_alone_is_enough():
    assert controls(all_time=None).limit(Interval.MONTHLY) == usd("100.00")
    assert controls(monthly=None).limit(Interval.ALL_TIME) == usd("300.00")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"per": "0"}, "positive"),
        ({"monthly": "50.00"}, "below the per-transaction"),
        ({"mccs": ()}, "merchant category"),
        ({"mccs": ("57",)}, "4-digit"),
    ],
)
def test_invalid_controls(kwargs, message):
    with pytest.raises(ValueError, match=message):
        controls(**kwargs)


def test_limit_in_other_currency_refused():
    with pytest.raises(ValueError, match="card currency"):
        CardControls(
            "USD",
            MCC,
            (
                SpendLimit(Interval.PER_TRANSACTION, usd("1.00")),
                SpendLimit(Interval.ALL_TIME, Money.of("1.00", "EUR")),
            ),
        )


def test_payload_shape_and_round_trip():
    c = controls()
    p = c.to_payload()
    assert p["allowed_currencies"] == ["USD"]
    assert p["allowed_merchant_categories"] == ["5734", "7372"]
    assert p["allowed_transaction_count"] == "MULTIPLE"
    limits = p["transaction_limits"]["limits"]
    assert [x["interval"] for x in limits] == ["PER_TRANSACTION", "MONTHLY", "ALL_TIME"]
    assert all(isinstance(x["amount"], Decimal) for x in limits)
    assert CardControls.from_provider(p) == c
    assert CardControls.from_dict(c.to_dict()) == c


def test_from_provider_fails_closed_on_extra_currency():
    p = controls().to_payload()
    p["allowed_currencies"] = ["USD", "EUR"]
    with pytest.raises(ValueError, match="expected exactly"):
        CardControls.from_provider(p)


def test_parse_money_refuses_float_and_excess_scale():
    with pytest.raises(TypeError):
        parse_money(1.5, "USD")
    with pytest.raises(ValueError, match="plain non-negative decimal"):
        parse_money("1.0050", "USD")
    with pytest.raises(ValueError, match="decimal places"):
        parse_money("1.005", "USD")
    with pytest.raises(ValueError, match="plain non-negative decimal"):
        parse_money("-1", "USD")
    assert parse_money("12", "JPY") == Money.of("12", "JPY")


@given(cents)
def test_per_transaction_limit_is_inclusive(limit_minor):
    limit = Money.from_minor(limit_minor, "USD")
    c = CardControls(
        "USD",
        MCC,
        (SpendLimit(Interval.PER_TRANSACTION, limit), SpendLimit(Interval.ALL_TIME, limit)),
    )
    assert control_violations(c, limit, "5734") == frozenset()
    over = limit + Money.from_minor(1, "USD")
    assert control_violations(c, over, "5734") == {DeclineReason.LIMIT_EXCEEDED}


@given(per=cents, extra=st.integers(0, 10_000_000), spent=st.integers(0, 30_000_000), amt=cents)
def test_cumulative_limit_counts_prior_spend(per, extra, spent, amt):
    per_m = Money.from_minor(per, "USD")
    monthly = Money.from_minor(per + extra, "USD")
    c = CardControls(
        "USD",
        MCC,
        (SpendLimit(Interval.PER_TRANSACTION, per_m), SpendLimit(Interval.MONTHLY, monthly)),
    )
    spent_m = Money.from_minor(spent, "USD")
    amount = Money.from_minor(amt, "USD")
    got = control_violations(c, amount, "5734", SpendSoFar({Interval.MONTHLY: spent_m}))
    expected_over = amount > per_m or spent_m + amount > monthly
    assert (DeclineReason.LIMIT_EXCEEDED in got) == expected_over
    assert got <= {DeclineReason.LIMIT_EXCEEDED}


@given(st.sampled_from(["EUR", "GBP", "AUD"]), cents)
def test_other_currency_fails_on_currency_alone(currency, amt):
    got = control_violations(controls(), Money.from_minor(amt, currency), "5734")
    assert got == {DeclineReason.CURRENCY_NOT_ALLOWED}


@given(st.from_regex(r"^[0-9]{4}$", fullmatch=True).filter(lambda m: m not in MCC))
def test_other_mcc_fails_on_mcc_alone(mcc):
    assert control_violations(controls(), usd("1.00"), mcc) == {
        DeclineReason.MERCHANT_CATEGORY_NOT_ALLOWED
    }


@given(a=cents, b=cents)
def test_reduces_or_equals_matches_limit_order(a, b):
    ca = controls(per=str(Money.from_minor(a, "USD").amount), monthly=None, all_time="1000000")
    cb = controls(per=str(Money.from_minor(b, "USD").amount), monthly=None, all_time="1000000")
    assert ca.reduces_or_equals(cb) == (a <= b)
    assert ca.reduces_or_equals(ca)


def test_adding_an_mcc_is_not_a_reduction():
    wider = controls(mccs=("5734", "7372", "5817"))
    assert not wider.reduces_or_equals(controls())
    assert controls().reduces_or_equals(wider)
