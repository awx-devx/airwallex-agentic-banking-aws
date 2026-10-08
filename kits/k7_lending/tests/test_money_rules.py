"""Unit and hypothesis property tests for every kit-7 money rule."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from awx_common import ApprovalBindingError, Money, transport
from awx_k7_lending.book import (
    LoanBook,
    PendingDisbursement,
    load_book,
    round_down_to,
    week_window,
)
from awx_k7_lending.ledger import (
    Acct,
    Entry,
    EntryKind,
    LedgerError,
    Line,
    Subledger,
    split_due,
)
from awx_k7_lending.policy import TrancheOutcome, size_disbursement
from awx_k7_lending.reconcile import reconcile_ledger
from awx_k7_lending.repayment import RepaymentTerms, plan_collection, weekly_due
from hypothesis import given, settings
from hypothesis import strategies as st

USD = "USD"
FUNDED = datetime(2025, 12, 28, 12, 0, tzinfo=UTC)  # first collectible week 2026-W01


def usd(x) -> Money:
    return Money.of(x, USD)


cents = st.integers(min_value=0, max_value=10_000_000)  # up to 100,000.00
money = cents.map(lambda c: Money.from_minor(c, USD))
signed_money = st.integers(min_value=-1_000_000, max_value=10_000_000).map(
    lambda c: Money.from_minor(c, USD)
)
rates = st.integers(min_value=1, max_value=10_000).map(lambda bp: Decimal(bp) / Decimal(10_000))


@st.composite
def repayment_terms(draw) -> RepaymentTerms:
    cap = draw(st.integers(min_value=1, max_value=1_000_000))
    floor = draw(st.integers(min_value=0, max_value=cap))
    return RepaymentTerms(draw(rates), Money.from_minor(cap, USD), Money.from_minor(floor, USD))


# Repayment formula


def test_formula_examples():
    t = RepaymentTerms(Decimal("0.10"), usd("1500.00"), usd("200.00"))
    assert weekly_due(usd("10000.00"), usd("8640.00"), t) == (usd("1000.00"), usd("1000.00"))
    assert weekly_due(usd("0.00"), usd("8640.00"), t)[1] == usd("200.00")  # floor
    assert weekly_due(usd("90000.00"), usd("8640.00"), t)[1] == usd("1500.00")  # cap
    assert weekly_due(usd("90000.00"), usd("120.00"), t)[1] == usd("120.00")  # final week
    assert weekly_due(
        usd("10.09"), usd("100.00"), RepaymentTerms(Decimal("0.10"), usd("50.00"), usd("0.00"))
    )[0] == usd("1.00")  # ROUND_DOWN, not 1.01
    p = plan_collection(
        usd("6000.00"),
        usd("5400.00"),
        usd("300.00"),
        RepaymentTerms(Decimal("0.15"), usd("1200.00"), usd("150.00")),
    )
    assert (p.due, p.collect, p.shortfall) == (usd("900.00"), usd("300.00"), usd("600.00"))
    assert plan_collection(usd("1.00"), usd("5.00"), usd("-3.00"), t).collect == usd("0.00")


@pytest.mark.parametrize(
    ("rate", "cap", "floor"),
    [("0", "10", "0"), ("1.5", "10", "0"), ("0.1", "0", "0"), ("0.1", "10", "11")],
)
def test_bad_terms(rate, cap, floor):
    with pytest.raises(ValueError, match=r"share_rate|cap|floor"):
        RepaymentTerms(Decimal(rate), usd(cap), usd(floor))
    with pytest.raises(TypeError):
        RepaymentTerms(0.1, usd("10"), usd("0"))  # type: ignore[arg-type]


@given(money, money, signed_money, repayment_terms())
@settings(max_examples=400)
def test_collection_never_exceeds_due_cap_or_wallet(revenue, outstanding, available, terms):
    p = plan_collection(revenue, outstanding, available, terms)
    zero = Money.zero(USD)
    assert zero <= p.collect <= p.due <= terms.weekly_cap
    assert p.due <= outstanding
    assert p.collect <= max(available, zero)
    assert p.collect + p.shortfall == p.due  # rounding never creates or loses a cent
    assert p.due >= min(terms.weekly_floor, outstanding)
    exact = revenue.amount * terms.share_rate
    assert p.share.amount <= exact < p.share.amount + Decimal("0.01")  # one rounding, down


# Ledger


def test_entry_must_balance_and_be_scoped():
    with pytest.raises(LedgerError, match="balance"):
        Entry("k", EntryKind.CAPITAL, (Line(Acct.PLATFORM_CASH, None, usd("1.00")),))
    with pytest.raises(LedgerError, match="loan_id"):
        Line(Acct.PRINCIPAL_RECEIVABLE, None, usd("1.00"))
    with pytest.raises(LedgerError, match="portfolio"):
        Line(Acct.PLATFORM_CASH, "loan-a", usd("1.00"))
    led = Subledger("AUD")
    with pytest.raises(LedgerError, match="ledger is"):
        led.post(
            Entry(
                "k",
                EntryKind.CAPITAL,
                (
                    Line(Acct.PLATFORM_CASH, None, usd("1.00")),
                    Line(Acct.PORTFOLIO_CAPITAL, None, usd("-1.00")),
                ),
            )
        )


@given(money, money, cents)
def test_split_due_sums_exactly_and_stays_in_buckets(principal, fee, due_c):
    total = (principal + fee).to_minor()
    due = Money.from_minor(min(due_c, total), USD)
    p, f = split_due(due, principal, fee)
    assert p + f == due
    assert Money.zero(USD) <= p <= principal
    assert Money.zero(USD) <= f <= fee


def test_split_due_refuses_over_billing():
    with pytest.raises(LedgerError):
        split_due(usd("10.01"), usd("5.00"), usd("5.00"))


def test_round_down_to_and_week_window():
    assert round_down_to(usd("3399.99"), usd("100.00")) == usd("3300.00")
    assert round_down_to(usd("-1.00"), usd("100.00")) == usd("0.00")
    start, end = week_window("2026-W41")
    assert (start.isoformat(), end.isoformat()) == (
        "2026-10-05T00:00:00+00:00",
        "2026-10-12T00:00:00+00:00",
    )


# Disbursement sizing


@given(money, signed_money, st.integers(1, 500_000), st.integers(1, 100_000), cents)
@settings(max_examples=400)
def test_tranche_never_breaches_floor_or_commitment(remaining, usable, floor_c, inc_c, min_c):
    from awx_k7_lending.book import PortfolioConfig

    cfg = PortfolioConfig(
        currency=USD,
        reserve_floor=Money.from_minor(floor_c, USD),
        min_disbursement=Money.from_minor(max(min_c, 1), USD),
        disbursement_increment=Money.from_minor(inc_c, USD),
        dual_approval_above=usd("25000.00"),
    )
    plan = size_disbursement(remaining, usable, cfg)
    assert Money.zero(USD) <= plan.amount <= remaining
    assert (plan.outcome is TrancheOutcome.DELAY) == plan.amount.is_zero()
    if not plan.amount.is_zero():
        assert plan.cash_after >= cfg.reserve_floor
        assert plan.cash_after == usable - plan.amount
    if plan.outcome is TrancheOutcome.PARTIAL:
        assert plan.amount.to_minor() % inc_c == 0
        assert plan.amount >= cfg.min_disbursement
    if plan.outcome is TrancheOutcome.FULL:
        assert plan.amount == remaining


# The whole subledger under random portfolio histories


@st.composite
def histories(draw):
    weeks = draw(st.integers(min_value=0, max_value=12))
    return {
        "commitment": draw(st.integers(100_00, 50_000_00)),
        "fee_bp": draw(st.integers(0, 2_000)),
        "terms": draw(repayment_terms()),
        "weeks": [(draw(cents), draw(st.integers(-10_000, 2_000_000))) for _ in range(weeks)],
    }


@given(histories())
@settings(max_examples=200, deadline=None)
def test_subledger_always_balances_and_reconciles(h):
    from datetime import timedelta

    from awx_k7_lending.book import LoanTerms, PortfolioConfig, WeekRecord

    cfg = PortfolioConfig(USD, usd("0.00"), usd("0.01"), usd("0.01"), usd("100000000.00"))
    book = LoanBook(cfg, "0123456789ab")
    commitment = Money.from_minor(h["commitment"], USD)
    book.add_loan(
        LoanTerms(
            "loan-x",
            "t-x",
            "acct_x",
            commitment,
            Decimal(h["fee_bp"]) / Decimal(10_000),
            h["terms"],
            "2026-W01",
        )
    )
    book.fund("capital", usd("1000000.00"))
    book.reserve_disbursement(
        PendingDisbursement("loan-x", 1, commitment, "rid-1", "owner-1", FUNDED), resend=False
    )
    book.settle_disbursement("rid-1", "cat_1", commitment, settled_at=FUNDED)
    fee = book.loan("loan-x").fee_for(commitment)
    collected = Money.zero(USD)
    start, _ = week_window("2026-W01")
    for i, (rev_c, avail_c) in enumerate(h["weeks"]):
        week = (start + timedelta(weeks=i)).strftime("%G-W%V")
        p = plan_collection(
            Money.from_minor(rev_c, USD),
            book.ledger.outstanding("loan-x"),
            Money.from_minor(avail_c, USD),
            h["terms"],
        )
        book.close_week(
            WeekRecord(
                "loan-x",
                week,
                p.due,
                p.collect,
                None,
                f"chg_{i}" if p.collect.amount else None,
                None,
            )
        )
        collected = collected + p.collect
        book = LoanBook.from_state(transport.loads(transport.dumps(book.to_state())))
        assert book.ledger.trial_balance().is_zero()
        for acct in (Acct.PRINCIPAL_RECEIVABLE, Acct.FEE_RECEIVABLE, Acct.DUE_RECEIVABLE):
            assert not book.ledger.balance(acct, "loan-x").is_negative()
    assert reconcile_ledger(book) == []
    # every cent is accounted for: outstanding + collected == principal + fee
    assert book.ledger.outstanding("loan-x") + collected == commitment + fee
    assert book.ledger.platform_cash() == usd("1000000.00") - commitment + collected
    # fee is earned only as it falls due, and never more than was charged
    earned = -book.ledger.balance(Acct.FEE_INCOME, "loan-x")
    unearned = -book.ledger.balance(Acct.UNEARNED_FEE, "loan-x")
    assert earned + unearned == fee
    assert not unearned.is_negative()


def test_reserve_disbursement_guards(world):
    book, _ = load_book(world.store)
    first = PendingDisbursement("loan-a", 1, usd("100.00"), "rid-1", "owner-1", FUNDED)
    book.reserve_disbursement(first, resend=False)
    book.reserve_disbursement(first, resend=True)  # idempotent for a re-send
    book.reserve_disbursement(first, resend=False)  # its own retried build: idempotent
    with pytest.raises(ApprovalBindingError, match="another run"):
        book.reserve_disbursement(  # a stale first-send decision of another proposal
            PendingDisbursement("loan-a", 1, usd("100.00"), "rid-1", "owner-2", FUNDED),
            resend=False,
        )
    with pytest.raises(ApprovalBindingError, match="gone"):
        book.reserve_disbursement(
            PendingDisbursement("loan-b", 1, usd("100.00"), "rid-9", "owner-1", FUNDED),
            resend=True,
        )
    with pytest.raises(LedgerError, match="in flight"):
        book.reserve_disbursement(
            PendingDisbursement("loan-a", 1, usd("100.00"), "rid-2", "owner-1", FUNDED),
            resend=False,
        )
    with pytest.raises(LedgerError, match="stale"):
        book.reserve_disbursement(
            PendingDisbursement("loan-b", 2, usd("100.00"), "rid-3", "owner-1", FUNDED),
            resend=False,
        )
    with pytest.raises(LedgerError, match="approved"):
        book.settle_disbursement("rid-1", "tr_1", usd("100.01"), settled_at=FUNDED)
    assert book.settle_disbursement("rid-1", "tr_1", usd("100.00"), settled_at=FUNDED)
    assert book.ledger.balance(Acct.FEE_RECEIVABLE, "loan-a") == usd("8.00")
    assert book.ledger.balance(Acct.UNEARNED_FEE, "loan-a") == usd("-8.00")
    assert book.ledger.balance(Acct.FEE_INCOME, "loan-a").is_zero()
    assert book.next_tranche("loan-a") == 2


def test_week_rules():
    from awx_k7_lending.book import first_week_starting_at_or_after, next_week

    assert first_week_starting_at_or_after(datetime(2026, 10, 5, 0, 0, tzinfo=UTC)) == "2026-W41"
    assert first_week_starting_at_or_after(datetime(2026, 10, 5, 0, 1, tzinfo=UTC)) == "2026-W42"
    assert next_week("2026-W53") == "2027-W01"
    assert next_week("2026-W52") == "2026-W53"


def test_revenue_filter_excludes_our_own_movements():
    from awx_k7_lending.provider import is_revenue

    base = {"transaction_type": "DEPOSIT", "amount": Decimal("5.00"), "currency": USD}
    assert is_revenue(base, USD, frozenset())
    assert not is_revenue({**base, "source_id": "tr_9"}, USD, frozenset({"tr_9"}))
    assert not is_revenue({**base, "description": "poc-k7-test-advance-x"}, USD, frozenset())
    assert not is_revenue({**base, "transaction_type": "TRANSFER"}, USD, frozenset())
    assert not is_revenue({**base, "currency": "AUD"}, USD, frozenset())
    assert not is_revenue({**base, "amount": Decimal("-5.00")}, USD, frozenset())


def test_one_loan_per_borrower_connected_account():
    """At the book: a second tenant on the same account is refused."""
    from awx_k7_lending.book import LoanTerms, PortfolioConfig

    cfg = PortfolioConfig(USD, usd("0.00"), usd("0.01"), usd("0.01"), usd("100.00"))
    book = LoanBook(cfg, "0123456789ab")
    terms = RepaymentTerms(Decimal("0.1"), usd("10.00"), usd("0.00"))
    book.add_loan(
        LoanTerms("loan-a", "t-a", "acct_same", usd("100.00"), Decimal("0"), terms, "2026-W41")
    )
    with pytest.raises(ValueError, match="connected account"):
        book.add_loan(
            LoanTerms("loan-b", "t-b", "acct_same", usd("100.00"), Decimal("0"), terms, "2026-W41")
        )
