"""Kit 7 scenarios through the real runner (local_runner.run_kit_workflow) against
FakeAirwallex: the required acceptance test, platform binding, reconciliation scope, collection
order and revenue, approvals, every runner fault, and kit rules M1 to M4."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from awx_common import (
    ApprovalDecision,
    Decision,
    OperationStatus,
    Origin,
    RiskTier,
    check_tenant_scope,
)
from awx_common.approvals import ApprovalBindingError, approval_state
from awx_common.errors import TenantScopeViolation, ValidationFailed
from awx_common.store import InMemoryKitStore, VersionConflict
from awx_common.workflow import PolicyDecision, StepContext
from awx_k7_lending import SPECS, load_book, update_book
from awx_k7_lending.book import BOOK_KEY, WeekRecord
from awx_k7_lending.ledger import DuplicatePosting, LedgerError
from awx_k7_lending.policy import TrancheOutcome
from awx_spine.fake import Fault

COLLECT = SPECS["collect_weekly_repayment"]
DISBURSE = SPECS["disburse_advance"]
RECONCILE = SPECS["reconcile_portfolio"]
CAT = "connected_account_transfers.create"
CHARGE = "charges.create"


def book_of(w):
    return load_book(w.store)[0]


def acct(w, lid):
    return w.borrowers[lid].connected_account_id


def disburse(w, lid, now=None, **kw):
    return w.run(DISBURSE, {"loan_id": lid}, tenant=w.borrowers[lid], now=now, **kw)


def collect(w, lid, week, now=None, **kw):
    return w.run(COLLECT, {"loan_id": lid, "week": week}, tenant=w.borrowers[lid], now=now, **kw)


def recon(w, now=None, **kw):
    from awx_common import TenantContext

    return w.run(RECONCILE, {}, tenant=TenantContext("platform"), now=now, **kw)


RECOVERED = "_awx_recovered_from"  # awx_spine.executor.RECOVERED_FROM


def rid_objects(w, kind, rid):
    return [o for o in w.fake.objects(kind) if o["request_id"] == rid]


def sends(w, name, rid=None):
    return [o for o in w.fake.executed if o.name == name and (rid is None or o.request_id == rid)]


@pytest.fixture
def portfolio(k7, world):
    """A (8,000) and B (5,000) disbursed at T0 (Tuesday of W41), so W42 is their first
    collectible week. W42 revenue: A 10,000 and B 6,000; B then spends down to a 300 wallet.
    Foreign poc-k7- objects from another run already sit on the shared account."""
    w = world
    w.fake.foreign("charges", acct(w, "loan-a"), k7.usd("123.00"))
    w.fake.foreign("connected_account_transfers", acct(w, "loan-b"), k7.usd("456.00"))
    for lid in ("loan-a", "loan-b"):
        out = disburse(w, lid)
        assert out.decision.risk_tier is RiskTier.SINGLE
        assert out.decision.computed["outcome"] == TrancheOutcome.FULL
        assert out.state == "SUCCEEDED", out.detail
        assert out.post.ok, out.post.detail
    w.fake.move_to(k7.IN_W42)
    w.fake.deposit(acct(w, "loan-a"), k7.usd("10000.00"))
    w.fake.deposit(acct(w, "loan-b"), k7.usd("6000.00"))
    w.fake.spend(acct(w, "loan-b"), k7.usd("10700.00"))  # 5,000 + 6,000 - 10,700 = 300
    return w


# The report's required acceptance test


def test_required_acceptance_floor_binds_underpayment_shrinks_third_disbursement(k7, portfolio):
    """Report kit 7 acceptance test: make the reserve floor bind, underpay one borrower, and
    assert the third disbursement is reduced so fresh platform cash stays above the floor;
    collection replay is rejected; approval tampering fails closed; the loan ledger reconciles
    to provider-settled movements."""
    w, usd = portfolio, k7.usd

    a = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    b = collect(w, "loan-b", k7.W42)
    assert a.state == "SUCCEEDED", a.detail
    assert a.decision.computed["collect"] == usd("1000.00")
    assert b.decision.computed["due"] == usd("900.00")
    assert b.decision.computed["collect"] == usd("300.00")
    book = book_of(w)
    assert book.ledger.arrears("loan-b") == usd("600.00")
    assert book.ledger.arrears("loan-a").is_zero()

    c = disburse(w, "loan-c")  # min(ledger 8,300, wallet 38,300) - floor 5,000
    assert c.decision.computed["outcome"] == TrancheOutcome.PARTIAL
    assert c.decision.computed["amount"] == usd("3300.00")  # 3,900 had B paid in full
    assert c.decision.risk_tier is RiskTier.SINGLE
    assert c.post.ok, c.post.detail
    book = book_of(w)
    assert book.ledger.platform_cash() == usd("5000.00") == book.config.reserve_floor
    assert w.fake.balance("USD") >= book.config.reserve_floor

    before = w.fake.creates(CHARGE)
    replay = collect(w, "loan-a", k7.W42, k7.AFTER_W42 + timedelta(hours=1))
    assert replay.state == "BLOCKED"
    assert not replay.ops
    assert w.fake.creates(CHARGE) == before

    forged = ApprovalDecision(
        c.proposal.proposal_id, "0" * 64, "approver-1", Decision.APPROVE, c.proposal.created_at
    )
    with pytest.raises(ApprovalBindingError):
        approval_state(c.proposal, [forged], now=c.proposal.created_at)
    for edited in (
        replace(c.proposal, intent={"loan_id": "loan-a"}),
        replace(c.proposal, computed={**c.proposal.computed, "amount": usd("3900.00")}),
    ):
        with pytest.raises(ApprovalBindingError):
            approval_state(edited, k7.approve(c.proposal), now=c.proposal.created_at)

    r = recon(w, origin=Origin.AGENT)
    assert r.proposal.risk_tier is RiskTier.AUTO  # agent_min_tier AUTO: no money moves
    assert r.post.ok, r.post.detail
    assert book_of(w).last_reconcile.ok


# Platform-bound movements


def test_charge_and_transfer_are_platform_bound_without_the_header(k7, portfolio):
    w = portfolio
    out = collect(w, "loan-b", k7.W42, k7.AFTER_W42)
    (charge,) = out.ops
    assert charge.on_behalf_of is None
    assert charge.body["source"] == acct(w, "loan-b")
    assert charge.body["reference"].startswith(f"poc-k7-{book_of(w).book_id}-")
    check_tenant_scope(charge, w.borrowers["loan-b"])
    for op in sends(w, CAT):
        assert op.on_behalf_of is None
        assert op.body["destination"] in (acct(w, "loan-a"), acct(w, "loan-b"))
    r = recon(w)
    (report,) = r.ops
    check_tenant_scope(report, k7.PLATFORM)
    assert all(on is None for m, p, on in w.fake.requests if p.startswith("/api/v1/charges"))


def test_a_headered_charge_never_reaches_airwallex(k7, portfolio):
    w = portfolio
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    headered = replace(out.ops[0], on_behalf_of=acct(w, "loan-a"))
    with pytest.raises(TenantScopeViolation):
        check_tenant_scope(headered, w.borrowers["loan-a"])
    assert w.fake.execute(headered).status is OperationStatus.FAILED


# Reconciliation from the book's own request_ids


def test_reconcile_is_clean_with_foreign_objects_on_the_shared_account(k7, portfolio):
    w = portfolio
    assert recon(w).post.ok
    collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    out = recon(w)
    assert out.post.ok, out.post.detail
    lists = [
        q
        for q in w.fake.requests
        if q[1] in ("/api/v1/charges", "/api/v1/connected_account_transfers")
    ]
    assert lists  # read back by ?request_id= only (the fake records the path, not the query)


def test_settled_but_unposted_own_movement_is_a_break(k7, portfolio):
    w = portfolio
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    rid = out.ops[0].request_id

    def unpost(book):
        record = book._weeks.pop(("loan-a", k7.W42))
        book.ledger = type(book.ledger)(
            book.config.currency,
            [e for e in book.ledger.entries if e.key != f"collect:loan-a:{k7.W42}"],
        )
        book._weeks[("loan-a", "2026-W40")] = replace(
            record, week="2026-W40", collected=k7.usd("0.00"), due=k7.usd("0.00")
        )

    update_book(w.store, unpost)
    r = recon(w)
    assert not r.post.ok
    assert rid in r.post.detail
    assert not book_of(w).last_reconcile.ok


# Funded first, weeks in order


def test_no_collection_before_funding_or_for_a_pre_funding_week(k7, world):
    w = world
    early = collect(w, "loan-a", k7.W41, k7.T0 + timedelta(days=6))
    assert early.state == "BLOCKED"
    assert "no settled tranche" in early.decision.reasons[0]
    disburse(w, "loan-a", k7.IN_W42)  # funded mid-W42
    for week in (k7.W41, k7.W42):
        out = collect(w, "loan-a", week, k7.AFTER_W43)
        assert out.state == "BLOCKED"
        assert "next week to collect" in out.decision.reasons[0]
    assert book_of(w).next_collectible_week("loan-a") == k7.W43


def test_weeks_are_collected_in_order_only(k7, portfolio):
    w = portfolio
    later = collect(w, "loan-a", k7.W43, k7.AFTER_W43)
    assert later.state == "BLOCKED"
    assert not later.ops
    assert collect(w, "loan-a", k7.W42).post.ok
    assert collect(w, "loan-a", k7.W43).post.ok


def test_week_not_over_is_blocked(k7, portfolio):
    assert collect(portfolio, "loan-a", k7.W42).state == "BLOCKED"


# The advance is not revenue


@pytest.mark.parametrize("name_credits", [True, False])
def test_a_tranche_inside_the_week_does_not_change_the_due(k7, name_credits):
    w = k7.make_portfolio(capital="10000.00")
    w.fake.credits_as_deposits = True  # our credit appears as a DEPOSIT row (pessimistic)
    w.fake.name_credits = name_credits
    first = disburse(w, "loan-a")
    assert first.decision.computed["amount"] == k7.usd("5000.00")  # partial: the floor binds
    w.fake.move_to(k7.IN_W42)
    w.fake.deposit(acct(w, "loan-a"), k7.usd("10000.00"))
    update_book(w.store, lambda b: b.fund("capital-2", k7.usd("5000.00")))
    second = disburse(w, "loan-a")  # 3,000 lands in A's wallet during W42
    assert second.decision.computed["amount"] == k7.usd("3000.00")
    assert second.post.ok, second.post.detail
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert out.decision.computed["revenue"] == k7.usd("10000.00")
    assert out.decision.computed["due"] == k7.usd("1000.00")  # not 1,300


def test_tranche_seen_settled_just_after_the_week_is_still_not_revenue(k7):
    w = k7.make_portfolio(capital="10000.00")
    w.fake.credits_as_deposits = True
    w.fake.name_credits = False
    disburse(w, "loan-a")
    w.fake.move_to(k7.IN_W42)
    w.fake.deposit(acct(w, "loan-a"), k7.usd("10000.00"))
    update_book(w.store, lambda b: b.fund("capital-2", k7.usd("5000.00")))
    end_w42 = k7.AFTER_W42 - timedelta(hours=9)  # Monday 00:00 of W43
    out = disburse(w, "loan-a", end_w42 - timedelta(seconds=0.5))  # created in W42
    assert out.post.ok, out.post.detail
    entry = [e for e in book_of(w).ledger.entries if e.key.startswith("disburse:")][-1]
    assert entry.memo["settled_at"] >= end_w42.isoformat()  # seen SETTLED in W43
    c = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert c.decision.computed["revenue"] == k7.usd("10000.00")


# Approvals and origins


def test_model_started_collection_needs_an_approver(k7, portfolio):
    w = portfolio
    waiting = collect(w, "loan-a", k7.W42, k7.AFTER_W42, origin=Origin.AGENT, decide=lambda p: [])
    assert waiting.proposal.risk_tier is RiskTier.SINGLE
    assert waiting.state == "EXPIRED"
    assert not waiting.ops
    approved = collect(w, "loan-a", k7.W42, origin=Origin.AGENT)
    assert approved.state == "SUCCEEDED", approved.detail
    assert approved.post.ok


def test_disbursement_waits_for_an_approver_and_self_approval_fails(k7, portfolio):
    w = portfolio
    out = disburse(w, "loan-c", decide=lambda p: [])
    assert out.state == "EXPIRED"
    assert not out.ops

    def self_approve(p):
        return [
            ApprovalDecision(
                p.proposal_id, p.intent_hash, p.proposed_by, Decision.APPROVE, p.created_at
            )
        ]

    out = disburse(w, "loan-c", decide=self_approve)
    assert out.state == "REJECTED"
    assert "binding" in out.detail
    assert not out.ops


def test_cash_change_after_approval_fails_closed_as_stale(k7, portfolio):
    w = portfolio
    out = disburse(
        w,
        "loan-c",
        before_execution=lambda: update_book(
            w.store, lambda b: b.fund("capital-2", k7.usd("1000.00"))
        ),
    )
    assert out.abandoned == "STALE"
    assert not out.ops
    assert not book_of(w).pending()


def test_wallet_change_before_execution_fails_closed_as_stale(k7, portfolio):
    w = portfolio
    out = collect(
        w,
        "loan-a",
        k7.W42,
        k7.AFTER_W42,
        origin=Origin.AGENT,
        before_execution=lambda: w.fake.spend(acct(w, "loan-a"), k7.usd("17500.00")),
    )
    assert out.abandoned == "STALE"
    assert not out.ops
    assert book_of(w).week_record("loan-a", k7.W42) is None


def test_reconcile_proposal_hash_is_stable_across_a_re_run(k7, portfolio):
    w = portfolio
    first = RECONCILE.policy({}, w.ctx(RECONCILE, k7.PLATFORM)).computed
    w.fake.move_to(k7.AFTER_W43)
    w.fake.deposit(acct(w, "loan-c"), k7.usd("1.00"))
    assert RECONCILE.policy({}, w.ctx(RECONCILE, k7.PLATFORM)).computed == first


def test_floor_bound_tight_enough_delays_the_tranche(k7):
    w = k7.make_portfolio(reserve_floor=k7.usd("7800.00"))
    disburse(w, "loan-a")
    b = disburse(w, "loan-b")
    assert b.decision.computed["outcome"] == TrancheOutcome.PARTIAL  # 4,200
    c = disburse(w, "loan-c")
    assert c.state == "BLOCKED"
    assert c.decision.computed["outcome"] == TrancheOutcome.DELAY
    assert not c.ops
    assert book_of(w).ledger.platform_cash() == k7.usd("7800.00")


def test_wallet_below_ledger_cash_binds_the_tranche(k7):
    w = k7.make_portfolio(fake=k7.K7Fake(k7.usd("9000.00")))
    out = disburse(w, "loan-c")
    assert out.decision.computed["amount"] == k7.usd("4000.00")  # 9,000 - floor, not 15,000


# Faults through the real runner


def test_insufficient_funds_at_execution_becomes_arrears_not_a_retry(k7, portfolio):
    w = portfolio
    w.fake.inject(CHARGE, Fault.INSUFFICIENT_FUNDS)
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    (result,) = out.results["charge"]
    assert result.status is OperationStatus.FAILED
    assert result.error_code == "insufficient_fund"
    assert out.post.ok, out.post.detail
    assert len(sends(w, CHARGE)) == 1
    book = book_of(w)
    assert book.ledger.arrears("loan-a") == k7.usd("1000.00")
    assert book.week_record("loan-a", k7.W42).collected.is_zero()
    w.fake.deposit(acct(w, "loan-a"), k7.usd("50000.00"))  # in W43
    nxt = collect(w, "loan-a", k7.W43, k7.AFTER_W43)
    assert nxt.decision.computed["collect"] == k7.usd("1500.00")  # the cap, never a catch-up
    assert book_of(w).ledger.arrears("loan-a") == k7.usd("1000.00")
    out = recon(w)
    assert out.post.ok, out.post.detail


def test_timeout_after_submit_reconciles_and_collects_once(k7, portfolio):
    w = portfolio
    w.fake.move_to(k7.AFTER_W42)
    w.fake.inject(CHARGE, Fault.TIMEOUT_AFTER_SUBMIT)
    n = len(w.fake.requests)
    out = collect(w, "loan-a", k7.W42)
    (result,) = out.results["charge"]
    assert (result.body or {}).get(RECOVERED) == "read_back"  # the timeout's own path
    assert ("GET", "/api/v1/charges", None) in w.fake.requests[n:]  # ?request_id= read-back
    assert len(sends(w, CHARGE)) == 1  # read back, never resent
    assert w.fake.creates(CHARGE) == 1
    assert out.post.ok, out.post.detail
    assert book_of(w).week_record("loan-a", k7.W42).collected == k7.usd("1000.00")


@pytest.mark.parametrize("op_name", [CHARGE, CAT])
def test_write_that_lands_invisible_stays_in_flight_then_posts_once(k7, portfolio, op_name):
    """Lands at the client timeout but stays invisible to ?request_id= and GET by id for an
    hour (M1): the first run keeps the money in flight, the next run re-emits the same
    Operation, Airwallex refuses the duplicate and the read-back finds the original."""
    w = portfolio
    w.fake.inject(op_name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    if op_name == CHARGE:
        first = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
        held = book_of(w).pending_collection("loan-a")
    else:
        first = disburse(w, "loan-c")
        held = book_of(w).pending_for("loan-c")
    assert next(iter(first.results.values()))[0].status is OperationStatus.OUTCOME_UNKNOWN
    assert held is not None
    assert len(sends(w, op_name, first.ops[0].request_id)) == 1  # no in-run resend
    w.fake.advance(3600)
    if op_name == CHARGE:
        again = collect(w, "loan-a", k7.W42)
        assert book_of(w).week_record("loan-a", k7.W42).collected == k7.usd("1000.00")
    else:
        again = disburse(w, "loan-c")
        assert book_of(w).disbursed("loan-c") == held.amount
    assert again.decision.computed["resend"] is True
    assert again.decision.computed["request_id"] == first.ops[0].request_id
    assert all(o == first.ops[0] for o in again.ops)  # re-emitted unchanged, if at all
    assert again.post.ok, again.post.detail
    assert w.fake.creates(op_name) == (1 if op_name == CHARGE else 3)


def test_list_lag_after_a_successful_create_still_posts_once(k7, portfolio):
    """The create succeeds but its charge stays invisible for 45 s, longer than the runner's
    30 s NotFound grace: the first run's finality read gives up and keeps the week
    held; the next run resolves the same request_id."""
    w = portfolio
    w.fake.inject(CHARGE, Fault.LIST_LAG, visible_after_s=45)  # past the 30 s grace
    first = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    rid = first.ops[0].request_id
    assert len(rid_objects(w, "charges", rid)) == 1  # the create itself landed
    assert "NotFound" in (first.detail or "")  # ...but the lagged finality read missed it
    assert not first.post.ok
    assert book_of(w).pending_collection("loan-a") is not None
    assert book_of(w).week_record("loan-a", k7.W42) is None
    w.fake.advance(30)
    second = collect(w, "loan-a", k7.W42)
    assert second.decision.computed["resend"] is True
    assert second.decision.computed["request_id"] == first.ops[0].request_id
    assert second.post.ok, second.post.detail
    assert w.fake.creates(CHARGE) == 1
    assert book_of(w).week_record("loan-a", k7.W42).collected == k7.usd("1000.00")


def test_list_lag_inside_the_not_found_grace_posts_in_one_run(k7, portfolio):
    """A 20 s lag sits inside the 30 s NotFound grace: the finality loop waits it out
    and the first run posts."""
    w = portfolio
    w.fake.inject(CHARGE, Fault.LIST_LAG, visible_after_s=20)
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert out.post.ok, out.post.detail
    assert w.fake.creates(CHARGE) == 1
    assert book_of(w).pending_collection("loan-a") is None


def test_a_500_that_still_applies_pays_once(k7, portfolio):
    w = portfolio
    w.fake.inject(CAT, Fault.SERVER_ERROR, applied=True)
    n = len(w.fake.requests)
    out = disburse(w, "loan-c")
    (result,) = out.results["transfer"]
    assert (result.body or {}).get(RECOVERED) == "read_back"  # the 500 went to read-back
    assert ("GET", "/api/v1/connected_account_transfers", None) in w.fake.requests[n:]
    assert len(sends(w, CAT, out.ops[0].request_id)) == 1  # landed, so never resent
    assert out.post.ok, out.post.detail
    assert len(rid_objects(w, "connected_account_transfers", out.ops[0].request_id)) == 1
    assert book_of(w).disbursed("loan-c") == out.decision.computed["amount"]


def test_a_429_then_a_crash_pays_once(k7, portfolio):
    """A 429 (nothing processed) is resent on the same request_id; that send lands and the
    adapter crashes before recording it; the reconcile hook finds the landed transfer."""
    w = portfolio
    w.fake.inject(CAT, Fault.RATE_LIMIT)
    w.fake.crash_after_next(CAT)
    n = len(w.fake.requests)
    out = disburse(w, "loan-c")
    posts = [q for q in w.fake.requests[n:] if q[1].endswith("/connected_account_transfers/create")]
    assert len(posts) == 2  # the 429, then the resend that landed
    rid = out.ops[0].request_id
    assert len(sends(w, CAT, rid)) == 2  # both sends carried this one request_id
    (result,) = out.results["transfer"]
    assert (result.body or {}).get(RECOVERED) == "reconcile"  # recovered after the crash
    assert out.post.ok, out.post.detail
    assert len(rid_objects(w, "connected_account_transfers", rid)) == 1


def test_duplicate_delivery_posts_once(k7, portfolio):
    w = portfolio
    w.fake.inject(CHARGE, Fault.DUPLICATE_DELIVERY)
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    (result,) = out.results["charge"]
    assert (result.body or {}).get(RECOVERED) == "duplicate_request"  # the duplicate's path
    assert out.post.ok, out.post.detail
    assert w.fake.creates(CHARGE) == 1
    entries = len(book_of(w).ledger.entries)
    ctx = w.ctx(COLLECT, w.borrowers["loan-a"])
    decision = PolicyDecision(out.proposal.risk_tier, out.proposal.reasons, out.proposal.computed)
    sc = StepContext(
        out.proposal.intent,
        decision,
        ctx,
        out.proposal.intent_hash,
        out.results,
        out.proposal.proposal_id,
    )
    assert COLLECT.postcondition(sc).ok  # the postcondition delivered again
    assert len(book_of(w).ledger.entries) == entries
    book = book_of(w)
    with pytest.raises(DuplicatePosting):
        book.close_week(replace(book.week_record("loan-a", k7.W42), collected=k7.usd("1.00")))
    entry = next(e for e in book.ledger.entries if e.key.startswith("disburse:"))
    assert (
        book.settle_disbursement(
            entry.request_id, entry.provider_id, k7.usd("8000.00"), settled_at=k7.T0
        )
        is False
    )


def test_suspended_charge_needs_an_operator_and_leaves_the_week_open(k7, portfolio):
    w = portfolio
    w.fake.suspend_next(CHARGE)
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert not out.post.ok
    assert "SUSPENDED" in out.post.detail
    book = book_of(w)
    assert book.week_record("loan-a", k7.W42) is None
    assert book.pending_collection("loan-a") is not None


def test_non_funds_rejection_is_not_booked_as_arrears(k7, portfolio):
    w = portfolio
    w.fake.refuse_next(CHARGE)
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert not out.post.ok
    book = book_of(w)
    assert book.week_record("loan-a", k7.W42) is None
    assert book.pending_collection("loan-a") is None
    assert book.ledger.arrears("loan-a").is_zero()


def test_concurrent_writers_cannot_both_win(k7, world):
    item = world.store.get(BOOK_KEY)
    update_book(world.store, lambda b: b.fund("capital-2", k7.usd("1.00")))
    with pytest.raises(VersionConflict):
        world.store.put(BOOK_KEY, item.value, expected_version=item.version)


def test_cross_tenant_and_scope_are_refused(k7, portfolio):
    w = portfolio
    with pytest.raises(TenantScopeViolation):
        w.run(
            COLLECT,
            {"loan_id": "loan-a", "week": k7.W42},
            tenant=w.borrowers["loan-b"],
            now=k7.AFTER_W42,
        )
    with pytest.raises(TenantScopeViolation):
        w.run(RECONCILE, {}, tenant=w.borrowers["loan-a"])
    with pytest.raises(ValidationFailed):
        COLLECT.validate({"loan_id": "loan-a", "week": k7.W42, "amount": "5"})
    with pytest.raises(ValidationFailed):
        DISBURSE.validate({"loan_id": "loan-c", "connected_account_id": "x"})


def test_no_portfolio_blocks_everything(k7, world):
    empty = InMemoryKitStore()
    for spec, raw, tenant in (
        (COLLECT, {"loan_id": "loan-a", "week": k7.W42}, world.borrowers["loan-a"]),
        (DISBURSE, {"loan_id": "loan-a"}, world.borrowers["loan-a"]),
        (RECONCILE, {}, k7.PLATFORM),
    ):
        ctx = replace(world.ctx(spec, tenant), platform_store=empty)
        assert spec.policy(spec.validate(raw), ctx).risk_tier is RiskTier.BLOCKED


def test_policies_need_only_the_platform_store(k7, portfolio):
    w = portfolio
    w.fake.move_to(k7.AFTER_W42)
    for spec, raw, tenant in (
        (COLLECT, {"loan_id": "loan-a", "week": k7.W42}, w.borrowers["loan-a"]),
        (DISBURSE, {"loan_id": "loan-c"}, w.borrowers["loan-c"]),
        (RECONCILE, {}, k7.PLATFORM),
    ):
        ctx = w.ctx(spec, tenant)
        assert ctx.store.get(BOOK_KEY) is None
        decision = spec.policy(spec.validate(raw), ctx)
        assert decision.risk_tier is not RiskTier.BLOCKED, decision.reasons


# Rule M2: every book mints its own id


def test_two_books_opened_alike_never_share_request_ids(k7):
    first = k7.make_portfolio()
    second = k7.make_portfolio(fake=first.fake)  # same shared account, same seed
    assert book_of(first).book_id != book_of(second).book_id
    a = disburse(first, "loan-a")
    b = disburse(second, "loan-a")
    assert a.post.ok, a.post.detail
    assert b.post.ok, b.post.detail
    assert a.ops[0].request_id != b.ops[0].request_id
    assert first.fake.creates(CAT) == 2  # a new transfer, nothing adopted
    assert set(book_of(first).issued_request_ids()).isdisjoint(book_of(second).issued_request_ids())


def test_book_id_is_minted_not_defaulted():
    import inspect

    from awx_k7_lending.book import LoanBook, PortfolioConfig

    assert "portfolio_id" not in inspect.signature(PortfolioConfig).parameters
    assert inspect.signature(LoanBook).parameters["book_id"].default is inspect.Parameter.empty


# Rule M1: a new attempt only after a definitive failure


def test_failed_tranche_gets_a_new_request_id_exactly_once(k7, world):
    w = world
    w.fake.fail_next(CAT)
    failed = disburse(w, "loan-a")
    assert not failed.post.ok
    assert "failed" in failed.post.detail
    assert book_of(w).pending_for("loan-a") is None
    retry = disburse(w, "loan-a", w.fake.now() + timedelta(minutes=5))
    assert retry.state == "SUCCEEDED", retry.detail  # its own approval
    assert retry.decision.computed["attempt"] == 2
    assert retry.ops[0].request_id != failed.ops[0].request_id
    assert [o.request_id for o in sends(w, CAT)] == [
        failed.ops[0].request_id,
        retry.ops[0].request_id,
    ]
    assert recon(w).post.ok


def test_lost_tranche_is_re_emitted_on_the_same_request_id(k7, world):
    w = world
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    assert not first.post.ok
    assert len(sends(w, CAT)) == 1  # empty reconcile read: no in-run resend (r4)
    held = book_of(w).pending_for("loan-a")
    assert held is not None
    assert book_of(w).attempt("disburse", "loan-a", "1") == 1
    assert w.fake.creates(CAT) == 0
    again = disburse(w, "loan-a", w.fake.now() + timedelta(minutes=5))
    assert again.decision.computed["resend"] is True
    assert again.ops[0].request_id == first.ops[0].request_id
    assert again.ops[0].body == first.ops[0].body
    assert again.post.ok, again.post.detail
    assert w.fake.creates(CAT) == 1


def test_refused_charge_moves_to_a_new_attempt_only_when_read_back_is_empty(k7, portfolio):
    w = portfolio
    w.fake.refuse_next(CHARGE)
    first = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert not first.post.ok
    assert book_of(w).attempt("collect", "loan-a", k7.W42) == 2
    second = collect(w, "loan-a", k7.W42, w.fake.now() + timedelta(hours=1))
    assert second.ops[0].request_id != first.ops[0].request_id
    assert second.post.ok, second.post.detail
    assert w.fake.creates(CHARGE) == 1


def test_refusal_whose_read_back_finds_an_object_is_not_retried(k7, portfolio):
    w = portfolio
    w.fake.refuse_after_create_next(CHARGE)
    first = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert book_of(w).attempt("collect", "loan-a", k7.W42) == 1  # found: not definitive
    if not first.post.ok:
        again = collect(w, "loan-a", k7.W42, w.fake.now() + timedelta(minutes=1))
        assert again.decision.computed["request_id"] == first.ops[0].request_id
    assert w.fake.creates(CHARGE) == 1
    assert book_of(w).week_record("loan-a", k7.W42).collected == k7.usd("1000.00")


def test_a_released_request_id_that_settles_is_a_break(k7, world):
    w = world
    w.fake.fail_next(CAT)
    failed = disburse(w, "loan-a")
    rid = failed.ops[0].request_id
    assert rid in book_of(w).released()
    w.fake.mark_settled(rid)  # it was not failed
    out = recon(w)
    assert not out.post.ok
    assert rid in out.post.detail


@pytest.mark.parametrize("op_name", [CAT, CHARGE])
def test_unknown_movement_whose_resend_is_refused_moves_money_once(k7, portfolio, op_name):
    """r3: a refusal at create is definitive only for a first send. The first send is lost,
    the next run's re-send is refused with an empty read-back, then the original lands late."""
    w = portfolio
    w.fake.move_to(k7.AFTER_W42)
    run = (
        (lambda: disburse(w, "loan-c"))
        if op_name == CAT
        else (lambda: collect(w, "loan-a", k7.W42))
    )
    w.fake.lose_next(op_name)
    first = run()
    assert len(sends(w, op_name, first.ops[0].request_id)) == 1
    w.fake.refuse_next(op_name)
    w.fake.advance(300)
    second = run()
    assert second.decision.computed["resend"] is True
    assert not second.post.ok
    book = book_of(w)
    if op_name == CAT:
        assert book.pending_for("loan-c") is not None
        assert book.attempt("disburse", "loan-c", "1") == 1
    else:
        assert book.pending_collection("loan-a") is not None
        assert book.attempt("collect", "loan-a", k7.W42) == 1
        assert book.week_record("loan-a", k7.W42) is None
    w.fake.land(first.ops[0])  # the original lands late
    w.fake.advance(300)
    third = run()
    assert third.decision.computed["request_id"] == first.ops[0].request_id
    assert all(o.request_id == first.ops[0].request_id for o in third.ops)
    assert third.post.ok, third.post.detail
    assert {o.request_id for o in sends(w, op_name)} >= {first.ops[0].request_id}
    rids = {
        o["request_id"]
        for o in w.fake.objects("charges" if op_name == CHARGE else "connected_account_transfers")
    }
    assert first.ops[0].request_id in rids
    if op_name == CAT:
        assert book_of(w).disbursed("loan-c") == first.decision.computed["amount"]
    else:
        assert book_of(w).week_record("loan-a", k7.W42).collected == k7.usd("1000.00")
    assert recon(w).post.ok


