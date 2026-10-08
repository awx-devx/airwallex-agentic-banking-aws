"""Scenario tests through the real runner (local_runner over FakeAirwallex)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from awx_common import Money, RiskTier
from awx_common.approvals import Origin
from awx_common.store import InMemoryKitStore
from awx_k1_treasury.book import BOOK_ID_KEY, load_book, load_book_id, seed_book
from awx_k1_treasury.keys import MissingBookId, book_id_of
from awx_k1_treasury.locks import LockState, quote_owner, read_lock, read_ratelock
from awx_k1_treasury.reads import READS
from awx_spine.fake import Fault

SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
OFFICE = {"obligation_ids": ["ob-office-usd"]}
SHIPPING = {"obligation_ids": ["ob-shipping"]}


def eur(a: str) -> Money:
    return Money.of(a, "EUR")


def usd(a: str) -> Money:
    return Money.of(a, "USD")


def lock(env, oid="ob-supplier-eur"):
    return read_lock(env.store, oid)


def reseed_v2(env, oid, amount=None):
    book = load_book(env.store)
    obs = tuple(
        replace(o, version="2", amount=amount or o.amount) if o.obligation_id == oid else o
        for o in book.obligations
    )
    seed_book(env.store, replace(book, book_version=book.book_version + "-v2", obligations=obs))


# --- the kit's acceptance scenario (fake run; test_acceptance.py has the sandbox one) -------


def test_acceptance_contradiction_changes_tier_plan_stays_above_floor_and_replay_is_idempotent(
    env, contradiction
):
    contradicted_raw = {**SUPPLIER, "contradictions": [contradiction]}
    env.lock(SUPPLIER)
    spec = env.specs["execute_plan"]
    supported = spec.policy(spec.validate(SUPPLIER), env.ctx())
    contradicted = spec.policy(spec.validate(contradicted_raw), env.ctx())
    assert supported.computed["conversions"] == contradicted.computed["conversions"]
    assert supported.risk_tier is RiskTier.AUTO
    assert contradicted.risk_tier is RiskTier.SINGLE
    (conv,) = contradicted.computed["conversions"]
    assert conv["buy"] == eur("6038.55")  # 7525.70 + 12.85 SWIFT fee - 1500 headroom
    assert conv["sell"] == read_ratelock(env.store, "EUR")["sell"]  # exact, from the lock

    seen_sell = []

    def approve_while_the_rate_moves(proposal):
        seen_sell.append(proposal.computed["conversions"][0]["sell"])
        env.move_rate("EUR", "1.03")  # the market moves between approval and execution

    first = env.execute(
        contradicted_raw, approvers=["approver"], during_approval=approve_while_the_rate_moves
    )
    assert first.state == "SUCCEEDED", (first.detail, first.postcondition)
    # Sized at 12.85; the fake charges 6.21, so the wallet ends 6.64 above its floor.
    assert env.money("EUR") == eur("506.64")
    assert env.money("USD") >= usd("20000")
    assert len(env.conversions()) == 1
    assert seen_sell == [conv["sell"]]  # the approver saw the exact sell (M3)
    assert env.conversions()[0]["sell_amount"] == conv["sell"].amount  # the locked price
    assert [t["status"] for t in env.transfers()] == ["PAID"]
    assert lock(env).state is LockState.PAID

    # The same obligation proposed again with other evidence, after a re-lock: blocked.
    again = env.pay(SUPPLIER)
    assert again.state == "BLOCKED"
    assert len(env.conversions()) == 1
    assert len(env.transfers()) == 1


def test_rate_moving_between_approval_and_execution_does_not_go_stale(env, contradiction):
    """The approval binds the locked quote, not a live rate, so rule 1's re-run is not STALE
    when the market rate moves."""
    env.lock(SUPPLIER)
    out = env.execute(
        {**SUPPLIER, "contradictions": [contradiction]},
        approvers=["approver"],
        during_approval=lambda p: env.move_rate("EUR", "0.9"),
    )
    assert out.state == "SUCCEEDED", out.detail
    assert len(env.conversions()) == 1


def test_expired_rate_lock_is_blocked_and_relock_works(env):
    env.lock(SUPPLIER)
    env.advance(3600 + 1)  # HR_1 quote expired
    out = env.execute(SUPPLIER)
    assert out.state == "BLOCKED"
    assert any("expired" in r for r in out.proposal.reasons)
    env.lock(SUPPLIER)
    assert env.execute(SUPPLIER).state == "SUCCEEDED"


def test_rate_lock_expiring_while_awaiting_approval_goes_stale(env, contradiction):
    env.lock(SUPPLIER)
    out = env.execute(
        {**SUPPLIER, "contradictions": [contradiction]},
        approvers=["approver"],
        during_approval=lambda p: env.advance(3600),
    )
    assert out.state == "FAILED"
    assert "changed since the proposal" in (out.detail or "")
    assert env.conversions() == []


def test_no_rate_lock_is_blocked(env):
    out = env.execute(SUPPLIER)
    assert out.state == "BLOCKED"
    assert any("no rate lock" in r for r in out.proposal.reasons)
    assert env.port.executed == []


def test_rate_lock_for_another_amount_is_blocked(env):
    env.lock(SUPPLIER)
    env.spend_elsewhere("EUR", "100")  # EUR headroom fell: the plan now buys more
    out = env.execute(SUPPLIER)
    assert out.state == "BLOCKED"
    assert any("re-lock" in r for r in out.proposal.reasons)


def test_a_used_rate_lock_is_blocked(env):
    env.lock(SUPPLIER)
    qid = read_ratelock(env.store, "EUR")["quote_id"]
    assert env.execute(SUPPLIER).state == "SUCCEEDED"
    assert quote_owner(env.store, qid) is not None
    reseed_v2(env, "ob-supplier-eur")  # even a new version: lock used, obligation PAID
    assert env.execute(SUPPLIER).state == "BLOCKED"


def test_lock_rate_is_auto_and_moves_no_money(env):
    usd_before = env.money("USD")
    out = env.lock(SUPPLIER)
    assert out.proposal.risk_tier is RiskTier.AUTO
    assert env.port.names() == ["fx.quotes.create"]
    assert env.money("USD") == usd_before
    assert "request_id" not in env.port.executed[0].body  # quotes ignore request_id


def test_lock_rate_blocks_when_nothing_to_convert(env):
    out = env.run("lock_rate", SHIPPING)
    assert out.state == "BLOCKED"


# --- repeat payments, fresh books and sandbox finality through the runner ------------------


def test_supported_then_contradicted_back_to_back_pay_once(env, contradiction):
    assert env.pay(SUPPLIER).state == "SUCCEEDED"
    second = env.pay({**SUPPLIER, "contradictions": [contradiction]}, approvers=["approver"])
    assert second.state == "BLOCKED"
    assert len(env.conversions()) == 1
    assert len(env.transfers()) == 1


def test_fresh_book_on_the_same_sandbox_pays_for_itself(env, make_env):
    """M2: a fresh book seeded the same way gets disjoint request_ids and its own payment."""
    first = env.execute(OFFICE)
    assert first.state == "SUCCEEDED", first.detail
    second_env = make_env(fake=env.fake)
    assert load_book_id(second_env.store) != load_book_id(env.store)
    second = second_env.execute(OFFICE)
    assert second.state == "SUCCEEDED", second.detail
    assert second.results["transfer"][0].request_id != first.results["transfer"][0].request_id
    assert len(env.transfers()) == 2


def test_paid_at_v1_is_not_paid_again_at_v2(env):
    assert env.pay(SUPPLIER).state == "SUCCEEDED"
    reseed_v2(env, "ob-supplier-eur")
    assert env.pay(SUPPLIER).state == "BLOCKED"
    assert len(env.transfers()) == 1


def test_transfer_waits_in_processing_without_a_simulation_call(make_env):
    env = make_env(advance=False)
    out = env.execute(OFFICE)
    assert out.state == "FAILED"  # finality times out: nothing moves without simulation
    assert [t["status"] for t in env.transfers()] == ["PROCESSING"]
    assert lock(env, "ob-office-usd").state is LockState.SUBMITTED
    reseed_v2(env, "ob-office-usd")
    assert env.execute(OFFICE).state == "BLOCKED"  # in flight at v1
    assert len(env.transfers()) == 1


def test_sandbox_advance_drives_sent_then_paid(env):
    out = env.execute(OFFICE)
    assert out.state == "SUCCEEDED"
    assert [t["status"] for t in env.transfers()] == ["PAID"]
    assert env.port.names().count("transfers.simulate_transition") == 2


def test_insufficient_funds_on_conversion_releases_and_a_relock_retries(env):
    env.lock(SUPPLIER)
    env.fake.inject("fx.conversions.create", Fault.INSUFFICIENT_FUNDS)
    out = env.execute(SUPPLIER)
    assert out.state == "FAILED"
    assert out.results["convert"][0].error_code == "insufficient_funds"
    assert lock(env).state is LockState.RELEASED  # a refusal at the first send
    assert env.execute(SUPPLIER).state == "BLOCKED"  # the quote was burned: re-lock
    env.lock(SUPPLIER)
    retry = env.execute(SUPPLIER)
    assert retry.state == "SUCCEEDED", retry.detail
    assert retry.results["convert"][0].request_id != out.results["convert"][0].request_id
    assert lock(env).attempt == 2
    assert len(env.conversions()) == 1


def test_insufficient_funds_on_transfer_needs_a_person(env):
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    out = env.pay(SUPPLIER)
    assert out.results["transfer"][0].error_code == "balance_insufficient"
    assert len(env.conversions()) == 1
    assert lock(env).state is LockState.NEEDS_PERSON
    assert env.pay(SUPPLIER).state == "BLOCKED"


def test_balance_spent_elsewhere_just_before_the_conversion_releases(env):
    """A build raised before emitting anything; the submission
    ledger shows nothing was sent under this attempt, so the lock is released."""
    env.lock(SUPPLIER)
    env.port.before_read("/fx/conversions", lambda: env.spend_elsewhere("USD", "100"))
    out = env.execute(SUPPLIER)
    assert out.state == "FAILED"
    assert "BalanceChanged" in (out.detail or "")
    assert env.conversions() == []
    assert lock(env).state is LockState.RELEASED
    assert lock(env).attempt == 1


def test_last_line_reserve_check_before_transfer(env):
    env.lock(SUPPLIER)
    env.port.before_read("/api/v1/transfers", lambda: env.spend_elsewhere("EUR", "600"))
    out = env.execute(SUPPLIER)
    assert "ReserveBreach" in (out.detail or "")
    assert [t for t in env.transfers() if t["reference"] != "poc-i1-elsewhere"] == []
    assert lock(env).state is LockState.NEEDS_PERSON  # converted, not paid


def test_cancelled_transfer_with_nothing_converted_is_released_and_retried(env):
    env.cancel_next_transfer()
    first = env.execute(SHIPPING)
    assert first.state == "FAILED"
    assert lock(env, "ob-shipping").state is LockState.RELEASED
    retry = env.execute(SHIPPING)
    assert retry.state == "SUCCEEDED", retry.detail
    assert retry.results["transfer"][0].request_id != first.results["transfer"][0].request_id
    assert [t["status"] for t in env.transfers()] == ["CANCELLED", "PAID"]


def test_cancelled_transfer_after_a_conversion_needs_a_person(env):
    env.cancel_next_transfer()
    env.pay(SUPPLIER)
    assert lock(env).state is LockState.NEEDS_PERSON
    assert env.pay(SUPPLIER).state == "BLOCKED"


def test_two_runners_sharing_one_store_pay_once(env, make_env):
    other = make_env(fake=env.fake, store=env.store)
    assert env.execute(OFFICE).state == "SUCCEEDED"
    assert other.execute(OFFICE).state == "BLOCKED"
    assert len(env.transfers()) == 1


def test_contradicted_plan_without_approval_expires_and_sends_nothing(env, contradiction):
    env.lock(SUPPLIER)
    out = env.execute({**SUPPLIER, "contradictions": [contradiction]})
    assert out.state == "EXPIRED"
    assert env.port.names() == ["fx.quotes.create"]
    assert lock(env) is None


def test_proposer_cannot_self_approve(env, contradiction):
    env.lock(SUPPLIER)
    out = env.execute({**SUPPLIER, "contradictions": [contradiction]}, approvers=["requester"])
    assert out.state == "REJECTED"
    assert env.conversions() == []


def test_agent_floor_is_auto_and_scheduled_runs_keep_the_policy_tier(env):
    out = env.execute(OFFICE, origin=Origin.SCHEDULE)
    assert out.proposal.risk_tier is RiskTier.AUTO
    assert env.specs["execute_plan"].agent_min_tier is RiskTier.AUTO
    assert env.specs["release_obligation"].agent_min_tier is RiskTier.SINGLE


def test_balance_move_between_approval_and_recheck_is_stale(env, contradiction):
    """Balances seen are in computed, which the proposal hash covers, so any move after
    approval fails STALE."""
    env.lock(SUPPLIER)
    out = env.execute(
        {**SUPPLIER, "contradictions": [contradiction]},
        approvers=["approver"],
        during_approval=lambda p: env.spend_elsewhere("GBP", "1"),
    )
    assert out.state == "FAILED"
    assert "changed since the proposal" in (out.detail or "")
    assert env.conversions() == []


def test_book_change_after_approval_is_stale(env, contradiction):
    env.lock(SUPPLIER)
    book = load_book(env.store)
    out = env.execute(
        {**SUPPLIER, "contradictions": [contradiction]},
        approvers=["approver"],
        during_approval=lambda p: seed_book(env.store, replace(book, book_version="demo-2")),
    )
    assert out.state == "FAILED"
    assert env.conversions() == []


def test_supplier_payout_that_would_breach_is_blocked(make_env):
    env = make_env({"USD": "26000", "EUR": "2000", "GBP": "1500"})
    assert env.run("lock_rate", SUPPLIER).state == "BLOCKED"
    assert env.execute(SUPPLIER).state == "BLOCKED"
    assert env.port.executed == []


def test_escalated_policy_exception_is_never_executed(env):
    out = env.execute({"obligation_ids": ["ob-contractor-gbp"]}, approvers=["a1", "a2"])
    assert out.state == "BLOCKED"
    assert env.port.executed == []


def test_unknown_obligation_is_blocked_and_model_input_is_refused(env):
    assert env.execute({"obligation_ids": ["ob-unknown"]}).state == "BLOCKED"
    from awx_k1_treasury.intake import InvalidInput

    with pytest.raises(InvalidInput):
        env.specs["execute_plan"].validate({**SUPPLIER, "amount": "1"})


def test_book_without_an_id_is_blocked_and_never_reaches_a_request_id(env):
    """M2: a store with a book but no book id is BLOCKED, and no request_id is built from a
    missing id."""
    store = InMemoryKitStore()
    seed_book(store, env.book)
    item = store.get(BOOK_ID_KEY)
    store._items.pop(BOOK_ID_KEY)  # a store written by another tool
    assert item is not None
    assert load_book_id(store) is None
    other = type(env)(env.fake, env.port, store, env.ops, env.specs, env.book)
    out = other.execute(OFFICE)
    assert out.state == "BLOCKED"
    assert any("book id" in r for r in out.proposal.reasons)
    with pytest.raises(MissingBookId):
        book_id_of({"book_id": None})


def test_demo_story_deposit_reopens_only_the_supplier_decision(make_env):
    env = make_env({"USD": "34000", "EUR": "2000", "GBP": "1500"})
    before = READS["read_treasury_plan"]({}, env.ctx())
    kinds = {d["obligation_id"]: d["decision"] for d in before["decisions"]}
    assert kinds == {
        "ob-shipping": "FUND",
        "ob-supplier-eur": "ESCALATE",
        "ob-saas-gbp": "FUND",
        "ob-office-usd": "DEFER",
        "ob-contractor-gbp": "ESCALATE",
    }
    env.deposit("USD", "8000")  # the Acme receipt arrives
    after = READS["read_treasury_plan"]({}, env.ctx())

    def key(d):
        return (d["decision"], d["amount_with_fee"], d["converted"])

    changed = sorted(
        b["obligation_id"]
        for b, a in zip(before["decisions"], after["decisions"], strict=True)
        if key(b) != key(a)
    )
    assert changed == ["ob-supplier-eur"]


def test_paid_obligation_drops_out_of_the_preview(env):
    env.pay(SUPPLIER)
    preview = READS["read_treasury_plan"]({}, env.ctx())
    assert "ob-supplier-eur" in preview["paid_or_in_flight"]
    assert preview["rate_locks"][0]["used"] is True


def test_aged_submitted_lock_is_reported_as_needing_a_person(make_env):
    """Reported by read_obligations, since no later postcondition reaches a SUBMITTED lock."""
    env = make_env(advance=False)
    env.execute(OFFICE)
    view = {o["obligation_id"]: o for o in READS["read_obligations"]({}, env.ctx())["obligations"]}
    assert view["ob-office-usd"]["lock"]["state"] == "SUBMITTED"
    assert view["ob-office-usd"]["lock"]["needs_person"] is False
    env.advance(timedelta(days=8).total_seconds())
    view = {o["obligation_id"]: o for o in READS["read_obligations"]({}, env.ctx())["obligations"]}
    assert view["ob-office-usd"]["lock"]["needs_person"] is True
    assert view["ob-office-usd"]["lock"]["days_since_submitted"] >= 7
    out = env.execute(OFFICE)
    assert any("needs a person" in r for r in out.proposal.reasons)


def test_a_retry_after_a_definitive_failure_is_a_new_execution(env):
    """The proposal ID is the Step Functions execution name: a retry after RELEASED must not
    reach the failed attempt's execution."""
    env.cancel_next_transfer()
    first = env.execute(SHIPPING)
    retry = env.execute(SHIPPING)
    assert retry.proposal.proposal_id != first.proposal.proposal_id
    assert retry.state == "SUCCEEDED"


