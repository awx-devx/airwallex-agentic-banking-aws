"""Money rules: fees, minimum conversion, rate ceilings, parsing, projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from awx_common import Money
from awx_k1_treasury.book import TransferMethod
from awx_k1_treasury.money_rules import (
    GUIDE_SWIFT_FEES,
    BadProviderData,
    ConversionLeg,
    IndicativeRate,
    PayoutLeg,
    ReserveBreach,
    UnknownFee,
    execution_states,
    min_conversion,
    parse_balances,
    parse_quote,
    parse_rate,
    payout_fee,
    payout_need,
    project,
    reserve_breaches,
    whole_plan_breaches,
)
from hypothesis import assume, given
from hypothesis import strategies as st

cents = st.integers(min_value=1, max_value=10**10)
non_neg_cents = st.integers(min_value=0, max_value=10**10)
rates = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("500"), places=6)
tolerances = st.decimals(min_value=Decimal(0), max_value=Decimal("0.2"), places=4)


def m(minor: int, cur: str = "EUR") -> Money:
    return Money.from_minor(minor, cur)


def test_swift_eur_fee_is_the_sandbox_figure_not_the_guide_figure():
    assert payout_fee(TransferMethod.SWIFT, "EUR") == Money.of("6.21", "EUR")  # [tr-swift-100]
    assert GUIDE_SWIFT_FEES["EUR"] == Money.of("12.85", "EUR")  # reference only
    assert payout_fee(TransferMethod.SWIFT, "EUR", GUIDE_SWIFT_FEES) == Money.of("12.85", "EUR")
    assert payout_fee(TransferMethod.LOCAL, "USD") == Money.zero("USD")
    with pytest.raises(UnknownFee):
        payout_fee(TransferMethod.SWIFT, "GBP")


@given(cents)
def test_payout_need_includes_exactly_one_fee(minor):
    amount = m(minor)
    assert payout_need(amount, TransferMethod.SWIFT) - amount == Money.of("6.21", "EUR")
    assert payout_need(amount, TransferMethod.LOCAL) == amount


@pytest.mark.parametrize("amount", ["1", "100", "1000", "4000"])
def test_every_eur_swift_payout_costs_the_same_flat_fee(amount):
    a = Money.of(amount, "EUR")
    assert payout_need(a, TransferMethod.SWIFT) == a + Money.of("6.21", "EUR")


@given(cents, non_neg_cents, non_neg_cents)
def test_min_conversion_is_the_least_that_keeps_the_floor(need_c, bal_c, floor_c):
    need, bal, floor = m(need_c), m(bal_c), m(floor_c)
    assume(bal >= floor)
    buy = min_conversion(need, bal, floor)
    assert not buy.is_negative()
    assert bal + buy - need >= floor
    if not buy.is_zero():
        one_cent_less = buy - m(1)
        assert bal + one_cent_less - need < floor
        assert bal + buy - need == floor  # lands exactly on the floor


def test_min_conversion_refuses_a_wallet_already_below_floor():
    with pytest.raises(ReserveBreach):
        min_conversion(m(100), m(10), m(50))


@given(cents, rates, tolerances, st.booleans())
def test_max_sell_rounds_up_and_covers_the_tolerance(buy_c, rate, tol, direct):
    buy = m(buy_c, "EUR")
    pair = "EURUSD" if direct else "USDEUR"
    r = IndicativeRate("EUR", "USD", pair, rate)
    per = rate if direct else Decimal(1) / rate
    exact = buy.amount * per * (1 + tol)
    sell = r.max_sell(buy, tol)
    assert sell.currency == "USD"
    assert sell.amount >= exact
    assert sell.amount - Decimal("0.01") < exact


def test_rate_direction_matches_the_sandbox_fixed_pair():
    """FAKE-FIDELITY: currency_pair EURUSD both ways, rate = USD per one EUR."""
    buy_eur = IndicativeRate("EUR", "USD", "EURUSD", Decimal("1.127611"))
    buy_usd = IndicativeRate("USD", "EUR", "EURUSD", Decimal("1.116014"))
    assert buy_eur.max_sell(Money.of("10", "EUR"), Decimal(0)) == Money.of("11.28", "USD")
    assert buy_usd.max_sell(Money.of("11.16", "USD"), Decimal(0)) == Money.of("10.00", "EUR")


def test_rate_direction_is_never_guessed():
    with pytest.raises(BadProviderData):
        IndicativeRate("EUR", "USD", "GBPUSD", Decimal("1.2"))
    with pytest.raises(BadProviderData):
        parse_rate(
            {"buy_currency": "GBP", "sell_currency": "USD", "currency_pair": "GBPUSD", "rate": 1},
            buy_currency="EUR",
            sell_currency="USD",
        )


def test_float_amounts_from_the_provider_are_refused():
    with pytest.raises(BadProviderData, match="float"):
        parse_balances([{"currency": "EUR", "available_amount": 12.5}], ["EUR"])
    with pytest.raises(BadProviderData, match="float"):
        parse_rate(
            {
                "buy_currency": "EUR",
                "sell_currency": "USD",
                "currency_pair": "EURUSD",
                "rate": 1.08,
            },
            buy_currency="EUR",
            sell_currency="USD",
        )


def test_parse_balances_ignores_other_currencies_and_defaults_missing_to_zero():
    body = [
        {"currency": "EUR", "available_amount": "10.50"},
        {"currency": "JPY", "available_amount": 3.14159},  # irrelevant row is not parsed
    ]
    out = parse_balances(body, ["EUR", "GBP"])
    assert out == {"EUR": Money.of("10.50", "EUR"), "GBP": Money.zero("GBP")}
    with pytest.raises(BadProviderData, match="two balance rows"):
        parse_balances([*body, {"currency": "EUR", "available_amount": "1"}], ["EUR"])


def test_parse_quote_requires_an_expiry():
    base = {
        "quote_id": "q",
        "buy_currency": "EUR",
        "sell_currency": "USD",
        "buy_amount": "1",
        "sell_amount": "1.08",
    }
    with pytest.raises(BadProviderData, match="expiry"):
        parse_quote(base)
    q = parse_quote({**base, "valid_to_at": "2026-10-19T08:01:00Z"})
    now = datetime(2026, 10, 19, 8, 0, 30, tzinfo=UTC)
    assert q.usable(now, timedelta(seconds=20))
    assert not q.usable(now, timedelta(seconds=31))


@given(st.integers(min_value=-3600, max_value=3600), st.integers(min_value=1, max_value=600))
def test_quote_usable_iff_margin_remains(offset_s, margin_s):
    base = datetime(2026, 10, 19, tzinfo=UTC)
    q = parse_quote(
        {
            "quote_id": "q",
            "buy_currency": "EUR",
            "sell_currency": "USD",
            "buy_amount": "1",
            "sell_amount": "1",
            "valid_to_at": base.isoformat(),
        }
    )
    now = base - timedelta(seconds=offset_s)
    assert q.usable(now, timedelta(seconds=margin_s)) == (offset_s >= margin_s)


def test_fee_exclusive_plan_passes_per_leg_but_the_whole_plan_check_catches_it():
    """The review's biggest trap: convert just the invoice amount, then the SWIFT fee breaches."""
    balances = {"USD": Money.of("42000", "USD"), "EUR": Money.of("2000", "EUR")}
    floors = {"USD": Money.of("20000", "USD"), "EUR": Money.of("500", "EUR")}
    fee_blind_buy = Money.of("7525.70", "EUR") - (balances["EUR"] - floors["EUR"])
    conv = ConversionLeg(fee_blind_buy, Money.of("7000", "USD"))
    # Leg one alone is fine.
    assert reserve_breaches(project(balances, [conv], []), floors) == ()
    payout = PayoutLeg(
        "ob", "ben", Money.of("7525.70", "EUR"), Money.of("12.85", "EUR"), TransferMethod.SWIFT
    )
    assert reserve_breaches(project(balances, [conv], [payout]), floors) == ("EUR",)