@pytest.mark.parametrize("shape", ["f13_refused", "f13_multiple"])
def test_uncertain_send_shapes_keep_the_money_in_flight(k7, world, shape):
    """Kit-side shape test (the real executor's answers are tested further down):
    a refused resend comes back OUTCOME_UNKNOWN with the refusal in
    body["refused"], and two objects under one request_id come back OUTCOME_UNKNOWN
    multiple_objects. Both are money still in flight: hold kept, no new attempt, a person
    past the margin."""
    w = world
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    getattr(w.fake, f"{shape}_next")(CAT)
    second = disburse(w, "loan-a", w.fake.now() + timedelta(minutes=5))
    assert second.results["transfer"][0].status is OperationStatus.OUTCOME_UNKNOWN
    assert not second.post.ok
    book = book_of(w)
    assert book.pending_for("loan-a") is not None
    assert book.attempt("disburse", "loan-a", "1") == 1
    assert not book.released()
    if shape == "f13_multiple":  # two objects really carry the request_id: never adopted
        w.fake.land(first.ops[0])
        w.fake.duplicate("connected_account_transfers", first.ops[0].request_id)
        r = recon(w)
        assert not r.post.ok
        assert "share request_id" in r.post.detail
    late = disburse(w, "loan-a", w.fake.now() + timedelta(days=6))
    assert late.state == "BLOCKED"
    assert "a person must resolve it" in late.decision.reasons[0]
    assert not late.ops