def test_version_bump_in_one_book_keeps_the_request_id(env, make_env):
    """Within one book (same book id) a version bump does not mint a new request_id. A second
    store with the same book id and a corrected amount finds the first transfer under that
    request_id with another amount, and fails closed without sending."""
    from awx_common.store import InMemoryKitStore
    from awx_k1_treasury.keys import transfer_request_id

    other_store = InMemoryKitStore()
    other_store._items = dict(env.store._items)  # another Lambda's view of the same book
    other = make_env(fake=env.fake, store=other_store)
    reseed_v2(other, "ob-shipping", Money.of("11000", "USD"))
    book_id = load_book_id(env.store)
    assert load_book_id(other.store) == book_id
    a = env.execute(SHIPPING)
    assert a.results["transfer"][0].request_id == transfer_request_id(book_id, "ob-shipping", 1)
    env.deposit("USD", "20000")  # enough cash that the corrected invoice is fundable
    b = other.execute(SHIPPING)
    assert b.state == "FAILED"
    assert "does not match" in (b.detail or "")
    assert len(env.transfers()) == 1


def test_a_landed_conversion_must_match_the_approved_quote_and_sell():
    from awx_common import OperationResult, OperationStatus
    from awx_k1_treasury.workflow import ConversionMismatch, check_landed_conversions

    approved = [{"quote_id": "q1", "sell": usd("100.00")}]

    def landed(**body):
        return OperationResult("r1", OperationStatus.SUCCEEDED, 201, "c1", "SETTLED", body)

    check_landed_conversions([landed(quote_id="q1", sell_amount=Decimal("100.00"))], approved)
    with pytest.raises(ConversionMismatch):
        check_landed_conversions([landed(quote_id="q2", sell_amount=Decimal("100.00"))], approved)
    with pytest.raises(ConversionMismatch):
        check_landed_conversions([landed(quote_id="q1", sell_amount=Decimal("101.00"))], approved)
    with pytest.raises(ConversionMismatch):
        check_landed_conversions([landed(quote_id="q1")], approved)


def test_reconcile_refuses_a_conversion_booked_on_another_quote(env):
    from awx_common.workflow import ReconcileUndetermined
    from awx_k1_treasury.api import find_conversion

    env.lock(SUPPLIER)
    assert env.execute(SUPPLIER).state == "SUCCEEDED"
    (sent,) = [o for o in env.port.executed if o.name == "fx.conversions.create"]
    other = type(sent)(**{**sent.__dict__, "body": {**sent.body, "quote_id": "another"}})
    with pytest.raises(ReconcileUndetermined):
        find_conversion(other, env.ctx().read)
