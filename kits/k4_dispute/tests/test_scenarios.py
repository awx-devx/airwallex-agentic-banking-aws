"""Scenario tests through the REAL runner (local_runner over FakeAirwallex): the kit's
required acceptance test, recovery paths, faults, and kit rules M1 to M4."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from awx_common import ApprovalDecision, Decision, Money, RiskTier
from awx_common.ops import OperationStatus
from awx_k4_dispute import READS, SPECS
from awx_k4_dispute.cases import LockHeld
from awx_k4_dispute.config import DEFAULT_CONFIG, DisputeConfig, _terms
from awx_k4_dispute.deadlines import DeadlineMissed
from awx_k4_dispute.evidence import instruction_markers
from awx_k4_dispute.ledger import EntryKind, EntrySource, outcome_entries, total
from awx_k4_dispute.provider import accept_request_id, challenge_request_id, parse_dispute
from awx_k4_dispute.workflow import PolicyChanged, build_specs
from k4_harness import (
    NOW,
    Lost,
    accept_input,
    approve,
    build_at,
    challenge_input,
    evidence_text,
    fact,
    make_ctx,
    run,
)

CHALLENGE = SPECS["challenge_dispute"]
ACCEPT = SPECS["accept_dispute"]
REFER = SPECS["refer_dispute"]
CLOSE = SPECS["close_dispute"]


def small_accept(dispute_id: str = "dsp_small") -> dict:
    return accept_input(
        dispute_id,
        "15.00",
        "LOW_VALUE",
        "Delivery scan has no signature and the dispute fee exceeds the amount.",
    )


def seen(port, dispute_id):
    return READS["read_dispute"]({"dispute_id": dispute_id}, make_ctx(port, ACCEPT))["case"]


# ---- the required acceptance test -------------------------------------------------------------


def test_required_acceptance_strong_fraud_challenged_rejected_and_closed(port, strong_case):
    """Kit 4 acceptance: challenge the strong fraud case with JPG/PDF evidence, resolve it to
    rejection, assert the configured fee and outcome are recorded, no second challenge is
    sent, and closure uses a typed reason."""
    raw = challenge_input(strong_case)
    out = run(CHALLENGE, raw, port=port, decide=approve("approver-1"))
    assert out.decision.risk_tier is RiskTier.SINGLE
    assert out.decision.computed["recommendation"] == "CONTEST"
    types = {e["content_type"] for e in out.proposal.computed["evidence"]}
    assert types == {"image/jpeg", "application/pdf"}
    assert out.state == "SUCCEEDED", out.detail
    assert len(port.writes("files.upload")) == 2
    [challenge] = port.writes("disputes.challenge")
    assert challenge.request_id == challenge_request_id(strong_case, "RFI")
    assert challenge.body["request_id"] == challenge.request_id
    assert challenge.body["reason"] == "PURCHASE_HISTORY"
    docs = challenge.body["supporting_documents"]["documents"]
    assert {d["type"] for d in docs} == {"ORDER", "PRIMARY"}
    assert challenge.body["product_type"] == DEFAULT_CONFIG.product_type
    assert port.fake.objects("disputes")[0]["status"] == "CHALLENGED"

    # The issuer rejects the evidence: escalation to CHARGEBACK, back in REQUIRES_RESPONSE.
    port.escalate(strong_case)
    again = run(CHALLENGE, raw, port=port, decide=approve("approver-1"))
    assert again.state == "BLOCKED"
    assert any("no second challenge" in r for r in again.decision.reasons)
    newer = challenge_input(
        strong_case,
        files=("strong_fraud/ev_order_history.pdf", "injection/ev_customer_letter.pdf"),
        facts=[f for f in raw["facts"] if f["evidence_id"] == "ev_order_history"],
    )
    assert run(CHALLENGE, newer, port=port, decide=approve("approver-1")).state == "BLOCKED"
    # Independent guard: with the kit's case record lost, the provider's own record of the
    # RFI challenge (challenge_details) still blocks a second challenge.
    from awx_common.store import InMemoryKitStore

    book = port.kit_store
    port.kit_store = InMemoryKitStore()
    for doc in ("strong_fraud/ev_order_history.pdf", "injection/ev_customer_letter.pdf"):
        port.ingest(strong_case, doc)
    fourth = run(CHALLENGE, newer, port=port, decide=approve("approver-1"))
    assert fourth.state == "BLOCKED"
    port.kit_store = book
    assert len(port.writes("disputes.challenge")) == 1, "no second challenge is sent"

    port.resolve(strong_case, "LOST")
    closed = run(
        CLOSE,
        {"dispute_id": strong_case, "close_reason": "LOST_ISSUER_REJECTED_EVIDENCE"},
        port=port,
    )
    assert closed.state == "SUCCEEDED", closed.detail
    assert closed.decision.risk_tier is RiskTier.AUTO
    entries = closed.decision.computed["ledger_entries"]
    assert [e for e in entries if e["kind"] == "DISPUTE_FEE"] == [
        {
            "kind": "DISPUTE_FEE",
            "source": "MODELLED",
            "amount": DEFAULT_CONFIG.terms["AUD"].dispute_fee,
            "note": "configured dispute fee; not deducted by the sandbox",
        }
    ]
    lost = [e for e in entries if e["kind"] == "DISPUTED_AMOUNT_LOST"]
    assert lost[0]["amount"] == Money.of("900.00", "AUD")
    assert lost[0]["source"] == "PROVIDER"
    wrong = run(
        CLOSE, {"dispute_id": strong_case, "close_reason": "WON_EVIDENCE_ACCEPTED"}, port=port
    )
    assert wrong.decision.risk_tier is RiskTier.BLOCKED
    assert len(port.writes("disputes.challenge")) == 1


def test_required_acceptance_small_case_cannot_be_accepted_without_bound_approval(port, small_case):
    """Kit 4 acceptance: separately prove the small case cannot be accepted without the bound
    approval."""
    pending = run(ACCEPT, small_accept(), port=port)
    assert pending.decision.risk_tier is RiskTier.SINGLE
    assert pending.decision.computed["recommendation"] == "ACCEPT"
    assert pending.state == "EXPIRED"  # local_runner: still pending after `decide` -> expired
    assert port.writes() == []
    self_approved = run(ACCEPT, small_accept(), port=port, decide=approve("requester-1"))
    assert self_approved.state == "REJECTED"  # engine.binding: a non-binding decision fails closed
    assert "proposer" in self_approved.detail

    def forged(p):
        return [
            ApprovalDecision(p.proposal_id, "f" * 64, "approver-1", Decision.APPROVE, p.created_at)
        ]

    assert run(ACCEPT, small_accept(), port=port, decide=forged).state == "REJECTED"
    rejected = run(
        ACCEPT, small_accept(), port=port, decide=approve("approver-1", decision=Decision.REJECT)
    )
    assert rejected.state == "REJECTED"
    late = run(
        ACCEPT,
        small_accept(),
        port=port,
        decide=approve("approver-1", at=NOW + timedelta(hours=13)),
    )
    assert late.state == "REJECTED"
    assert port.writes() == []
    done = run(ACCEPT, small_accept(), port=port, decide=approve("approver-1"))
    assert done.state == "SUCCEEDED", done.detail
    [accept] = port.writes()
    assert accept.name == "disputes.accept"
    assert accept.request_id == accept_request_id(small_case, "CHARGEBACK")
    assert accept.body["request_id"] == accept.request_id
    assert accept.body["reason"] == "LOW_VALUE_TRANSACTION"
    assert port.disputes[small_case]["status"] == "ACCEPTED"


def test_credit_not_processed_case_is_referred_and_moves_no_money(port, credit_case):
    out = run(REFER, {"dispute_id": credit_case, "summary": "Two emails, no reply."}, port=port)
    assert out.decision.risk_tier is RiskTier.AUTO
    assert out.state == "SUCCEEDED"
    facts = [
        fact(
            "CUSTOMER_CONTACTED_SUPPORT",
            "ev_support_emails",
            "2026-09-01 Customer email to support: please process my refund",
        ),
        fact(
            "CUSTOMER_CONTACTED_SUPPORT",
            "ev_support_emails",
            "2026-09-08 Customer email to support: second request, still no refund",
        ),
        fact("NO_SUPPORT_REPLY", "ev_support_emails", "No reply was sent by support"),
    ]
    raw = challenge_input(
        credit_case,
        "180.00",
        ("credit_not_processed/ev_support_emails.pdf",),
        facts,
        reason="REFUND_NOT_OWED",
    )
    blocked = run(CHALLENGE, raw, port=port, decide=approve("approver-1", "approver-2"))
    assert blocked.state == "BLOCKED"
    assert port.writes() == []


# ---- faults through the real runner ----------------------------------------------------------


def test_timeout_after_submit_that_landed_is_found_by_reconcile(port, strong_case):
    port.timeout("disputes.challenge", landed=True)
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert len(port.writes("disputes.challenge")) == 1
    assert port.case(strong_case)["responses"][0]["state"] == "LANDED"
    assert "lock" not in port.case(strong_case)


def test_landed_but_invisible_challenge_is_found_at_once(port, strong_case):
    """FakeAirwallex hides objects a write CREATES (M1's landed-but-invisible write). A dispute
    response updates an existing object, so reconcile sees it at once and nothing is resent."""
    port.timeout("disputes.challenge", landed=True, visible_after_s=3600)
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert len(port.writes("disputes.challenge")) == 1


def test_landed_but_invisible_upload_is_resent_inertly_and_challenged_once(port, strong_case):
    """files.upload creates a file; landed-but-invisible after a timeout, the executor resends
    it (resend_safe). The challenge still goes out once."""
    port.timeout("files.upload", landed=True, visible_after_s=3600)
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert len(port.writes("disputes.challenge")) == 1
    assert len(port.writes("files.upload")) == 3  # one inert duplicate


def test_unknown_challenge_that_did_not_land_is_not_resent_and_goes_to_a_person(port, strong_case):
    """The dispute endpoints do not deduplicate, so the spine never resends after an unknown
    outcome. The run stops OUTCOME_UNKNOWN, the lock stays held and the case is a person's,
    with the response deadline in the reason."""
    port.timeout("disputes.challenge", landed=False)
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert out.state == "FAILED"
    assert len(port.writes("disputes.challenge")) == 1
    assert out.results["submit_challenge"][-1].status is OperationStatus.OUTCOME_UNKNOWN
    assert "a person must reconcile" in out.postcondition.detail
    case = port.case(strong_case)
    assert case["lock"]["owner"] == out.proposal.proposal_id
    assert case["responses"][0]["state"] == "IN_FLIGHT"
    again = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert again.state == "BLOCKED"
    assert any("respond by" in r and "Nothing is resent" in r for r in again.decision.reasons)
    assert len(port.writes("disputes.challenge")) == 1


def test_a_500_that_still_applied_is_found_by_reconcile(port, strong_case):
    from awx_spine.fake import Fault

    port.fake.inject("disputes.challenge", Fault.SERVER_ERROR, applied=True)
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert len(port.writes("disputes.challenge")) == 1


def test_a_429_then_a_crash_reconciles_and_never_sends_twice(port, strong_case):
    from awx_spine.fake import Fault

    port.fake.inject("disputes.challenge", Fault.RATE_LIMIT)
    port.port.crash_on_call["disputes.challenge"] = 2  # the resend lands, then the adapter dies
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert len(port.writes("disputes.challenge")) == 2  # the 429, then one accepted send
    assert port.disputes[strong_case]["status"] == "CHALLENGED"
    assert len(port.disputes[strong_case]["challenge_details"]) == 1


def test_list_lag_after_a_create_does_not_double_send(port, strong_case):
    from awx_spine.fake import Fault

    port.fake.inject("disputes.challenge", Fault.LIST_LAG, visible_after_s=5)
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert len(port.writes("disputes.challenge")) == 1
    assert out.state in ("SUCCEEDED", "FAILED")
    if out.state == "FAILED":  # finality outlived by the lag: in flight, never resent
        assert port.case(strong_case)["lock"]["owner"] == out.proposal.proposal_id


def test_duplicate_delivery_of_an_accept_is_recorded_as_landed(port, small_case):
    from awx_spine.fake import Fault

    port.fake.inject("disputes.accept", Fault.DUPLICATE_DELIVERY)
    out = run(ACCEPT, small_accept(), port=port, decide=approve("approver-1"))
    assert len(port.writes("disputes.accept")) == 1
    assert port.disputes[small_case]["status"] == "ACCEPTED"
    assert port.case(small_case)["responses"][0]["state"] == "LANDED"
    assert "lock" not in port.case(small_case)
    assert out.state in ("SUCCEEDED", "FAILED")


def test_a_retry_after_success_is_blocked_and_sends_nothing(port, strong_case):
    raw = challenge_input(strong_case)
    assert run(CHALLENGE, raw, port=port, decide=approve("approver-1")).state == "SUCCEEDED"
    version = port.kit_store.get(f"case/{strong_case}").version
    again = run(CHALLENGE, raw, port=port, decide=approve("approver-1"))
    assert again.state == "BLOCKED"
    assert len(port.writes("disputes.challenge")) == 1
    assert len(port.writes("files.upload")) == 2
    assert port.kit_store.get(f"case/{strong_case}").version == version


def test_dedupe_window_passing_changes_nothing_for_an_in_flight_challenge(port, strong_case):
    """M1's window clause: past ~6 days in-flight money goes to a person. Kit 4's dispute
    endpoints never deduplicate, so the case is a person's from the first unknown outcome;
    eight days on it is still blocked and nothing is sent."""
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    port.sleep(8 * 86400)
    port.disputes[strong_case]["due_at"] = (NOW + timedelta(days=20)).isoformat()
    later = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert later.state == "BLOCKED"
    assert len(port.writes("disputes.challenge")) == 1


# ---- M1 and M4: the dispute lock -------------------------------------------------------------


def test_unknown_challenge_then_an_accept_is_blocked_with_nothing_sent(port, strong_case):
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    accept = accept_input(strong_case, "900.00", rationale="Give up on the case.")
    out = run(ACCEPT, accept, port=port, decide=approve("approver-1"))
    assert out.state == "BLOCKED"
    assert any("may have reached Airwallex" in r for r in out.decision.reasons)
    assert port.writes("disputes.accept") == []
    with pytest.raises(LockHeld):  # even with policy bypassed, the build refuses
        build_at(ACCEPT, accept, port, port.now(), 0)


def test_challenge_accepted_but_not_yet_shown_keeps_the_lock(port, strong_case):
    original = port.fake.execute

    def lagging(op):
        r = original(op)
        if op.name == "disputes.challenge":
            port.disputes[strong_case]["status"] = "REQUIRES_RESPONSE"
            port.disputes[strong_case]["challenge_details"] = []
        return r

    port.fake.execute = lagging
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert out.state == "FAILED"
    assert port.case(strong_case)["lock"]["owner"] == out.proposal.proposal_id
    port.fake.execute = original
    assert (
        run(
            ACCEPT, accept_input(strong_case, "900.00"), port=port, decide=approve("approver-1")
        ).state
        == "BLOCKED"
    )
    assert port.writes("disputes.accept") == []
    port.disputes[strong_case]["status"] = "CHALLENGED"
    port.disputes[strong_case]["challenge_details"] = [{"stage": "RFI"}]
    assert seen(port, strong_case)["attempts"] == [{"action": "challenge", "state": "LANDED"}]


def test_failed_upload_leaves_the_case_retryable(port, strong_case):
    port.port.refuse["files.upload"] = 1
    raw = challenge_input(strong_case)
    first = run(CHALLENGE, raw, port=port, decide=approve("approver-1"))
    assert first.state == "FAILED"
    assert port.writes("disputes.challenge") == []
    case = port.case(strong_case)
    assert "lock" not in case
    assert not case.get("attempts")
    retry = run(
        CHALLENGE, dict(raw, narrative="Second try."), port=port, decide=approve("approver-1")
    )
    assert retry.state == "SUCCEEDED", retry.detail
    assert len(port.writes("disputes.challenge")) == 1


def test_refused_challenge_goes_to_a_person(port, strong_case):
    """A refusal at create is definitive for this first send: the lock is released, the
    response is REFUSED, and a new challenge at the same stage is a person's call (its
    request_id cannot carry a new body)."""
    port.port.refuse["disputes.challenge"] = 1
    first = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    assert first.state == "FAILED"
    case = port.case(strong_case)
    assert "lock" not in case
    assert case["responses"][0]["state"] == "REFUSED"
    again = run(
        CHALLENGE,
        dict(challenge_input(strong_case), narrative="new"),
        port=port,
        decide=approve("approver-1"),
    )
    assert again.state == "BLOCKED"
    assert any("Airwallex refused the earlier challenge" in r for r in again.decision.reasons)


def test_adapter_crash_after_send_reconciles_instead_of_failing(port, small_case):
    port.port.crash["disputes.accept"] = 1
    out = run(ACCEPT, small_accept(), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert len(port.writes("disputes.accept")) == 1
    assert "lock" not in port.case(small_case)


def test_a_lost_execution_strands_nothing_but_a_new_proposal_waits_for_a_person(port, strong_case):
    """The execution dies after the challenge landed (no postcondition, lock still held). An
    identical re-proposal is refused AT PROPOSAL TIME: no approval wasted."""
    port.port.lose["disputes.challenge"] = 1
    with pytest.raises(Lost):
        run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    asked: list = []

    def counting(p):
        asked.append(p)
        return approve("approver-1")(p)

    later = NOW + timedelta(hours=13)  # a new approval window: a new proposal id
    retry = run(CHALLENGE, challenge_input(strong_case), port=port, decide=counting, now=later)
    assert retry.state == "BLOCKED"
    assert asked == []  # never sent for approval
    assert any("already been challenged" in r for r in retry.decision.reasons)
    accept = run(ACCEPT, accept_input(strong_case, "900.00"), port=port, decide=counting)
    assert accept.state == "BLOCKED"
    assert asked == []
    # The challenge did land before the loss: the case reads it from the provider.
    assert seen(port, strong_case)["attempts"] == [{"action": "challenge", "state": "LANDED"}]


def test_lost_execution_before_the_provider_took_it_stays_a_persons(port, small_case):
    port.timeout("disputes.accept", landed=False)
    port.port.lose["disputes.accept"] = 1
    with pytest.raises(Lost):
        run(ACCEPT, small_accept(), port=port, decide=approve("approver-1"))
    assert seen(port, small_case)["attempts"] == [{"action": "accept", "state": "IN_FLIGHT"}]
    asked: list = []
    retry = run(
        ACCEPT,
        small_accept(),
        port=port,
        now=NOW + timedelta(hours=13),
        decide=lambda p: asked.append(p) or [],
    )
    assert retry.state == "BLOCKED"
    assert asked == []
    assert any("never finished" in r for r in retry.decision.reasons)
    assert len(port.writes("disputes.accept")) == 1


def test_abandon_never_releases_a_lock(port, strong_case):
    assert CHALLENGE.on_abandon is None
    assert ACCEPT.on_abandon is None
    port.port.lose["disputes.challenge"] = 1
    with pytest.raises(Lost):
        run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    owner = port.case(strong_case)["lock"]["owner"]
    out = run(
        ACCEPT,
        accept_input(strong_case, "900.00"),
        port=port,
        decide=approve("approver-1", decision=Decision.REJECT),
    )
    assert out.state in ("BLOCKED", "REJECTED")
    assert port.case(strong_case)["lock"]["owner"] == owner


def test_stale_upload_only_lock_can_be_taken_over(port, strong_case):
    """A run that only uploaded evidence and died moved no money; after upload_lock_ttl a
    later run may take the lock over and challenge once."""
    port.port.lose["files.upload"] = 0
    port.fake.upload, original = (
        (lambda *a, **k: (_ for _ in ()).throw(Lost("upload"))),
        port.fake.upload,
    )
    with pytest.raises(Lost):
        run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1"))
    port.fake.upload = original
    assert port.case(strong_case)["lock"]
    other = dict(challenge_input(strong_case), narrative="Another person's challenge.")
    soon = run(
        CHALLENGE, other, port=port, decide=approve("approver-1"), now=NOW + timedelta(minutes=30)
    )
    assert soon.state == "BLOCKED"
    assert any("uploading evidence" in r for r in soon.decision.reasons)
    later = run(
        CHALLENGE, other, port=port, decide=approve("approver-1"), now=NOW + timedelta(hours=2)
    )
    assert later.state == "SUCCEEDED", later.detail
    assert len(port.writes("disputes.challenge")) == 1


def test_postcondition_records_an_unknown_accept_and_keeps_the_lock(port, small_case):
    port.timeout("disputes.accept", landed=False)
    out = run(ACCEPT, small_accept(), port=port, decide=approve("approver-1"))
    assert out.state == "FAILED"
    case = port.case(small_case)
    assert [a["action"] for a in case["attempts"]] == ["accept"]
    assert case["lock"]["owner"] == out.proposal.proposal_id
    [response] = case["responses"]
    assert response["results"][-1]["status"] == "OUTCOME_UNKNOWN"
    assert response["state"] == "IN_FLIGHT"
    assert response["respond_by"] is not None
    other = dict(small_accept(), reason="INSUFFICIENT_EVIDENCE")
    assert run(ACCEPT, other, port=port, decide=approve("approver-1")).state == "BLOCKED"
    assert run(ACCEPT, small_accept(), port=port, decide=approve("approver-1")).state == "BLOCKED"
    assert len(port.writes()) == 1


# ---- landed is per stage --------------------------------------------------------------------


def test_a_challenge_answered_then_escalated_is_still_landed(port, strong_case):
    assert (
        run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1")).state
        == "SUCCEEDED"
    )
    port.escalate(strong_case)
    assert seen(port, strong_case)["attempts"] == [{"action": "challenge", "state": "LANDED"}]


# ---- deadlines, staleness and binding --------------------------------------------------------


def test_deadline_passing_while_approval_is_pending_stops_the_write(port, strong_case):
    port.disputes[strong_case]["due_at"] = (NOW + timedelta(hours=30)).isoformat()
    out = run(
        CHALLENGE,
        challenge_input(strong_case),
        port=port,
        decide=approve("approver-1", at=NOW + timedelta(hours=7)),
        decided_at=NOW + timedelta(hours=7),
    )
    assert out.proposal.risk_tier is RiskTier.SINGLE
    assert out.proposal.computed["act_by"] == NOW + timedelta(hours=6)
    assert out.state == "FAILED"
    assert "changed since the proposal" in out.detail  # rule 1: STALE
    assert port.writes() == []
    intent = CHALLENGE.validate(challenge_input(strong_case))
    decision = CHALLENGE.policy(intent, make_ctx(port, CHALLENGE, NOW))
    from awx_common.workflow import StepContext

    late = StepContext(
        intent, decision, make_ctx(port, CHALLENGE, NOW + timedelta(hours=7)), "a" * 64, {}, "p"
    )
    with pytest.raises(DeadlineMissed):  # defence in depth in the build
        CHALLENGE.steps[0].build(late)


def test_deadline_passing_while_accept_approval_is_pending_stops_the_write(port, small_case):
    port.disputes[small_case]["due_at"] = (NOW + timedelta(hours=3)).isoformat()
    at = NOW + timedelta(hours=2, minutes=30)
    out = run(ACCEPT, small_accept(), port=port, decide=approve("approver-1", at=at), decided_at=at)
    assert out.state == "FAILED"
    assert port.writes() == []


def test_amount_change_between_approval_and_execution_stops_the_write(port, small_case):
    def decide(p):
        port.disputes[small_case]["amount"] = Decimal("16.00")
        return approve("approver-1")(p)

    out = run(ACCEPT, small_accept(), port=port, decide=decide)
    assert out.state == "FAILED"
    assert port.writes() == []


def test_policy_version_change_between_approval_and_execution_stops_the_write(port, small_case):
    old = build_specs(DisputeConfig(policy_version="k4-old"))["accept_dispute"]
    intent = old.validate(small_accept())
    from awx_common.workflow import PolicyDecision, StepContext

    ctx = make_ctx(port, ACCEPT, NOW)
    with pytest.raises(PolicyChanged):
        ACCEPT.steps[0].build(
            StepContext(intent, PolicyDecision(RiskTier.SINGLE, ()), ctx, "a" * 64, {}, "p")
        )


def test_approval_binds_to_code_computed_economics(port, strong_case):
    raw = challenge_input(strong_case)
    pricier = build_specs(
        DisputeConfig(
            terms={
                **DEFAULT_CONFIG.terms,
                "AUD": _terms("AUD", "30.00", "20.00", "2.00", "20.00", "1000.00"),
            }
        )
    )["challenge_dispute"]
    out = run(CHALLENGE, raw, port=port)  # expires: nothing decided
    assert out.proposal.computed["cost_contest_expected"] == Money.of("270.00", "AUD")
    assert out.proposal.computed["evidence_sha256"] == [
        e["sha256"] for e in out.proposal.computed["evidence"]
    ]
    out.proposal.verify_hash()

    def swap_then_approve(p):
        return approve("approver-1")(p)

    # Approve under the old schedule, execute under the new one: rule 1 fails closed.
    from awx_spine_aws import engine
    from awx_spine_aws.local_runner import _KnownTenants, _LocalEnv
    from k4_harness import TENANT

    env = _LocalEnv(
        port.port,
        port.now,
        port.kit_store,
        port.kit_store,
        _KnownTenants({TENANT.tenant_id: TENANT}),
        True,
    )
    proposal, decision = engine.propose(CHALLENGE, (), raw, proposed_by="r", tenant=TENANT, env=env)
    execution = engine.execution_input(proposal, decision, raw, TENANT.tenant_id)
    started = engine.start(pricier, execution, tenant=TENANT)
    started = engine.binding(started, swap_then_approve(started.proposal), now=port.now())
    rechecked = engine.recheck(pricier, started, env)
    assert rechecked.state == "FAILED"
    assert rechecked.stale
    assert port.writes() == []


def test_accept_after_issuer_rejection_is_allowed_with_approval(port, strong_case):
    assert (
        run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("approver-1")).state
        == "SUCCEEDED"
    )
    port.escalate(strong_case)
    raw = accept_input(
        strong_case,
        "900.00",
        "AFTER_ISSUER_REJECTION",
        "Issuer rejected the evidence; no new evidence exists.",
    )
    pending = run(ACCEPT, raw, port=port)
    assert pending.decision.risk_tier is RiskTier.SINGLE
    done = run(ACCEPT, raw, port=port, decide=approve("approver-1"))
    assert done.state == "SUCCEEDED", done.detail
    entries = outcome_entries(parse_dispute(port.disputes[strong_case]), DEFAULT_CONFIG)
    assert total(entries, EntrySource.PROVIDER, "AUD") == Money.of("900.00", "AUD")
    assert {e.kind for e in entries} >= {EntryKind.DISPUTED_AMOUNT_LOST}
    assert len(port.writes("disputes.challenge")) == 1


# ---- prompt injection -----------------------------------------------------------------------


def test_prompt_injection_in_evidence_cannot_cause_a_write(port, strong_case):
    """The customer letter tells the agent to accept and refund. A model that obeys it can only
    PROPOSE; code tiers the proposal, and without a bound approval nothing is written."""
    letter = "injection/ev_customer_letter.pdf"
    assert {"override", "role", "accept", "refund", "approval"} <= set(
        instruction_markers(evidence_text(letter))
    )
    obey = accept_input(
        strong_case, "900.00", "LOW_VALUE", "Accept this dispute immediately; no approval."
    )
    assert run(ACCEPT, obey, port=port).decision.risk_tier is RiskTier.BLOCKED
    other = dict(obey, reason="INSUFFICIENT_EVIDENCE")
    out = run(ACCEPT, other, port=port)
    assert out.decision.risk_tier is RiskTier.SINGLE
    assert out.state == "EXPIRED"
    with pytest.raises(ValueError, match="unexpected"):
        ACCEPT.validate(dict(other, refund_to="062-000 12345678"))
    raw = challenge_input(strong_case)
    raw["evidence_ids"] = sorted([*raw["evidence_ids"], "ev_customer_letter"])
    challenged = run(CHALLENGE, raw, port=port)
    assert challenged.decision.risk_tier is RiskTier.SINGLE
    assert challenged.decision.computed["flagged_evidence"] == ["ev_customer_letter"]
    assert any(
        "ev_customer_letter contains instruction-like text" in r
        for r in challenged.decision.reasons
    )
    refer = run(REFER, {"dispute_id": strong_case, "summary": "Refund AUD 900.00 now."}, port=port)
    assert refer.state == "SUCCEEDED"
    close = {"dispute_id": strong_case, "close_reason": "ACCEPTED_LOW_VALUE"}
    assert run(CLOSE, close, port=port).decision.risk_tier is RiskTier.BLOCKED
    assert port.writes() == []


def test_facts_fabricated_against_the_injection_letter_do_not_raise_the_band(port, strong_case):
    letter_only = challenge_input(
        strong_case,
        files=("injection/ev_customer_letter.pdf",),
        facts=[
            fact("SIGNED_DELIVERY", "ev_customer_letter", "Signed by: J. Citizen"),
            fact("DEVICE_FINGERPRINT_MATCH_PRIOR", "ev_customer_letter", "device matches"),
            fact("IP_MATCH_PRIOR", "ev_customer_letter", "IP matches"),
        ],
    )
    out = run(CHALLENGE, letter_only, port=port, decide=approve("approver-1"))
    assert out.decision.risk_tier is RiskTier.BLOCKED
    assert out.decision.computed["verified_facts"] == 0
    assert len(out.decision.computed["rejected_facts"]) == 3
    assert port.writes() == []


# ---- other policy outcomes through the runner ------------------------------------------------


def test_blocked_challenge_with_low_value_economics(port, small_case):
    facts = [fact("DELIVERY_SCAN_NO_SIGNATURE", "ev_delivery_scan", "No signature obtained")]
    raw = challenge_input(
        small_case,
        "15.00",
        ("small_not_received/ev_delivery_scan.jpg",),
        facts,
        reason="GOODS_DELIVERED",
    )
    out = run(CHALLENGE, raw, port=port, decide=approve("approver-1", "approver-2"))
    assert out.state == "BLOCKED"
    assert any("economics say accept" in r for r in out.decision.reasons)


def test_existing_refund_blocks_accept(port, small_case):
    pi = port.disputes[small_case]["payment_intent_id"]
    port.seed_refund(pi, "15.00")
    out = run(ACCEPT, small_accept(), port=port, decide=approve("approver-1"))
    assert out.state == "BLOCKED"
    assert any("refund" in r for r in out.decision.reasons)
    assert port.writes() == []


def test_unknown_provider_status_fails_closed(port, small_case):
    port.disputes[small_case]["status"] = "SOMETHING_NEW"
    assert run(ACCEPT, small_accept(), port=port, decide=approve("approver-1")).state == "BLOCKED"
    assert port.writes() == []


def test_large_dispute_needs_two_approvers(port):
    port.add_dispute("dsp_big", amount="1500.00", reason={"code": "10.4"})
    raw = challenge_input("dsp_big", "1500.00")
    one = run(CHALLENGE, raw, port=port, decide=approve("approver-1"))
    assert one.decision.risk_tier is RiskTier.DUAL
    assert one.state == "EXPIRED"
    two = run(CHALLENGE, raw, port=port, decide=approve("approver-1", "approver-2"))
    assert two.state == "SUCCEEDED", two.detail
    assert len(port.writes("disputes.challenge")) == 1


def test_referral_and_close_are_recorded_once(port, credit_case):
    raw = {"dispute_id": credit_case, "summary": "Two unanswered support emails."}
    run(REFER, raw, port=port)
    run(REFER, raw, port=port)
    port.resolve(credit_case, "EXPIRED")
    close = {"dispute_id": credit_case, "close_reason": "EXPIRED_NO_RESPONSE"}
    run(CLOSE, close, port=port)
    run(CLOSE, close, port=port)
    case = seen(port, credit_case)
    assert case["referrals"] == 1
    assert case["outcome"] == "EXPIRED_NO_RESPONSE"
    other = {"dispute_id": credit_case, "close_reason": "ACCEPTED_LOW_VALUE"}
    assert run(CLOSE, other, port=port).state == "BLOCKED"