def test_a_hold_past_the_dedup_margin_goes_to_a_person(k7):
    w = k7.make_portfolio(fake=k7.K7Fake(k7.usd(k7.SHARED_WALLET)))
    w.fake.inject(CAT, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=8 * 86400)
    disburse(w, "loan-a")
    count = len(w.fake.executed)
    late = disburse(w, "loan-a", w.fake.now() + timedelta(days=6))
    assert late.state == "BLOCKED"
    assert "a person must resolve it" in late.decision.reasons[0]
    assert not late.ops
    assert len(w.fake.executed) == count
    w.fake.advance(2 * 86400)  # past the ~7-day window a resend would create a second object
    assert disburse(w, "loan-a").state == "BLOCKED"
    assert w.fake.creates(CAT) == 1


# Rule M4: holds belong to the run that placed them


def test_identical_proposal_cannot_release_an_unknown_tranche_hold(k7, world):
    w = world
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    held = book_of(w).pending_for("loan-a")
    assert held is not None
    assert held.owner == first.proposal.proposal_id
    rejected = disburse(w, "loan-a", w.fake.now() + timedelta(minutes=1), decide=k7.reject)
    assert rejected.abandoned == "REJECTED"  # on_abandon ran in the runner
    assert rejected.proposal.proposal_id != first.proposal.proposal_id
    ctx = w.ctx(DISBURSE, w.borrowers["loan-a"])
    decision = PolicyDecision(first.proposal.risk_tier, (), first.proposal.computed)
    DISBURSE.on_abandon(
        StepContext(
            first.proposal.intent, decision, ctx, first.proposal.intent_hash, {}, "another-proposal"
        ),
        "EXPIRED",
    )  # even carrying the first proposal's intent_hash
    idle = StepContext(
        rejected.proposal.intent,
        decision,
        ctx,
        rejected.proposal.intent_hash,
        {},
        rejected.proposal.proposal_id,
    )
    assert not DISBURSE.postcondition(idle).ok  # a run that sent nothing
    assert book_of(w).pending_for("loan-a") == held
    assert w.fake.creates(CAT) == 0
    third = disburse(w, "loan-a", w.fake.now() + timedelta(minutes=2))
    assert third.ops[0].request_id == first.ops[0].request_id
    assert third.post.ok, third.post.detail
    assert w.fake.creates(CAT) == 1


