"""Plan and policy: decisions, whole-plan invariants, confidence band and approval matrix."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from awx_common import Money, RiskTier
from awx_k1_treasury.book import (
    DEMO_BOOK,
    Forecast,
    Obligation,
    Priority,
    TransferMethod,
    TreasuryBook,
)
from awx_k1_treasury.money_rules import IndicativeRate, payout_fee, project
from awx_k1_treasury.plan import FUNDED, DecisionKind, PlanConfig, affected_decisions, build_plan
from awx_k1_treasury.policy import (
    ConfidenceBand,
    PolicyConfig,
    approval_tier,
    band_rank,
    confidence_band,
    tier_rank,
)
from hypothesis import given, settings
from hypothesis import strategies as st

RATES = {
    "EUR": IndicativeRate("EUR", "USD", "EURUSD", Decimal("1.08")),
    "GBP": IndicativeRate("GBP", "USD", "GBPUSD", Decimal("1.27")),
}
CFG = PolicyConfig()
T0 = datetime(2026, 10, 19, tzinfo=UTC)


def bal(usd: str, eur: str = "2000", gbp: str = "1500") -> dict[str, Money]:
    return {"USD": Money.of(usd, "USD"), "EUR": Money.of(eur, "EUR"), "GBP": Money.of(gbp, "GBP")}


def tier_for(balances, contradicted=(), ids=("ob-supplier-eur",), config=CFG):
    plan = build_plan(DEMO_BOOK, balances, RATES, config.plan, only=ids)
    band = confidence_band(DEMO_BOOK.forecasts, set(contradicted))
    return plan, approval_tier(plan, band, DEMO_BOOK, config, requested=ids)


def test_demo_plan_after_deposit():
    plan = build_plan(DEMO_BOOK, bal("42000"), RATES, PlanConfig())
    kinds = {d.obligation_id: d.kind for d in plan.decisions}
    assert kinds["ob-shipping"] is DecisionKind.FUND
    assert kinds["ob-supplier-eur"] is DecisionKind.CONVERT_AND_FUND
    assert kinds["ob-saas-gbp"] is DecisionKind.FUND
    assert kinds["ob-office-usd"] is DecisionKind.DEFER
    assert kinds["ob-contractor-gbp"] is DecisionKind.ESCALATE
    assert plan.breaches == ()
    (conv,) = plan.conversions
    assert conv.buy == Money.of("6038.55", "EUR")  # 7525.70 + 12.85 fee - 1500 headroom
    assert conv.sell == Money.of("6652.07", "USD")  # 6038.55 * 1.08 * 1.02, rounded up
    assert plan.projected["EUR"] == Money.of("500", "EUR")


def test_affected_decisions_after_deposit_is_only_the_supplier():
    before = build_plan(DEMO_BOOK, bal("34000"), RATES, PlanConfig())
    after = build_plan(DEMO_BOOK, bal("42000"), RATES, PlanConfig())
    assert affected_decisions(before, after) == ("ob-supplier-eur",)
    assert affected_decisions(after, after) == ()


def test_missing_rate_escalates_instead_of_guessing():
    plan = build_plan(DEMO_BOOK, bal("42000"), {}, PlanConfig(), only=["ob-supplier-eur"])
    assert plan.decision("ob-supplier-eur").kind is DecisionKind.ESCALATE
    assert plan.conversions == ()


def test_unknown_swift_fee_escalates():
    book = TreasuryBook(
        "v",
        "USD",
        (
            Obligation(
                "o", "d", "b", Money.of("100", "GBP"), TransferMethod.SWIFT, T0, Priority.STANDARD
            ),
        ),
        (),
        {"USD": Money.of("0", "USD"), "GBP": Money.of("0", "GBP")},
    )
    plan = build_plan(
        book, {"USD": Money.of("1000", "USD"), "GBP": Money.of("1000", "GBP")}, RATES, PlanConfig()
    )
    assert plan.decisions[0].kind is DecisionKind.ESCALATE
    assert "SWIFT fee" in plan.decisions[0].reason


def test_fee_table_is_config():
    small = PlanConfig(swift_fees={"EUR": Money.of("6.21", "EUR")})
    plan = build_plan(DEMO_BOOK, bal("42000"), RATES, small, only=["ob-supplier-eur"])
    assert plan.conversions[0].buy == Money.of("6031.91", "EUR")


@pytest.mark.parametrize("charged", ["6.21", "6.22", "12.85"])
def test_the_default_plan_keeps_the_eur_floor_whichever_recorded_fee_is_charged(charged):
    """8 Oct 2026: the sandbox charged 6.22 on some EUR SWIFT payouts; sized at 6.21, the EUR
    wallet ended 0.01 under its floor after paying."""
    plan = build_plan(DEMO_BOOK, bal("42000"), RATES, PlanConfig(), only=["ob-supplier-eur"])
    (conv,) = plan.conversions
    (payout,) = plan.payouts
    eur_after = bal("42000")["EUR"] + conv.buy - payout.amount - Money.of(charged, "EUR")
    assert eur_after >= DEMO_BOOK.floors["EUR"]


def test_book_requires_floors():
    with pytest.raises(ValueError, match="floor"):
        TreasuryBook("v", "USD", (), (), {})


# --- whole-plan invariants over random books -------------------------------------------------

currencies = st.sampled_from(["USD", "EUR", "GBP"])


@st.composite
def obligations(draw, i):
    cur = draw(currencies)
    method = (
        draw(st.sampled_from([TransferMethod.LOCAL, TransferMethod.SWIFT]))
        if cur == "EUR"
        else TransferMethod.LOCAL
    )
    return Obligation(
        f"o{i}",
        "d",
        f"b{i}",
        Money.from_minor(draw(st.integers(1, 5_000_000)), cur),
        method,
        T0 + timedelta(hours=draw(st.integers(0, 72))),
        draw(st.sampled_from(list(Priority))),
        policy_exception=draw(st.sampled_from([None, None, None, "exception"])),
    )


@st.composite
def books(draw):
    n = draw(st.integers(1, 6))
    obs = tuple(draw(obligations(i)) for i in range(n))
    floors = {
        c: Money.from_minor(draw(st.integers(0, 1_000_000)), c) for c in ("USD", "EUR", "GBP")
    }
    balances = {c: Money.from_minor(draw(st.integers(0, 10_000_000)), c) for c in floors}
    return TreasuryBook("v", "USD", obs, (), floors), balances


@settings(max_examples=300)
@given(books(), st.decimals(min_value=Decimal(0), max_value=Decimal("0.2"), places=3))
def test_any_plan_keeps_every_touched_wallet_at_or_above_its_floor(case, tol):
    book, balances = case
    plan = build_plan(book, balances, RATES, PlanConfig(rate_tolerance=tol))
    assert plan.breaches == ()
    touched = {c.buy.currency for c in plan.conversions} | {p.amount.currency for p in plan.payouts}
    if plan.conversions:
        touched.add("USD")
    for cur in touched:
        assert plan.projected[cur] >= book.floors[cur]
    assert plan.projected == project(plan.balances, plan.conversions, plan.payouts)
    # Fees are always in the plan, and every funded decision has exactly one payout.
    funded = [d for d in plan.decisions if d.kind in FUNDED]
    assert [p.obligation_id for p in plan.payouts] == [d.obligation_id for d in funded]
    for p in plan.payouts:
        assert p.fee == payout_fee(p.method, p.amount.currency)
        assert plan.decision(p.obligation_id).need == p.amount + p.fee
    # The conversion per currency is exactly the sum of what each obligation needed bought.
    for c in plan.conversions:
        bought = [d.buy for d in funded if d.buy is not None and d.buy.currency == c.buy.currency]
        assert sum((b.amount for b in bought), Decimal(0)) == c.buy.amount
        assert c.sell >= RATES[c.buy.currency].max_sell(c.buy, Decimal(0))
    # Exceptions are never executed; deferrables wait while a critical obligation is unfunded.
    for d in plan.decisions:
        ob = book.obligation(d.obligation_id)
        if ob.policy_exception:
            assert d.kind is DecisionKind.ESCALATE
        elif ob.priority is Priority.DEFERRABLE:
            assert d.kind is not DecisionKind.ESCALATE
        else:
            assert d.kind is not DecisionKind.DEFER
    assert len(plan.decisions) == len(book.obligations)


# --- confidence band and approval matrix ----------------------------------------------------


def fc(rid, bank=True, inv=True):
    return Forecast(rid, "p", Money.of("1", "USD"), T0, bank, inv)


def test_band_rules():
    assert confidence_band([fc("a")], set()) is ConfidenceBand.HIGH
    assert confidence_band([fc("a"), fc("b", inv=False)], set()) is ConfidenceBand.MEDIUM
    assert confidence_band([], set()) is ConfidenceBand.MEDIUM
    assert confidence_band([fc("a")], {"a"}) is ConfidenceBand.LOW


@given(st.lists(st.tuples(st.booleans(), st.booleans()), min_size=0, max_size=4), st.data())
def test_contradictions_can_only_lower_confidence(flags, data):
    fs = [fc(f"r{i}", b, v) for i, (b, v) in enumerate(flags)]
    ids = [f.receipt_id for f in fs]
    some = data.draw(st.sets(st.sampled_from(ids))) if ids else set()
    assert band_rank(confidence_band(fs, some)) <= band_rank(confidence_band(fs, set()))


def test_matrix_supported_auto_contradicted_single():
    _, t = tier_for(bal("42000"))
    assert t.tier is RiskTier.AUTO
    _, t = tier_for(bal("42000"), contradicted=["rcpt-acme"])
    assert t.tier is RiskTier.SINGLE
    assert any("LOW confidence" in r for r in t.reasons)


def test_matrix_blocks_an_unfunded_or_escalated_obligation():
    _, t = tier_for(bal("26000"))
    assert t.tier is RiskTier.BLOCKED
    _, t = tier_for(bal("42000"), ids=["ob-contractor-gbp"])
    assert t.tier is RiskTier.BLOCKED


def test_matrix_thin_funding_reserve_needs_a_person():
    # Funding wallet ends 1,500 above its 20,000 floor, inside the 2,000 cushion.
    plan, t = tier_for(bal("28152.07"))
    assert plan.projected["USD"] - DEMO_BOOK.floors["USD"] == Money.of("1500", "USD")
    assert t.tier is RiskTier.SINGLE
    assert any("reserve floor" in r for r in t.reasons)


def test_matrix_dual_above_single_approver_max():
    cfg = PolicyConfig(
        single_approver_max=Money.of("6000", "USD"),
        auto_conversion_limit={
            ConfidenceBand.HIGH: Money.of("5000", "USD"),
            ConfidenceBand.MEDIUM: Money.of("2500", "USD"),
            ConfidenceBand.LOW: Money.of("0", "USD"),
        },
    )
    _, t = tier_for(bal("42000"), config=cfg)
    assert t.tier is RiskTier.DUAL


def test_matrix_large_payout_needs_a_person():
    _, t = tier_for(bal("42000"), ids=["ob-shipping"])
    assert t.tier is RiskTier.AUTO  # 12,000 USD under the 15,000 autonomous payout limit
    cfg = PolicyConfig(auto_payout_limit={"USD": Money.of("1000", "USD")})
    _, t = tier_for(bal("42000"), ids=["ob-shipping"], config=cfg)
    assert t.tier is RiskTier.SINGLE


@given(st.integers(min_value=26_000_00, max_value=200_000_00))
def test_lower_confidence_never_lowers_the_tier(usd_minor):
    balances = bal("0")
    balances["USD"] = Money.from_minor(usd_minor, "USD")
    _, hi = tier_for(balances)
    _, lo = tier_for(balances, contradicted=["rcpt-acme"])
    assert tier_rank(lo.tier) >= tier_rank(hi.tier)


@given(st.integers(min_value=1, max_value=20_000_00))
def test_auto_only_within_the_band_limit(eur_minor):
    book = TreasuryBook(
        "v",
        "USD",
        (
            Obligation(
                "o",
                "d",
                "b",
                Money.from_minor(eur_minor, "EUR"),
                TransferMethod.LOCAL,
                T0,
                Priority.STANDARD,
            ),
        ),
        (fc("r"),),
        dict(DEMO_BOOK.floors),
    )
    balances = {
        "USD": Money.of("500000", "USD"),
        "EUR": Money.of("0", "EUR"),
        "GBP": Money.of("0", "GBP"),
    }
    for band in ConfidenceBand:
        plan = build_plan(book, balances, RATES, CFG.plan)
        t = approval_tier(plan, band, book, CFG, requested=["o"])
        conv = plan.conversion_total
        if t.tier is RiskTier.AUTO:
            assert conv is not None
            assert conv <= CFG.auto_conversion_limit[band]
            assert Money.from_minor(eur_minor, "EUR") <= CFG.auto_payout_limit["EUR"]
        if band is ConfidenceBand.LOW:
            assert t.tier is not RiskTier.AUTO


def test_policy_config_rejects_inverted_limits():
    with pytest.raises(ValueError, match="rise"):
        PolicyConfig(
            auto_conversion_limit={
                ConfidenceBand.HIGH: Money.of("1", "USD"),
                ConfidenceBand.MEDIUM: Money.of("5", "USD"),
                ConfidenceBand.LOW: Money.of("0", "USD"),
            }
        )
