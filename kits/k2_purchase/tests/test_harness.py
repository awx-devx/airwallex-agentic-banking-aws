import pytest
from awx_common import Money
from awx_k2_purchase.controls import (
    CardControls,
    DeclineReason,
    Interval,
    SpendLimit,
    SpendSoFar,
    control_violations,
)
from awx_k2_purchase.harness import (
    PROBE_ORDER,
    HarnessNotPossible,
    Observation,
    ProbeKind,
    judge,
    plan_probes,
    required_funding,
)
from awx_k2_purchase.provider import TxnState
from hypothesis import given
from hypothesis import strategies as st

MCC = ("5734", "5817", "7372")


def card(per: str, monthly: str | None, all_time: str | None, single=False) -> CardControls:
    limits = [SpendLimit(Interval.PER_TRANSACTION, Money.of(per, "USD"))]
    if monthly:
        limits.append(SpendLimit(Interval.MONTHLY, Money.of(monthly, "USD")))
    if all_time:
        limits.append(SpendLimit(Interval.ALL_TIME, Money.of(all_time, "USD")))
    return CardControls("USD", MCC, tuple(limits), single_use=single)


def passing_observations(probes):
    return {
        p.kind: Observation(
            p.kind,
            TxnState.CLEARED if p.expect is None else TxnState.DECLINED,
            None if p.expect is None else p.expect.value,
        )
        for p in probes
    }


def test_probes_for_a_monthly_card():
    probes = plan_probes(card("1500.00", "1500.00", "4500.00"), SpendSoFar({}))
    by = {p.kind: p for p in probes}
    assert tuple(by) == PROBE_ORDER
    assert by[ProbeKind.AT_LIMIT].amount == Money.of("1500.00", "USD")
    assert by[ProbeKind.AT_LIMIT].expect is None
    assert by[ProbeKind.OVER_LIMIT].amount == Money.of("1500.01", "USD")
    assert by[ProbeKind.OVER_LIMIT].expect is DeclineReason.LIMIT_EXCEEDED
    assert by[ProbeKind.CURRENCY].amount.currency == "EUR"
    assert by[ProbeKind.CURRENCY].expect is DeclineReason.CURRENCY_NOT_ALLOWED
    assert by[ProbeKind.MERCHANT_CATEGORY].merchant_category == "5812"
    assert by[ProbeKind.MERCHANT_CATEGORY].expect is DeclineReason.MERCHANT_CATEGORY_NOT_ALLOWED
    # MONTHLY == per transaction, so after the at-limit charge one cent breaks it.
    assert by[ProbeKind.CUMULATIVE].amount == Money.of("0.01", "USD")
    assert by[ProbeKind.CUMULATIVE].expect is DeclineReason.LIMIT_EXCEEDED
    # Limits are checked before funds, so only the at-limit debit needs funding.
    assert required_funding(probes, "USD") == Money.of("1500.00", "USD")