def test_a_run_that_sent_nothing_cannot_close_a_held_week(k7, portfolio):
    w = portfolio
    w.fake.lose_next(CHARGE)
    collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert book_of(w).pending_collection("loan-a") is not None
    zero = k7.usd("0.00")
    with pytest.raises(LedgerError, match="in flight"):
        update_book(
            w.store,
            lambda b: b.close_week(
                WeekRecord("loan-a", k7.W42, k7.usd("1000.00"), zero, None, None, None)
            ),
        )


def test_scheduled_run_contexts_are_platform_read_for_movements(k7, portfolio):
    """Rule 5: finality and reconcile reads of body-bound movements are headerless."""
    w = portfolio
    collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    movement_reads = [
        q
        for q in w.fake.requests
        if q[0] == "GET"
        and (
            q[1].startswith("/api/v1/charges")
            or q[1].startswith("/api/v1/connected_account_transfers")
        )
    ]
    assert movement_reads
    assert all(q[2] is None for q in movement_reads)
    borrower_reads = [q for q in w.fake.requests if q[1] == "/api/v1/balances/history"]
    assert all(q[2] == acct(w, "loan-a") or q[2] == acct(w, "loan-b") for q in borrower_reads)


# A stale first-send decision never builds under another run's hold


def _with_race(spec, other_run):
    """The same spec, except that `other_run` runs (and places its hold) between this run's
    rule 1 re-check and its build: the Step Functions window between Recheck and Build, or
    a schedule run and an agent run decided together."""
    (step,) = spec.steps
    fired = []

    def build(ctx):
        if not fired:
            fired.append(other_run())
        return step.build(ctx)

    return replace(spec, steps=(replace(step, build=build),)), fired