@given(
    st.lists(st.tuples(cents, cents), max_size=3),
    st.lists(st.tuples(cents, st.sampled_from(["EUR", "USD"])), max_size=4),
    non_neg_cents,
    non_neg_cents,
)
def test_start_and_end_bound_every_execution_state(convs, pays, usd_c, eur_c):
    """Conversions run first: the funding wallet only falls and a bought wallet rises then
    falls. So the lowest point of every wallet is its start or its end, and checking the
    current balance plus the whole-plan projection covers every intermediate state."""
    balances = {"USD": m(usd_c, "USD"), "EUR": m(eur_c, "EUR")}
    cl = [ConversionLeg(m(b, "EUR"), m(s, "USD")) for b, s in convs]
    pl = [
        PayoutLeg(f"o{i}", "b", m(p, cur), Money.zero(cur), TransferMethod.LOCAL)
        for i, (p, cur) in enumerate(pays)
    ]
    final = project(balances, cl, pl)
    states = execution_states(balances, cl, pl)
    assert states[-1] == final
    for cur in balances:
        assert min(s[cur] for s in states) == min(states[0][cur], final[cur])


def test_whole_plan_check_looks_at_every_intermediate_state():
    """A conversion that sells a currency a later leg then buys back would dip below its
    floor in between; the end state alone would hide it."""
    floors = {"USD": Money.of("100", "USD"), "EUR": Money.of("0", "EUR")}
    balances = {"USD": Money.of("150", "USD"), "EUR": Money.of("0", "EUR")}
    sell_usd = ConversionLeg(Money.of("90", "EUR"), Money.of("100", "USD"))
    buy_usd = ConversionLeg(Money.of("100", "USD"), Money.of("90", "EUR"))
    assert reserve_breaches(project(balances, [sell_usd, buy_usd], []), floors) == ()
    assert whole_plan_breaches(balances, [sell_usd, buy_usd], [], floors) == ("USD",)


def test_missing_floor_is_a_breach():
    assert reserve_breaches({"JPY": Money.of(5, "JPY")}, {}) == ("JPY",)
