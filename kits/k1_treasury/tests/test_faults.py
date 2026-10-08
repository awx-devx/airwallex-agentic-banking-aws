"""Faults and kit rules M1 to M4 through the real runner, plus the two interleavings the
runner cannot express (a run dying between a step and its postcondition, a second delivery of
one build), which call the kit's builds and postcondition directly against FakeAirwallex."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from awx_common import OperationResult, OperationStatus, TenantContext
from awx_common.approvals import Proposal
from awx_common.workflow import PolicyContext, StepContext
from awx_k1_treasury.locks import LockState, get_submission, read_lock
from awx_k1_treasury.reads import READS
from awx_spine.fake import Fault

SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
OFFICE = {"obligation_ids": ["ob-office-usd"]}
SHIPPING = {"obligation_ids": ["ob-shipping"]}


def lock(env, oid):
    return read_lock(env.store, oid)


def mine(env):
    return [t for t in env.transfers() if t.get("reference") != "poc-i1-elsewhere"]


# --- faults through the runner --------------------------------------------------------------


def test_write_landing_at_timeout_but_invisible_for_a_while_is_reconciled_once(env):
    env.lock(SUPPLIER)
    env.fake.inject("fx.conversions.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=20)
    env.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=20)
    out = env.execute(SUPPLIER)
    assert out.state == "SUCCEEDED", out.detail
    assert len(env.conversions()) == 1
    assert len(env.transfers()) == 1
    assert lock(env, "ob-supplier-eur").state is LockState.PAID


def test_list_lag_after_a_successful_create(env):
    env.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=15)
    out = env.execute(OFFICE)
    assert out.state == "SUCCEEDED", out.detail
    assert len(env.transfers()) == 1


@pytest.mark.parametrize("name", ["fx.conversions.create", "transfers.create"])
def test_a_500_that_still_applies_is_reconciled_not_resent(env, name):
    env.lock(SUPPLIER)
    env.fake.inject(name, Fault.SERVER_ERROR, applied=True)
    out = env.execute(SUPPLIER)
    assert out.state == "SUCCEEDED", out.detail
    assert len(env.conversions()) == 1
    assert len(env.transfers()) == 1


def test_a_429_then_a_crash_after_the_resend_left(env):
    env.fake.inject("transfers.create", Fault.RATE_LIMIT)

    def crash(op, result):
        if result.status is OperationStatus.SUCCEEDED:
            raise RuntimeError("adapter Lambda timed out after the call left")

    env.port.after_execute("transfers.create", lambda op, r: None)  # the 429 itself
    env.port.after_execute("transfers.create", crash)  # the resend: sent, then the crash
    out = env.execute(OFFICE)
    assert out.state == "SUCCEEDED", out.detail
    assert len(env.transfers()) == 1


def test_stale_balance_read_cannot_fund_a_breach(env):
    env.lock(SUPPLIER)
    env.fake.inject("balances.current", Fault.STALE_READ, times=3)
    out = env.execute(SUPPLIER)
    assert env.money("EUR").amount >= Decimal("500")
    assert env.money("USD").amount >= Decimal("20000")
    assert len(env.transfers()) <= 1
    assert out.state in ("SUCCEEDED", "FAILED", "BLOCKED")


def test_duplicate_delivery_of_a_write_creates_one_object(env):
    env.fake.inject("transfers.create", Fault.DUPLICATE_DELIVERY)
    out = env.execute(OFFICE)
    assert out.state == "SUCCEEDED", out.detail
    assert len(env.transfers()) == 1


# --- M1: unknown money stays in flight; past the window a person decides --------------------


def test_m1_unknown_transfer_invisible_past_the_dedup_window_never_resends(make_env):
    """The transfer landed at the timeout but stays invisible for 8 days. The run resends the
    same Operation inside the window (rule 9) and is told duplicate_request_id; the lock holds
    as SUBMITTED. After the ~7-day dedupe has expired, no run sends again: a new proposal is
    BLOCKED, read_obligations says a person must act, and the release tool needs DUAL."""
    env = make_env()
    env.fake.inject(
        "transfers.create",
        Fault.TIMEOUT_AFTER_SUBMIT,
        visible_after_s=timedelta(days=8).total_seconds(),
    )
    first = env.execute(OFFICE)
    assert first.state == "FAILED"
    assert lock(env, "ob-office-usd").state is LockState.SUBMITTED
    assert lock(env, "ob-office-usd").attempt == 1
    sends = env.port.names().count("transfers.create")
    env.advance(timedelta(days=7, hours=12).total_seconds())  # dedupe expired, still invisible
    assert env.execute(OFFICE).state == "BLOCKED"
    assert env.port.names().count("transfers.create") == sends
    view = {o["obligation_id"]: o for o in READS["read_obligations"]({}, env.ctx())["obligations"]}
    assert view["ob-office-usd"]["lock"]["needs_person"] is True
    rel = env.release(
        "ob-office-usd", "invisible transfer, past the dedup window", approvers=["a1"]
    )
    assert rel.proposal.risk_tier.value == "DUAL_APPROVER"
    assert rel.state == "EXPIRED"  # one approver is not enough
    assert lock(env, "ob-office-usd").state is LockState.SUBMITTED
    assert len(env.transfers()) == 1


def test_m1_unknown_conversion_holds_the_lock_and_sends_no_transfer(env):
    env.lock(SUPPLIER)
    env.fake.inject("fx.conversions.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=10_000)
    out = env.execute(SUPPLIER)
    assert out.state == "FAILED"
    assert env.transfers() == []
    assert lock(env, "ob-supplier-eur").state is LockState.NEEDS_PERSON
    assert lock(env, "ob-supplier-eur").attempt == 1


def test_m1_refused_resend_after_an_unknown_send_is_not_definitive(make_env):
    """The first send landed (invisible), the money left, dedupe expired, and the resend is
    refused balance_insufficient. That refusal is NOT definitive: the lock must hold."""
    env = make_env(dedupe_window_s=8)
    env.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=10_000)
    env.port.after_execute("transfers.create", lambda op, r: env.spend_elsewhere("USD", "25000"))
    out = env.execute(SHIPPING)
    assert lock(env, "ob-shipping").state is not LockState.RELEASED
    assert lock(env, "ob-shipping").attempt == 1
    # The executor reports the refused resend as OUTCOME_UNKNOWN with the refusal in
    # body["last_error"] (and body["refused"]), never as FAILED.
    (res,) = out.results["transfer"]
    assert res.status is OperationStatus.OUTCOME_UNKNOWN
    assert res.body["last_error"]["error_code"] == "balance_insufficient"
    assert res.body["refused"]["error_type"] == "InsufficientFunds"
    assert res.error_code is None


# --- uncertain send result shapes (direct postcondition) -----------------------------------


def _ctx(env, proposal: Proposal, prior) -> StepContext:
    pc = PolicyContext(TenantContext("platform"), env.now(), env.ctx().read, store=env.store)
    return StepContext(
        proposal.intent, _decision(proposal), pc, proposal.intent_hash, prior, proposal.proposal_id
    )


def _decision(proposal: Proposal):
    from awx_common.workflow import PolicyDecision

    return PolicyDecision(proposal.risk_tier, proposal.reasons, proposal.computed)


def _proposal(env, action: str, raw) -> Proposal:
    from datetime import timedelta as td

    spec = env.specs[action]
    intent = spec.validate(raw)
    d = spec.policy(intent, env.ctx())
    return Proposal.create(
        kit="k1",
        action=action,
        intent=intent,
        computed=d.computed,
        risk_tier=d.risk_tier,
        reasons=d.reasons,
        proposed_by="requester",
        now=env.now(),
        ttl=td(hours=4),
        proposal_id=f"p-{len(env.outcomes)}-{env.now().timestamp()}",
    )


def _send_transfers(env, proposal: Proposal, *, deliver: bool = True):
    spec = env.specs["execute_plan"]
    ops = spec.steps[1].build(_ctx(env, proposal, {"convert": ()}))
    return [env.fake.execute(o) for o in ops] if deliver else []


@pytest.mark.parametrize(
    "shape",
    [
        {
            "body": {
                "last_error": {"error_type": "InsufficientFunds", "error_code": "x"},
                "refused": {"error_type": "InsufficientFunds", "error_code": "x"},
            },
            "error_code": None,
        },
        {
            "body": {"last_error": {"error_type": "RateLimited", "error_code": "429"}},
            "error_code": None,
        },
        {"body": {"provider_ids": ["a", "b"]}, "error_code": "multiple_objects"},
    ],
)
def test_uncertain_send_shapes_keep_the_money_in_flight(env, shape):
    """A refused resend (body["refused"]) and two objects under one request_id both come
    back OUTCOME_UNKNOWN; the lock holds, no new attempt."""
    p = _proposal(env, "execute_plan", OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))  # takes the lock
    rid = env.specs["execute_plan"].steps[1].build(_ctx(env, p, {"convert": ()}))[0].request_id
    unknown = OperationResult(
        rid, OperationStatus.OUTCOME_UNKNOWN, error_type="OutcomeUnknown", **shape
    )
    env.specs["execute_plan"].postcondition(_ctx(env, p, {"convert": (), "transfer": (unknown,)}))
    assert lock(env, "ob-office-usd").state is LockState.NEEDS_PERSON
    assert lock(env, "ob-office-usd").attempt == 1


# --- M4: holds owned by proposal_id; empty priors never release -----------------------------


def test_m4_identical_proposal_with_an_empty_prior_never_releases(env):
    """Run 1 sends its transfer and dies before its postcondition, with the list lagging.
    Another execution's postcondition (empty prior) touches nothing, and policy blocks."""
    env.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=10_000)
    p1 = _proposal(env, "execute_plan", OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p1, {}))
    _send_transfers(env, p1)  # sent; run 1 dies here
    env.advance(4 * 3600 + 60)
    p2 = _proposal(env, "execute_plan", OFFICE)
    assert p2.risk_tier.value == "BLOCKED"
    fake_p2 = Proposal.create(
        kit="k1",
        action="execute_plan",
        intent=p1.intent,
        computed=p1.computed,
        risk_tier=p1.risk_tier,
        reasons=p1.reasons,
        proposed_by="requester",
        now=env.now(),
        ttl=timedelta(hours=4),
        proposal_id="p-other",
    )
    assert fake_p2.intent_hash == p1.intent_hash  # an intent-hash owner would be shared
    res = env.specs["execute_plan"].postcondition(_ctx(env, fake_p2, {}))
    assert not res.ok
    assert lock(env, "ob-office-usd").state is LockState.LOCKED
    assert lock(env, "ob-office-usd").owner == p1.proposal_id
    assert env.execute(OFFICE).state == "BLOCKED"
    assert len(env.transfers()) == 1