@pytest.mark.parametrize("op_name", [CAT, CHARGE])
def test_stale_first_send_decision_cannot_release_another_runs_hold(k7, portfolio, op_name):
    """Run A's send is lost (it may land later). Run B decided before A built, so its decision
    says first send; B's send would be refused. B must not build at all: a refusal of its send
    would release A's hold, and the next proposal would pay on attempt 2 while A's original
    lands. Pays (or collects) exactly once."""
    w = portfolio
    w.fake.move_to(k7.AFTER_W42)
    if op_name == CAT:
        # Enough cash that the full tranche still fits after A's hold, so B's re-sized plan
        # matches its approval and only the hold guard can stop it.
        update_book(w.store, lambda b: b.fund("capital-2", k7.usd("20000.00")))
        lid, spec, raw = "loan-c", DISBURSE, {"loan_id": "loan-c"}
        hold = lambda b: b.pending_for(lid)  # noqa: E731
        kind = "connected_account_transfers"
    else:
        lid, spec, raw = "loan-a", COLLECT, {"loan_id": "loan-a", "week": k7.W42}
        hold = lambda b: b.pending_collection(lid)  # noqa: E731
        kind = "charges"

    def run_a():
        w.fake.lose_next(op_name)
        out = w.run(spec, raw, tenant=w.borrowers[lid], proposed_by="scheduler")
        w.fake.refuse_next(op_name)  # a plain refusal of B's send at create
        return out

    raced, fired = _with_race(spec, run_a)
    b = w.run(raced, raw, tenant=w.borrowers[lid], proposed_by="agent-user")
    (a,) = fired
    rid = a.ops[0].request_id
    assert b.proposal.computed["resend"] is False  # B's decision never saw A's hold
    assert b.proposal.computed["request_id"] == rid  # ...and names the same money
    assert "ApprovalBindingError" in (b.detail or "")  # fail closed before sending
    assert len(sends(w, op_name, rid)) == 1  # only A's send; B sent nothing
    held = hold(book_of(w))
    assert held is not None
    assert held.owner == a.proposal.proposal_id
    attempt_slot = "1" if op_name == CAT else k7.W42
    attempt_kind = "disburse" if op_name == CAT else "collect"
    assert book_of(w).attempt(attempt_kind, lid, attempt_slot) == 1
    w.fake.clear_faults(op_name)
    w.fake.land(a.ops[0])  # A's original lands late
    w.fake.advance(60)
    nxt = w.run(spec, raw, tenant=w.borrowers[lid])
    assert nxt.proposal.computed["request_id"] == rid
    assert nxt.post.ok, nxt.post.detail
    assert [o["request_id"] for o in w.fake.objects(kind) if o["request_id"] == rid] == [rid]
    if op_name == CAT:
        assert w.fake.creates(CAT) == 3  # A and B's two tranches, plus this one, once
        assert book_of(w).disbursed(lid) == nxt.proposal.computed["amount"]
    else:
        assert w.fake.creates(CHARGE) == 1
        assert book_of(w).week_record(lid, k7.W42).collected == k7.usd("1000.00")


