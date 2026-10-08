"""Clearing a stuck response: a clear never acts while the original response can still be sent
or may not yet be visible, needs one approval and an empty read-back, and keeps watching the
attempt for a late landing. Through the real runner, with one RunRegistry."""

from __future__ import annotations

from datetime import timedelta

from awx_common import RiskTier
from awx_common.workflow import RUN_RUNNING, PolicyContext, StepContext
from awx_k4_dispute import READS, SPECS, TOOLS
from awx_k4_dispute.clearing import ATTEMPT_LEASE_S, CLEAR_BOUND
from awx_k4_dispute.provider import challenge_request_id
from k4_harness import (
    NOW,
    TENANT,
    accept_input,
    approve,
    challenge_input,
    make_ctx,
    past_clear_bound,
    run,
)

ACCEPT = SPECS["accept_dispute"]
CHALLENGE = SPECS["challenge_dispute"]
REFER = SPECS["refer_dispute"]
CLOSE = SPECS["close_dispute"]
CLEAR = SPECS["clear_dispute_response"]


def case_refs(port, dispute_id):
    return READS["read_dispute"]({"dispute_id": dispute_id}, make_ctx(port, ACCEPT))["case"]


def _clear_raw(dispute_id):
    return {
        "dispute_id": dispute_id,
        "request_ref": challenge_request_id(dispute_id, "RFI"),
        "note": "nothing on the dashboard",
    }


def test_the_bound_covers_the_lease_the_finality_deadline_and_a_margin():
    from awx_spine.executor import DEFAULT_ATTEMPT_LEASE_S

    assert ATTEMPT_LEASE_S == DEFAULT_ATTEMPT_LEASE_S
    assert timedelta(seconds=ATTEMPT_LEASE_S + 120 + 60) < CLEAR_BOUND


def test_a_clear_while_the_original_send_is_paused_is_blocked_and_the_lock_held(port, strong_case):
    """Probe P1: the original challenge run waits at its disputes.challenge send; a clear is
    proposed then, even with the clock far past the bound. The owner run reads RUNNING."""
    seen = {}
    original = port.port.execute

    def paused(op):
        if op.name == "disputes.challenge" and "clear" not in seen:
            port.sleep(CLEAR_BOUND.total_seconds() * 3)
            seen["clear"] = run(CLEAR, _clear_raw(strong_case), port=port, decide=approve("a-1"))
            seen["lock"] = dict(port.case(strong_case).get("lock") or {})
        return original(op)

    port.port.execute = paused
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert out.state == "SUCCEEDED", out.detail
    clear = seen["clear"]
    assert clear.state == "BLOCKED"
    assert any("RUNNING" in r for r in clear.decision.reasons)
    assert seen["lock"]["owner"] == out.proposal.proposal_id
    assert len(port.writes("disputes.challenge")) == 1
    assert not any(a.get("cleared") for a in port.case(strong_case)["attempts"])


def test_an_early_clear_then_a_challenge_and_an_accept_are_all_blocked(port, strong_case):
    """The owner run has finished (its send unknown), but the attempt is younger than the
    bound: the clear is BLOCKED, and so are a new challenge and an accept, zero new writes."""
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    before = len(port.writes())
    clear = run(CLEAR, _clear_raw(strong_case), port=port, decide=approve("a-1"))
    assert clear.state == "BLOCKED"
    assert any("a clear waits" in r for r in clear.decision.reasons)
    assert port.case(strong_case)["lock"]
    challenge = run(
        CHALLENGE,
        dict(challenge_input(strong_case), narrative="again"),
        port=port,
        decide=approve("a-1"),
    )
    accept = run(ACCEPT, accept_input(strong_case, "900.00"), port=port, decide=approve("a-1"))
    assert challenge.state == "BLOCKED"
    assert accept.state == "BLOCKED"
    assert len(port.writes()) == before


def test_a_clear_after_the_bound_with_the_owner_terminal_succeeds(port, strong_case):
    port.timeout("disputes.challenge", landed=False)
    first = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    status = port.runs.status(
        "k4", "challenge_dispute", first.proposal.proposal_id, TENANT.tenant_id
    )
    assert status == "SUCCEEDED"
    past_clear_bound(port)
    clear = run(CLEAR, _clear_raw(strong_case), port=port, decide=approve("a-1"))
    assert clear.state == "SUCCEEDED", clear.detail
    assert "lock" not in port.case(strong_case)
    (attempt,) = port.case(strong_case)["attempts"]
    assert attempt["cleared"]["by"] == clear.proposal.proposal_id


