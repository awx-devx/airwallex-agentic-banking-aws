"""Stage changes and lost results through the real runner: a sent response with no recorded
result stays in flight, a stage move fails the build closed, and refused or unknown responses
across an escalation."""

from __future__ import annotations

from awx_k4_dispute import SPECS
from awx_k4_dispute.provider import challenge_request_id
from awx_spine.fake import Fault
from k4_harness import accept_input, approve, challenge_input, make_ctx, run

ACCEPT = SPECS["accept_dispute"]
CHALLENGE = SPECS["challenge_dispute"]


def _lag_then_fail_finality(port, dispute_id, op_name):
    """After `op_name` reaches Airwallex: the provider does not show it yet, and the finality
    read raises once. The runner then drops the step's result, so the
    postcondition sees an empty prior although the operation was sent."""
    original = port.fake.execute

    def execute(op):
        result = original(op)
        if op.name == op_name:
            d = port.disputes[dispute_id]
            d["status"] = "REQUIRES_RESPONSE"
            d["accept_details"], d["challenge_details"] = [], []
            port.fake.inject("disputes.get", Fault.SERVER_ERROR)
        return result

    port.fake.execute = execute
    return original


def _case_attempts(port, dispute_id):
    out = READ({"dispute_id": dispute_id}, make_ctx(port, ACCEPT))
    return out["case"]["attempts"]


from awx_k4_dispute import READS  # noqa: E402

READ = READS["read_dispute"]


def test_accept_sent_then_finality_read_raises_keeps_the_lock_and_blocks_a_challenge(
    port, strong_case
):
    """An accept that was handed to the
    executor and has no recorded result is IN_FLIGHT. The lock stays held and the challenge
    proposed next is BLOCKED with nothing sent (accept is irreversible)."""
    original = _lag_then_fail_finality(port, strong_case, "disputes.accept")
    out = run(ACCEPT, accept_input(strong_case, "900.00"), port=port, decide=approve("a-1"))
    port.fake.execute = original
    assert out.state == "FAILED"
    assert len(port.writes("disputes.accept")) == 1
    case = port.case(strong_case)
    assert case["lock"]["owner"] == out.proposal.proposal_id
    assert case["responses"][0]["state"] == "IN_FLIGHT"
    assert _case_attempts(port, strong_case) == [{"action": "accept", "state": "IN_FLIGHT"}]
    challenge = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert challenge.state == "BLOCKED"
    again = run(
        ACCEPT,
        accept_input(strong_case, "900.00", "MERCHANT_ERROR"),
        port=port,
        decide=approve("a-1"),
    )
    assert again.state == "BLOCKED"
    assert port.writes("disputes.challenge") == []
    assert len(port.writes("disputes.accept")) == 1


def test_challenge_sent_then_finality_read_raises_stays_in_flight(port, strong_case):
    original = _lag_then_fail_finality(port, strong_case, "disputes.challenge")
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    port.fake.execute = original
    assert out.state == "FAILED"
    assert port.case(strong_case)["lock"]["owner"] == out.proposal.proposal_id
    assert port.case(strong_case)["responses"][0]["state"] == "IN_FLIGHT"
    assert (
        run(ACCEPT, accept_input(strong_case, "900.00"), port=port, decide=approve("a-1")).state
        == "BLOCKED"
    )
    assert port.writes("disputes.accept") == []


def test_stage_moving_during_upload_sends_no_challenge_and_releases_the_lock(port, strong_case):
    """Approved at RFI, the dispute moves to CHARGEBACK during the second upload.
    The challenge build fails closed (nothing sent) and the upload-only lock is released."""
    original = port.port.upload
    uploads = []

    def upload(data, **kw):
        uploads.append(1)
        result = original(data, **kw)
        if len(uploads) == 2:
            port.escalate(strong_case)
        return result

    port.port.upload = upload
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert out.state == "FAILED"
    assert "stage" in out.detail
    assert port.writes("disputes.challenge") == []
    case = port.case(strong_case)
    assert "lock" not in case
    assert not case.get("attempts")
    assert port.disputes[strong_case]["stage"] == "CHARGEBACK"


def test_accept_refuses_a_stage_other_than_the_approved_one(port, strong_case):
    from datetime import timedelta

    from awx_common.workflow import StepContext
    from k4_harness import NOW

    raw = accept_input(strong_case, "900.00")
    intent = ACCEPT.validate(raw)
    decision = ACCEPT.policy(intent, make_ctx(port, ACCEPT))
    assert decision.computed["stage"] == "RFI"
    port.escalate(strong_case)
    ctx = StepContext(
        intent, decision, make_ctx(port, ACCEPT, NOW + timedelta(minutes=1)), "a" * 64, {}, "pid"
    )
    import pytest
    from awx_k4_dispute.workflow import StaleDispute

    with pytest.raises(StaleDispute, match="stage"):
        ACCEPT.steps[0].build(ctx)
    assert port.writes() == []


def test_an_unknown_rfi_challenge_does_not_lapse_on_escalation(port, strong_case):
    """A stage change is not proof an unknown response never landed (the per-stage
    record shape is unconfirmed live). The attempt stays IN_FLIGHT, with a person."""
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    port.escalate(strong_case)
    assert _case_attempts(port, strong_case) == [{"action": "challenge", "state": "IN_FLIGHT"}]
    later = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert later.state == "BLOCKED"
    assert len(port.writes("disputes.challenge")) == 1


def test_a_refused_rfi_challenge_lapses_and_the_chargeback_reopens(port, strong_case):
    """A definitive refusal at RFI, then the RFI runs out
    into a chargeback: LAPSED, and the new stage is challenged once under its own key."""
    from datetime import timedelta

    from k4_harness import NOW

    port.port.refuse["disputes.challenge"] = 1
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    port.escalate(strong_case)
    assert _case_attempts(port, strong_case) == [{"action": "challenge", "state": "LAPSED"}]
    out = run(
        CHALLENGE,
        challenge_input(strong_case),
        port=port,
        decide=approve("a-1"),
        now=NOW + timedelta(minutes=5),
    )
    assert out.state == "SUCCEEDED", out.detail
    first, second = port.writes("disputes.challenge")
    assert first.request_id == challenge_request_id(strong_case, "RFI")
    assert second.request_id == challenge_request_id(strong_case, "CHARGEBACK")


def test_a_live_upload_lock_is_refused_at_proposal_time_even_with_earlier_attempts(
    port, strong_case
):
    """Another run's live upload-only lock blocks a proposal even when the case
    already has an earlier (lapsed) attempt, so no approval is wasted."""
    from awx_k4_dispute.cases import take_lock
    from awx_k4_dispute.provider import parse_dispute
    from k4_harness import NOW

    port.port.refuse["disputes.challenge"] = 1
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    port.escalate(strong_case)
    view = parse_dispute(port.disputes[strong_case])
    take_lock(port.kit_store, strong_case, "uploader", "challenge", view, port.now(), CHALLENGE_TTL)
    asked: list = []
    out = run(
        CHALLENGE, challenge_input(strong_case), port=port, decide=lambda p: asked.append(p) or []
    )
    assert out.state == "BLOCKED"
    assert asked == []
    assert any("uploading evidence" in r for r in out.decision.reasons)
    assert NOW


from datetime import timedelta as _td  # noqa: E402

CHALLENGE_TTL = _td(hours=1)