def test_m4_own_redelivery_with_an_empty_prior_holds_for_a_person(env):
    """The owner's postcondition after a delivery that cannot see the earlier send: the
    submission ledger shows the transfer was emitted, so the lock is never released."""
    env.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=10_000)
    p1 = _proposal(env, "execute_plan", OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p1, {}))
    _send_transfers(env, p1)
    env.specs["execute_plan"].postcondition(_ctx(env, p1, {}))
    assert lock(env, "ob-office-usd").state is LockState.NEEDS_PERSON
    assert lock(env, "ob-office-usd").attempt == 1


def test_crash_after_record_submission_holds(env):
    """The transfer build recorded its submission (so an operation may have been returned and
    sent) and the run died: hold."""
    p1 = _proposal(env, "execute_plan", OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p1, {}))
    ops = env.specs["execute_plan"].steps[1].build(_ctx(env, p1, {"convert": ()}))
    assert get_submission(env.store, ops[0].request_id) is not None
    env.specs["execute_plan"].postcondition(_ctx(env, p1, {"convert": ()}))
    assert lock(env, "ob-office-usd").state is LockState.NEEDS_PERSON
    assert env.transfers() == []


def test_raise_before_record_submission_releases(env):
    """The build raised before it recorded or returned anything: release."""
    p1 = _proposal(env, "execute_plan", OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p1, {}))
    env.specs["execute_plan"].postcondition(_ctx(env, p1, {"convert": ()}))
    assert lock(env, "ob-office-usd").state is LockState.RELEASED
    assert lock(env, "ob-office-usd").attempt == 1


def test_two_objects_under_the_kits_request_id_hold_for_a_person(env):
    """Two transfers already hold this attempt's request_id (dedupe expired between them).
    Kit 1's own reconcile refuses to pick one (ReconcileUndetermined) before the executor's
    multiple_objects path is reached: nothing is sent and the lock goes to a person."""
    from awx_k1_treasury.book import load_book_id
    from awx_k1_treasury.keys import transfer_request_id

    p = _proposal(env, "execute_plan", OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))
    (op,) = env.specs["execute_plan"].steps[1].build(_ctx(env, p, {"convert": ()}))
    assert op.request_id == transfer_request_id(load_book_id(env.store), "ob-office-usd", 1)
    env.fake.execute(op)
    env.advance(7 * 86400 + 1)
    env.fake.execute(op)
    assert len(env.transfers()) == 2
    env.specs["execute_plan"].postcondition(_ctx(env, p, {"convert": ()}))
    assert lock(env, "ob-office-usd").state is LockState.NEEDS_PERSON
    assert "undetermined" in lock(env, "ob-office-usd").note