def test_after_a_clear_a_different_action_waits_out_the_bound(port, strong_case):
    """Optional item: the irreversible cross-action pair (challenge cleared, then accept)."""
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    past_clear_bound(port)
    assert run(CLEAR, _clear_raw(strong_case), port=port, decide=approve("a-1")).state == (
        "SUCCEEDED"
    )
    soon = run(ACCEPT, accept_input(strong_case, "900.00"), port=port, decide=approve("a-1"))
    assert soon.state == "BLOCKED"
    assert any("cleared moments ago" in r for r in soon.decision.reasons)
    past_clear_bound(port)
    later = run(ACCEPT, accept_input(strong_case, "900.00"), port=port, decide=approve("a-1"))
    assert later.state == "SUCCEEDED", later.detail
    assert port.writes("disputes.accept")


def _stuck_target(port, dispute_id):
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(dispute_id), port=port, decide=approve("a-1"))
    past_clear_bound(port)
    return CLEAR.validate(_clear_raw(dispute_id))


def _ctx(port, *, proposal_id, run_status):
    base = make_ctx(port, CLEAR)
    return PolicyContext(
        tenant=TENANT,
        now=base.now,
        read=base.read,
        store=base.store,
        proposal_id=proposal_id,
        run_status=run_status,
    )


def test_run_status_none_error_and_missing_callable_block(port, strong_case):
    intent = _stuck_target(port, strong_case)

    def boom(pid, action):
        raise RuntimeError("throttled")

    for status in (lambda p, a: None, lambda p, a: RUN_RUNNING, boom):
        d = CLEAR.policy(intent, _ctx(port, proposal_id="p", run_status=status))
        assert d.risk_tier is RiskTier.BLOCKED, d.reasons
    d = CLEAR.policy(intent, _ctx(port, proposal_id="p", run_status=None))
    assert d.risk_tier is RiskTier.BLOCKED
    # At the agent's propose call (no proposal yet, no callable): decided at recheck.
    d = CLEAR.policy(intent, _ctx(port, proposal_id=None, run_status=None))
    assert d.risk_tier is RiskTier.SINGLE
    assert any("checked when the approved run starts" in r for r in d.reasons)


def test_the_clear_postcondition_reapplies_the_rule(port, strong_case):
    """An approval granted on a terminal reading cannot apply if the postcondition now reads
    the owner RUNNING (or cannot read it): nothing is cleared and the lock stays."""
    intent = _stuck_target(port, strong_case)
    decision = CLEAR.policy(intent, make_ctx(port, CLEAR))
    assert decision.risk_tier is RiskTier.SINGLE
    for status in (lambda p, a: RUN_RUNNING, None):
        ctx = _ctx(port, proposal_id="pid-clear", run_status=status)
        post = CLEAR.postcondition(StepContext(intent, decision, ctx, "a" * 64, {}, "pid-clear"))
        assert not post.ok
        assert "not cleared" in post.detail
    assert port.case(strong_case)["lock"]
    assert not any(a.get("cleared") for a in port.case(strong_case)["attempts"])


def test_an_attempt_without_a_recorded_time_is_never_cleared(port, strong_case):
    intent = _stuck_target(port, strong_case)
    item = port.kit_store.get(f"case/{strong_case}")
    value = dict(item.value)
    value["attempts"] = [
        {k: v for k, v in a.items() if k != "recorded_at"} for a in value["attempts"]
    ]
    port.kit_store.put(f"case/{strong_case}", value, expected_version=item.version)
    d = CLEAR.policy(intent, make_ctx(port, CLEAR))
    assert d.risk_tier is RiskTier.BLOCKED
    assert any("no recorded time" in r for r in d.reasons)


def _cleared_own_attempt(port, dispute_id, action):
    """Record an attempt as run `pid-owner`, then mark it cleared, as a clear would."""
    from awx_k4_dispute.cases import clear_attempt, record_attempt, take_lock
    from awx_k4_dispute.provider import accept_request_id, parse_dispute

    spec = CHALLENGE if action == "challenge" else ACCEPT
    raw = (
        challenge_input(dispute_id) if action == "challenge" else accept_input(dispute_id, "900.00")
    )
    intent = spec.validate(raw)
    decision = spec.policy(intent, make_ctx(port, spec))
    view = parse_dispute(port.disputes[dispute_id])
    rid = (challenge_request_id if action == "challenge" else accept_request_id)(dispute_id, "RFI")
    take_lock(port.kit_store, dispute_id, "pid-owner", action, view, port.now(), CLEAR_BOUND)
    record_attempt(
        port.kit_store, dispute_id, "pid-owner", action, intent, decision, rid, port.now()
    )
    clear_attempt(
        port.kit_store,
        dispute_id,
        "pid-owner",
        rid,
        "pid-clear",
        at=port.now(),
        owner_terminal=True,
    )
    return spec, intent, decision


def test_a_redriven_owner_accept_never_sends_after_its_attempt_was_cleared(port, strong_case):
    import pytest
    from awx_k4_dispute.workflow import StaleDispute

    spec, intent, decision = _cleared_own_attempt(port, strong_case, "accept")
    ctx = StepContext(intent, decision, make_ctx(port, spec), "a" * 64, {}, "pid-owner")
    with pytest.raises(StaleDispute, match="cleared"):
        spec.steps[0].build(ctx)
    assert port.writes() == []


