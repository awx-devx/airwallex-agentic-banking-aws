"""Hypothesis properties for kit 6's money rules, through local_runner and FakeAirwallex."""

from decimal import Decimal

from awx_common import Money, canonical_json
from awx_k6_payroll import EMPLOYER_SPEC, PAYROLL_SPEC
from awx_k6_payroll.pricing import SWIFT_FEES
from awx_k6_payroll.workflow import LOCK_SPEC
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from k6_data import PERIOD, de, employer, gb, us
from k6_fake import children, make_env, run

CODES = ["e0", "e1", "e2"]
LINES = {"us": us, "de": de, "gb": gb}
cents = st.integers(min_value=1000, max_value=300_000).map(lambda c: f"{Decimal(c) / 100:.2f}")
balance = st.integers(min_value=0, max_value=1_500_000).map(lambda c: f"{Decimal(c) / 100:.2f}")
SETTINGS = settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def payroll(draw, code):
    kinds = draw(st.lists(st.sampled_from(sorted(LINES)), min_size=1, max_size=3))
    return employer(code, *[LINES[k](f"k{n}", draw(cents)) for n, k in enumerate(kinds)])


@SETTINGS
@given(st.data())
def test_no_employer_is_ever_funded_from_another_employers_balance(data):
    """Each employer's outcome in the batch equals its outcome alone with the same wallet,
    and no write ever names another employer's account."""
    codes = CODES[: data.draw(st.integers(min_value=2, max_value=3))]
    payrolls = {c: data.draw(payroll(c)) for c in codes}
    balances = {c: data.draw(balance) for c in codes}
    env = make_env(**balances)
    raw = {"pay_period": PERIOD, "employers": list(payrolls.values())}
    run(LOCK_SPEC, raw, env)
    out = children(run(PAYROLL_SPEC, raw, env))
    accts = set(env.accts.values())
    for op in env.fake.sent_ops:
        named = op.on_behalf_of or op.body.get("source")
        if named is not None:
            assert not any(o in canonical_json(op.body) for o in accts - {named})
    for code in codes:
        solo_env = make_env(**{code: balances[code]})
        solo_raw = {"pay_period": PERIOD, "employers": [payrolls[code]]}
        run(LOCK_SPEC, solo_raw, solo_env)
        solo = run(PAYROLL_SPEC, solo_raw, solo_env)
        solo_state = children(solo)[code].state if solo.child_outcomes else solo.state
        assert out[code].state == solo_state
        assert out[code].state in ("SUCCEEDED", "BLOCKED"), out[code].detail
        if out[code].state == "BLOCKED":
            assert env.money_ops(acct=env.accts[code]) == []
            assert any("SHORTFALL" in r for r in out[code].proposal.reasons)


@SETTINGS
@given(payroll("e0"))
def test_per_employer_totals_reconcile_exactly(emp):
    raw = {"pay_period": PERIOD, "employers": [emp]}
    start = Decimal("1000000")
    env = make_env(e0=str(start))
    run(LOCK_SPEC, raw, env)
    out = run(PAYROLL_SPEC, raw, env)
    child = children(out)["e0"]
    assert child.state == "SUCCEEDED", (child.detail, child.postcondition)
    intent = EMPLOYER_SPEC.validate(out.proposal.computed["children"][0])
    pricing = intent["pricing"]
    for ccy, need in pricing["by_currency"].items():
        lines = [c for c in intent["contractors"] if c["amount"].currency == ccy]
        total = sum((c["amount"] for c in lines), Money.zero(ccy))
        swift = sum(
            (SWIFT_FEES[ccy] for c in lines if c["transfer_method"] == "SWIFT"), Money.zero(ccy)
        )
        assert need["need"] == total + swift
        if ccy != "USD":
            assert env.fake.balance(ccy, env.accts["e0"]).is_zero()
    sold = sum((v["sell"] for v in intent.get("fx_lock", {}).values()), Money.zero("USD"))
    direct = pricing["by_currency"].get("USD", {"need": Money.zero("USD")})["need"]
    spent = Money.of(start, "USD") - env.fake.balance("USD", env.accts["e0"])
    assert spent == sold + direct + pricing["platform_fee"]
    assert env.fake.balance("USD") == pricing["platform_fee"]  # the platform got the fee only