def test_required_acceptance_limit_boundary():
    """Kit 2 required acceptance test: at limit clears, limit + 0.01 fails
    LIMIT_EXCEEDED, disallowed currency and merchant category fail with their own reasons, and
    INSUFFICIENT_FUNDS during the policy test is a harness failure, not a passed control."""
    controls = card("100.00", "100.00", "300.00")
    probes = plan_probes(controls, SpendSoFar({}))
    by = {p.kind: p for p in probes}
    limit = controls.limit(Interval.PER_TRANSACTION)
    # Boundary, checked against the rule model.
    assert by[ProbeKind.AT_LIMIT].amount == limit
    assert control_violations(controls, limit, "5734") == frozenset()
    assert by[ProbeKind.OVER_LIMIT].amount == limit + Money.of("0.01", "USD")
    assert control_violations(controls, by[ProbeKind.OVER_LIMIT].amount, "5734") == {
        DeclineReason.LIMIT_EXCEEDED
    }
    # All as designed: pass.
    obs = passing_observations(probes)
    assert judge(probes, obs).ok
    # Right reasons are specific: a currency probe declined for the MCC reason fails.
    wrong = dict(obs)
    wrong[ProbeKind.CURRENCY] = Observation(
        ProbeKind.CURRENCY, TxnState.DECLINED, "MERCHANT_CATEGORY_NOT_ALLOWED"
    )
    assert not judge(probes, wrong).ok
    # At-limit that declines fails.
    wrong = dict(obs)
    wrong[ProbeKind.AT_LIMIT] = Observation(ProbeKind.AT_LIMIT, TxnState.DECLINED, "LIMIT_EXCEEDED")
    assert not judge(probes, wrong).ok
    # Limit + 0.01 that clears fails.
    wrong = dict(obs)
    wrong[ProbeKind.OVER_LIMIT] = Observation(ProbeKind.OVER_LIMIT, TxnState.CLEARED, None)
    assert not judge(probes, wrong).ok
    # INSUFFICIENT_FUNDS anywhere: harness failure, even on a probe meant to decline.
    for kind in PROBE_ORDER:
        broken = dict(obs)
        broken[kind] = Observation(kind, TxnState.DECLINED, "INSUFFICIENT_FUNDS")
        verdict = judge(probes, broken)
        assert not verdict.ok
        assert verdict.detail.startswith("HARNESS FAILURE: INSUFFICIENT_FUNDS")


def test_authorized_but_not_cleared_is_not_a_pass():
    probes = plan_probes(card("100.00", "100.00", "300.00"), SpendSoFar({}))
    obs = passing_observations(probes)
    obs[ProbeKind.AT_LIMIT] = Observation(ProbeKind.AT_LIMIT, TxnState.AUTHORIZED, None)
    assert not judge(probes, obs).ok
    obs.pop(ProbeKind.CUMULATIVE)
    assert "never observed" in judge(probes, obs).detail


@pytest.mark.parametrize(
    ("controls", "message"),
    [
        (card("100.00", None, "100.00", single=True), "single-use"),
        (card("100.00", "500.00", "500.00"), "cannot be isolated"),
    ],
)
def test_not_isolatable(controls, message):
    with pytest.raises(HarnessNotPossible, match=message):
        plan_probes(controls, SpendSoFar({}))


def test_prior_spend_that_leaves_no_room_for_at_limit():
    controls = card("100.00", "100.00", "300.00")
    with pytest.raises(HarnessNotPossible):
        plan_probes(controls, SpendSoFar({Interval.MONTHLY: Money.of("50.00", "USD")}))


@given(
    per=st.integers(2, 5_000_000),
    gap=st.integers(0, 1_000_000),
    spent=st.integers(0, 1_000_000),
)
def test_every_planned_probe_isolates_exactly_one_rule(per, gap, spent):
    """For any card the planner accepts, each probe breaks exactly its own rule."""
    per_m = Money.from_minor(per, "USD")
    monthly = Money.from_minor(per + gap, "USD")
    controls = CardControls(
        "USD",
        MCC,
        (SpendLimit(Interval.PER_TRANSACTION, per_m), SpendLimit(Interval.MONTHLY, monthly)),
    )
    state = SpendSoFar({Interval.MONTHLY: Money.from_minor(spent, "USD")})
    try:
        probes = plan_probes(controls, state)
    except HarnessNotPossible:
        assert spent + per > per + gap or (per + gap) - (spent + per) + 1 > per
        return
    assert [p.kind for p in probes] == list(PROBE_ORDER)
    at = next(p for p in probes if p.kind is ProbeKind.AT_LIMIT)
    over = next(p for p in probes if p.kind is ProbeKind.OVER_LIMIT)
    assert at.amount == per_m
    assert over.amount == per_m + Money.from_minor(1, "USD")
    after = SpendSoFar({Interval.MONTHLY: state.of(Interval.MONTHLY, "USD") + per_m})
    for p in probes:
        s = after if p.kind is ProbeKind.CUMULATIVE else state
        got = control_violations(controls, p.amount, p.merchant_category, s)
        assert got == (frozenset() if p.expect is None else {p.expect})