def test_a_redriven_owner_challenge_never_sends_after_its_attempt_was_cleared(port, strong_case):
    import pytest
    from awx_k4_dispute.workflow import StaleDispute

    spec, intent, decision = _cleared_own_attempt(port, strong_case, "challenge")
    ctx = StepContext(intent, decision, make_ctx(port, spec), "a" * 64, {}, "pid-owner")
    with pytest.raises(StaleDispute, match="cleared"):
        spec.steps[1].build(ctx)
    assert port.writes() == []


# ---- the clear tool: a person clears a stuck IN_FLIGHT case ---------------------------------


def _stuck(port, dispute_id):
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(dispute_id), port=port, decide=approve("a-1"))
    past_clear_bound(port)
    return case_refs(port, dispute_id)["attempt_refs"][0]["request_ref"]


def test_clear_needs_an_approval_and_an_empty_read_back(port, strong_case):
    ref = _stuck(port, strong_case)
    raw = {"dispute_id": strong_case, "request_ref": ref, "note": "nothing on the dashboard"}
    pending = run(CLEAR, raw, port=port)
    assert pending.decision.risk_tier is RiskTier.SINGLE
    assert pending.state == "EXPIRED"
    assert port.case(strong_case)["lock"]
    done = run(CLEAR, raw, port=port, decide=approve("a-1"))
    assert done.state == "SUCCEEDED", done.detail
    assert case_refs(port, strong_case)["attempts"] == [{"action": "challenge", "state": "CLEARED"}]
    # A new challenge goes out once, under the stage's NEXT key.
    again = run(
        CHALLENGE,
        challenge_input(strong_case),
        port=port,
        decide=approve("a-1"),
        now=NOW + timedelta(minutes=5),
    )
    assert again.state == "SUCCEEDED", again.detail
    first, second = port.writes("disputes.challenge")
    assert first.request_id == challenge_request_id(strong_case, "RFI")
    assert second.request_id == challenge_request_id(strong_case, "RFI", 2)


def test_clear_is_refused_when_the_response_shows(port, strong_case):
    ref = _stuck(port, strong_case)
    port.disputes[strong_case]["status"] = "CHALLENGED"
    port.disputes[strong_case]["challenge_details"] = [{"stage": "RFI"}]
    out = run(
        CLEAR,
        {"dispute_id": strong_case, "request_ref": ref, "note": "n"},
        port=port,
        decide=approve("a-1"),
    )
    assert out.state == "BLOCKED"


def test_clear_is_refused_on_an_undetermined_read(port, strong_case):
    ref = _stuck(port, strong_case)
    port.disputes[strong_case]["status"] = "SOMETHING_NEW"
    out = run(
        CLEAR,
        {"dispute_id": strong_case, "request_ref": ref, "note": "n"},
        port=port,
        decide=approve("a-1"),
    )
    assert out.state == "BLOCKED"
    assert any("not empty" in r for r in out.decision.reasons)
    assert port.case(strong_case)["lock"]


def test_a_late_landing_after_a_clear_still_blocks_a_second_challenge(port, strong_case):
    ref = _stuck(port, strong_case)
    run(
        CLEAR,
        {"dispute_id": strong_case, "request_ref": ref, "note": "n"},
        port=port,
        decide=approve("a-1"),
    )
    # The original challenge lands late: the kit keeps watching it.
    port.disputes[strong_case]["status"] = "CHALLENGED"
    port.disputes[strong_case]["challenge_details"] = [{"stage": "RFI"}]
    assert case_refs(port, strong_case)["attempts"] == [{"action": "challenge", "state": "LANDED"}]
    port.disputes[strong_case]["status"] = "REQUIRES_RESPONSE"  # e.g. a lagging read
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert out.state == "BLOCKED"
    assert len(port.writes("disputes.challenge")) == 1


def test_clear_postcondition_records_a_landing_between_approval_and_run(port, strong_case):
    ref = _stuck(port, strong_case)
    raw = {"dispute_id": strong_case, "request_ref": ref, "note": "n"}

    def decide(p):
        port.disputes[strong_case]["status"] = "CHALLENGED"
        port.disputes[strong_case]["challenge_details"] = [{"stage": "RFI"}]
        return approve("a-1")(p)

    out = run(CLEAR, raw, port=port, decide=decide)
    assert out.state in ("FAILED", "POSTCONDITION_FAILED")
    assert "cleared" not in str(port.case(strong_case)["attempts"][0])


def test_clear_tool_is_registered_and_never_auto():
    assert {t.name for t in TOOLS} >= {"propose_clear_dispute_response"}
    assert CLEAR.agent_min_tier is RiskTier.SINGLE
