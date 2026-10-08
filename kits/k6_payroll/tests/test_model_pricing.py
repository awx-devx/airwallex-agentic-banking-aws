from decimal import Decimal

import pytest
from awx_common import Money, canonical_json
from awx_k6_payroll.model import PayrollInputError, employer_tenant_id, parse_batch
from awx_k6_payroll.pricing import (
    RateUnusable,
    available_balance,
    payout_cost,
    plan_funding,
    sell_for,
    with_margin,
)
from awx_k6_payroll.workflow import validate_batch, validate_employer
from k6_data import PERIOD, batch, child_raw, de, employer, gb, us


def usd(x):
    return Money.of(x, "USD")


def eur(x):
    return Money.of(x, "EUR")


def test_batch_is_canonical_and_sorted():
    raw = batch()
    raw["employers"].reverse()
    parsed = parse_batch(raw)
    assert [e["employer"] for e in parsed["employers"]] == ["a", "b", "c"]
    assert [c["contractor"] for c in parsed["employers"][1]["contractors"]] == [
        "bea",
        "bernd",
        "bob",
    ]
    assert canonical_json(validate_batch(raw)) == canonical_json(validate_batch(batch()))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda b: b["employers"][0]["contractors"][0]["amount"].update(amount=1.5), "decimal"),
        (lambda b: b["employers"][0]["contractors"][0].update(transfer_method=None), "LOCAL or"),
        (
            lambda b: b["employers"][0]["contractors"][0]["bank"].update(category="checking"),
            "exactly",
        ),
        (
            lambda b: b["employers"][0]["contractors"][0]["address"].update(country_code="FR"),
            "corridor",
        ),
        (lambda b: b["employers"][0]["contractors"].append(us("alice", "1.00")), "twice"),
        (lambda b: b["employers"].append(employer("a", us("zed", "1.00"))), "twice"),
        (lambda b: b.update(pay_period="2026-10-06"), "ISO week"),
        (lambda b: b["employers"][0]["contractors"][0].update(account_id="acct_x"), "unknown"),
        (lambda b: b["employers"][0]["contractors"][0]["amount"].update(amount="0.00"), "positive"),
        (
            lambda b: b["employers"][0]["contractors"][0]["amount"].update(amount="1.001"),
            "decimal places",
        ),
    ],
)
def test_bad_input_is_refused(mutate, message):
    raw = batch()
    mutate(raw)
    with pytest.raises(PayrollInputError, match=message):
        validate_batch(raw)


def test_child_validate_is_idempotent_and_refuses_tampered_pricing():
    child = validate_employer(
        child_raw(employer("a", us("alice", "1200.00"), de("anna", "2000.00")))
    )
    assert validate_employer(child) == child
    tampered = {**child, "pricing": {**child["pricing"], "platform_fee": usd("0.01")}}
    with pytest.raises(PayrollInputError, match="pricing"):
        validate_employer(tampered)


def test_swift_fee_is_added_once_per_swift_payout_before_converting():
    child = validate_employer(
        child_raw(employer("a", us("alice", "100.00"), de("anna", "2000.00"), de("axel", "100.00")))
    )
    eur_need = child["pricing"]["by_currency"]["EUR"]
    assert eur_need["payouts"] == eur("2100.00")
    assert eur_need["swift_count"] == 2
    assert eur_need["swift_fees"] == eur("25.70")  # 2 x 12.85, the guide's figure
    assert eur_need["need"] == eur("2125.70")
    assert child["pricing"]["by_currency"]["USD"]["need"] == usd("100.00")
    assert child["pricing"]["platform_fee"] == usd("16.00")  # 10.00 + 3 x 2.00
    assert payout_cost(child["contractors"][1]) == eur("2012.85")


def test_swift_without_evidenced_fee_and_unknown_funding_currency_block():
    gbp_swift = {**gb("bea", "700.00"), "transfer_method": "SWIFT"}
    with pytest.raises(PayrollInputError, match="corridor"):
        validate_employer(child_raw(employer("b", gbp_swift)))
    child = validate_employer(child_raw(employer("b", us("bob", "1.00"), funding="SGD")))
    assert child["pricing"]["blockers"] == ["no platform fee schedule for funding currency SGD"]


def test_sell_for_reads_the_fixed_pair_as_second_per_first_and_rounds_up():
    # Live: currency_pair is fixed (EURUSD both ways); rate is USD per EUR.
    body = {"currency_pair": "EURUSD", "rate": Decimal("1.127611")}
    assert sell_for(eur("100.00"), "USD", body, rate_key="rate") == usd("112.77")
    inverse = {"currency_pair": "USDEUR", "rate": Decimal("0.9")}
    assert sell_for(eur("100.00"), "USD", inverse, rate_key="rate") == usd("111.12")
    with pytest.raises(RateUnusable, match="currency_pair"):
        sell_for(eur("1.00"), "USD", {"currency_pair": "GBPEUR", "rate": "1"}, rate_key="rate")
    with pytest.raises(RateUnusable, match="float"):
        sell_for(eur("1.00"), "USD", {"currency_pair": "EURUSD", "rate": 1.1}, rate_key="rate")
    assert with_margin(usd("100.00")) == usd("102.00")


def test_us_state_must_be_iso():
    bad = us("sam", "10.00")
    bad["address"]["state"] = "TX"
    with pytest.raises(PayrollInputError, match="ISO"):
        validate_employer(child_raw(employer("a", bad)))


def test_funding_boundary_is_exact():
    child = validate_employer(child_raw(employer("c", us("carl", "800.00"))))
    required = usd("812.00")  # 800 + 10 base + 2 per contractor
    exact = plan_funding(child["pricing"], "USD", available=required, rates={})
    assert exact["required"] == required
    assert exact["shortfall"].is_zero()
    short = plan_funding(child["pricing"], "USD", available=required - usd("0.01"), rates={})
    assert short["shortfall"] == usd("0.01")


def test_available_balance_refuses_floats_and_duplicates():
    assert available_balance([{"currency": "USD", "available_amount": "5.00"}], "USD") == usd("5")
    assert available_balance({"items": []}, "EUR") == eur("0")
    with pytest.raises(RateUnusable):
        available_balance([{"currency": "USD", "available_amount": 5.0}], "USD")
    with pytest.raises(RateUnusable):
        available_balance([{"currency": "USD", "available_amount": "1"}] * 2, "USD")


def test_tenant_id_is_per_employer():
    assert employer_tenant_id("a") != employer_tenant_id("b")
    assert PERIOD in child_raw(employer("a", us("x", "1.00")))["pay_period"]