def test_a_refusal_seen_by_a_run_that_does_not_own_the_hold_is_not_definitive(k7, world):
    """Defence in depth (M4 with M1): only the hold's owner made the first send, so only its
    refusal can be definitive; any other run's refusal is of a re-send."""
    w = world
    w.fake.lose_next(CAT)
    a = disburse(w, "loan-a", proposed_by="scheduler")
    held = book_of(w).pending_for("loan-a")
    ctx = w.ctx(DISBURSE, w.borrowers["loan-a"])
    from awx_common import OperationResult

    refused = OperationResult(
        a.ops[0].request_id,
        OperationStatus.FAILED,
        400,
        error_type="ValidationFailed",
        error_code="validation_failed",
    )
    decision = PolicyDecision(a.proposal.risk_tier, (), a.proposal.computed)
    other = StepContext(
        a.proposal.intent,
        decision,
        ctx,
        a.proposal.intent_hash,
        {"transfer": (refused,)},
        "another-proposal",
    )
    assert not DISBURSE.postcondition(other).ok
    assert book_of(w).pending_for("loan-a") == held
    assert book_of(w).attempt("disburse", "loan-a", "1") == 1


def test_a_final_429_keeps_the_tranche_in_flight(k7, world):
    """A retryable FAILED never reads as a refusal, so it never burns the
    attempt, whatever the runner does after an uncertain send."""
    w = world
    w.fake.inject(CAT, Fault.RATE_LIMIT, times=20)
    out = disburse(w, "loan-a")
    (result,) = out.results["transfer"]
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "RateLimited"
    assert not out.post.ok
    book = book_of(w)
    assert book.pending_for("loan-a") is not None
    assert book.attempt("disburse", "loan-a", "1") == 1
    assert not book.released()


def test_refusal_read_back_that_finds_another_payment_is_not_adopted(k7, world):
    """An object under our request_id with another amount or account is
    never settled as this tranche."""
    w = world
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    rid = first.ops[0].request_id
    other = w.fake.land(replace(first.ops[0], body={**first.ops[0].body, "amount": 1}))
    assert other.status is OperationStatus.SUCCEEDED
    w.fake.refuse_next(CAT)
    ctx = w.ctx(DISBURSE, w.borrowers["loan-a"])
    from awx_common import OperationResult

    refused = OperationResult(
        rid,
        OperationStatus.FAILED,
        400,
        error_type="ValidationFailed",
        error_code="validation_failed",
    )
    decision = PolicyDecision(first.proposal.risk_tier, (), first.proposal.computed)
    sc = StepContext(
        first.proposal.intent,
        decision,
        ctx,
        first.proposal.intent_hash,
        {"transfer": (refused,)},
        first.proposal.proposal_id,
    )
    post = DISBURSE.postcondition(sc)
    assert not post.ok
    assert "does not match" in post.detail
    assert book_of(w).disbursed("loan-a").is_zero()
    assert book_of(w).pending_for("loan-a") is not None


# The real executor's answers while a send is uncertain


@pytest.mark.parametrize("op_name", [CAT, CHARGE])
def test_refused_resend_comes_back_unknown_from_the_real_executor(k7, portfolio, op_name):
    """No fixture shape here: the first send is lost, the operation store marks it
    may_have_landed, and the next run's re-send is refused at create. The executor itself
    returns OUTCOME_UNKNOWN with the refusal in body["refused"] and body["last_error"]; kit 7
    keeps the money held on the original request_id."""
    w = portfolio
    w.fake.move_to(k7.AFTER_W42)
    run = (
        (lambda: disburse(w, "loan-c"))
        if op_name == CAT
        else (lambda: collect(w, "loan-a", k7.W42))
    )
    step = "transfer" if op_name == CAT else "charge"
    w.fake.lose_next(op_name)
    first = run()
    rid = first.ops[0].request_id
    assert w.ops.get(rid).may_have_landed
    w.fake.refuse_next(op_name)
    w.fake.advance(300)
    second = run()
    (result,) = second.results[step]
    assert result.status is OperationStatus.OUTCOME_UNKNOWN
    assert result.error_code is None  # the resend's answer is not the original's
    assert result.body["refused"]["error_code"] == "validation_failed"
    assert "last_error" in result.body
    book = book_of(w)
    held = book.pending_for("loan-c") if op_name == CAT else book.pending_collection("loan-a")
    assert held is not None
    assert held.request_id == rid
    assert not book.released()
    w.fake.land(first.ops[0])
    w.fake.advance(300)
    third = run()
    assert third.post.ok, third.post.detail
    kind = "connected_account_transfers" if op_name == CAT else "charges"
    assert len(rid_objects(w, kind, rid)) == 1


def test_two_objects_under_one_request_id_stay_in_flight(k7, world):
    """Two objects carry the request_id (real executor: OUTCOME_UNKNOWN multiple_objects):
    nothing is adopted, the hold stays, and reconciliation reports the break."""
    w = world
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    rid = first.ops[0].request_id
    w.fake.land(first.ops[0])
    w.fake.duplicate("connected_account_transfers", rid)
    second = disburse(w, "loan-a", w.fake.now() + timedelta(minutes=5))
    (result,) = second.results["transfer"]
    assert result.status is OperationStatus.OUTCOME_UNKNOWN
    assert result.error_code == "multiple_objects"
    assert not second.post.ok
    book = book_of(w)
    assert book.pending_for("loan-a") is not None
    assert book.disbursed("loan-a").is_zero()
    assert book.attempt("disburse", "loan-a", "1") == 1
    r = recon(w)
    assert not r.post.ok
    assert "share request_id" in r.post.detail


# A retried build of the same proposal is idempotent (rule 0)


@pytest.mark.parametrize("op_name", [CAT, CHARGE])
def test_a_retried_build_of_the_same_proposal_sends_once(k7, portfolio, op_name):
    """The deployed Build task retries on service exceptions, so a build whose store write
    committed may run again for the same proposal. It must return the same operation, not
    fail closed on its own hold."""
    w = portfolio
    w.fake.move_to(k7.AFTER_W42)
    if op_name == CAT:
        lid, spec, raw, step_name = "loan-c", DISBURSE, {"loan_id": "loan-c"}, "transfer"
    else:
        lid, spec, raw, step_name = (
            "loan-a",
            COLLECT,
            {"loan_id": "loan-a", "week": k7.W42},
            "charge",
        )
    (step,) = spec.steps

    def twice(ctx):
        first = step.build(ctx)
        again = step.build(ctx)  # the retry
        assert again == first
        return again

    retried = replace(spec, steps=(replace(step, build=twice),))
    out = w.run(retried, raw, tenant=w.borrowers[lid])
    assert out.state == "SUCCEEDED", out.detail
    assert out.post.ok, out.post.detail
    (result,) = out.results[step_name]
    assert len(sends(w, op_name, result.request_id)) == 1
    book = book_of(w)
    assert book.pending_for(lid) is None
    assert book.pending_collection(lid) is None


def test_reconcile_build_twice_requests_one_report(k7, portfolio):
    """Rule 0 for the third build: the report request is built twice for the same proposal
    (the first result thrown away) and sent once; no hold, claim or snapshot is stranded."""
    w = portfolio
    (step,) = RECONCILE.steps

    def twice(ctx):
        step.build(ctx)  # thrown away
        return step.build(ctx)

    retried = replace(RECONCILE, steps=(replace(step, build=twice),))
    from awx_common import TenantContext

    out = w.run(retried, {}, tenant=TenantContext("platform"))
    assert out.post.ok, out.post.detail
    assert len([o for o in w.fake.executed if o.name == "platform_reports.create"]) == 1
    book = book_of(w)
    assert not book.pending()
    assert all(book.pending_collection(t.loan_id) is None for t in book.loans())
    assert book.last_reconcile.ok


def test_mismatched_object_under_our_request_id_stays_in_flight(k7, world):
    """The mismatch shape through the real executor: another object holds our
    request_id. It comes back OUTCOME_UNKNOWN request_id_reused, never FAILED, with no
    top-level provider_id; kit 7 never adopts it and keeps the hold for a person."""
    w = world
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    w.fake.land(replace(first.ops[0], body={**first.ops[0].body, "amount": 1}))
    second = disburse(w, "loan-a", w.fake.now() + timedelta(minutes=5))
    (result,) = second.results["transfer"]
    assert result.status is OperationStatus.OUTCOME_UNKNOWN
    assert result.error_code == "request_id_reused"
    assert result.provider_id is None
    assert not second.post.ok
    assert "a person must decide" in second.post.detail
    book = book_of(w)
    assert book.disbursed("loan-a").is_zero()
    assert book.pending_for("loan-a") is not None
    assert book.attempt("disburse", "loan-a", "1") == 1
    assert not book.released()


# The labelled DEMO collection period: a live demo closes periods in minutes


def test_demo_collection_period_runs_in_minutes_with_the_same_rules(k7):
    from awx_k7_lending.book import first_week_starting_at_or_after, next_week, week_window

    w = k7.make_portfolio(demo_period_s=300)  # 5-minute DEMO periods
    disburse(w, "loan-a")
    period = first_week_starting_at_or_after(w.fake.now() + timedelta(seconds=1), 300)
    assert period.startswith("DEMO300S-")
    assert book_of(w).next_collectible_week("loan-a") == period
    start, end = week_window(period)
    assert end - start == timedelta(minutes=5)
    w.fake.move_to(start + timedelta(seconds=30))
    w.fake.deposit(acct(w, "loan-a"), k7.usd("10000.00"))
    early = collect(w, "loan-a", period)
    assert early.state == "BLOCKED"  # the period has not ended
    iso = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert iso.state == "BLOCKED"  # a demo book takes only DEMO periods
    w2 = k7.make_portfolio(demo_period_s=300)
    disburse(w2, "loan-a")
    w2.fake.move_to(w2.fake.now() + timedelta(seconds=1))
    p2 = book_of(w2).next_collectible_week("loan-a")
    w2.fake.move_to(week_window(p2)[0] + timedelta(seconds=30))
    w2.fake.deposit(acct(w2, "loan-a"), k7.usd("10000.00"))
    out = collect(w2, "loan-a", p2, week_window(p2)[1] + timedelta(seconds=5))
    assert out.post.ok, out.post.detail
    assert out.decision.computed["collect"] == k7.usd("1000.00")  # same formula, same cap
    replay = collect(w2, "loan-a", p2)
    assert replay.state == "BLOCKED"
    assert book_of(w2).next_collectible_week("loan-a") == next_week(p2)
    assert out.ops[0].body["reference"].endswith(f"-{p2}-a1")  # labelled DEMO on Airwallex


# Read-backs that find another payment, and by-id reads after a send


def _held_tranche_with_stray(w, k7):
    """A tranche held on a lost send, with ANOTHER object (a different amount) landed under
    its request_id."""
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    w.fake.land(replace(first.ops[0], body={**first.ops[0].body, "amount": 1}))
    return first


def _post_with(w, first, result):
    from awx_common import OperationResult  # noqa: F401

    ctx = w.ctx(DISBURSE, w.borrowers["loan-a"])
    decision = PolicyDecision(first.proposal.risk_tier, (), first.proposal.computed)
    return DISBURSE.postcondition(
        StepContext(
            first.proposal.intent,
            decision,
            ctx,
            first.proposal.intent_hash,
            {"transfer": (result,)},
            first.proposal.proposal_id,
        )
    )


def test_observe_read_back_never_adopts_a_mismatched_object(k7, world):
    """An unknown result whose ?request_id= read-back finds another payment
    (different amount) is UNKNOWN 'a person must look', never settled as this tranche."""
    from awx_common import OperationResult

    w = world
    first = _held_tranche_with_stray(w, k7)
    unknown = OperationResult(
        first.ops[0].request_id, OperationStatus.OUTCOME_UNKNOWN, error_type="OutcomeUnknown"
    )
    post = _post_with(w, first, unknown)
    assert not post.ok
    assert "a person must look" in post.detail
    book = book_of(w)
    assert book.disbursed("loan-a").is_zero()
    assert book.pending_for("loan-a") is not None


@pytest.mark.parametrize(
    "code",
    [
        "request_id_reused",
        "request_id_bound_to_another_body",
        "multiple_objects",
        "first_attempt_unknown",
        "past_dedupe_margin",
    ],
)
def test_person_codes_with_a_mismatched_object_present_keep_the_hold(k7, world, code):
    """Each of the five codes is in flight for a person even though an object
    (another payment) sits under the request_id: nothing is adopted or released."""
    from awx_common import OperationResult
    from awx_k7_lending.workflow import PERSON_CODES

    assert code in PERSON_CODES
    w = world
    first = _held_tranche_with_stray(w, k7)
    result = OperationResult(
        first.ops[0].request_id,
        OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_code=code,
        body={"details": {"provider_id": "tr_stray"}},
    )
    post = _post_with(w, first, result)
    assert not post.ok
    assert "a person must decide" in post.detail
    book = book_of(w)
    assert book.disbursed("loan-a").is_zero()
    assert book.pending_for("loan-a") is not None
    assert not book.released()
    assert book.attempt("disburse", "loan-a", "1") == 1


def test_succeeded_then_not_found_by_id_is_in_flight(k7, world):
    """A SUCCEEDED result whose by-id read is NotFound is unknown, never
    absent: the hold stays, nothing is posted or released."""
    from awx_common import OperationResult

    w = world
    w.fake.lose_next(CAT)
    first = disburse(w, "loan-a")
    ghost = OperationResult(
        first.ops[0].request_id,
        OperationStatus.SUCCEEDED,
        201,
        provider_id="tr_not_visible_yet",
        provider_status="NEW",
    )
    post = _post_with(w, first, ghost)
    assert not post.ok
    assert "not visible by id" in post.detail
    book = book_of(w)
    assert book.pending_for("loan-a") is not None
    assert not book.released()
    assert book.disbursed("loan-a").is_zero()


def test_transfer_in_credits_are_not_revenue_and_the_backstop_still_subtracts(k7):
    """Pins the pessimistic backstop. The fake shows our tranche as a
    TRANSFER_IN row, not a DEPOSIT, so the row filter never counts it. The backstop still
    subtracts every tranche credited that week from the DEPOSIT total, whatever the row
    type: revenue 10,000 - 3,000 = 7,000, so the due is 700 rather than 1,000. Pessimistic
    (it under-collects, never over-collects). The live row shape is unprobed; if it is
    TRANSFER_IN, the backstop could be limited to DEPOSIT-shaped credits."""
    w = k7.make_portfolio(capital="10000.00")
    disburse(w, "loan-a")
    w.fake.move_to(k7.IN_W42)
    w.fake.deposit(acct(w, "loan-a"), k7.usd("10000.00"))
    update_book(w.store, lambda b: b.fund("capital-2", k7.usd("5000.00")))
    assert disburse(w, "loan-a").post.ok  # 3,000 lands in W42
    from awx_common import ReadRequest

    rows = w.fake.read(
        ReadRequest(
            "/api/v1/balances/history",
            {
                "from_post_at": "2026-10-12T00:00:00+00:00",
                "to_post_at": "2026-10-19T00:00:00+00:00",
            },
            acct(w, "loan-a"),
        )
    )["items"]
    assert {"DEPOSIT", "TRANSFER_IN"} <= {r["transaction_type"] for r in rows}
    out = collect(w, "loan-a", k7.W42, k7.AFTER_W42)
    assert out.decision.computed["revenue"] == k7.usd("7000.00")
    assert out.decision.computed["due"] == k7.usd("700.00")
