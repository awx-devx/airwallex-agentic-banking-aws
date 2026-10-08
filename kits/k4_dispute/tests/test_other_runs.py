"""Another run's response: a run that finds another run's response already landed reports who
answered and sends nothing, and attempt numbers are re-derived under the lock."""

from __future__ import annotations

from datetime import timedelta

import pytest
from awx_k4_dispute import SPECS
from awx_k4_dispute.provider import (
    challenge_request_id,
)
from k4_harness import (
    NOW,
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


# ---- already done by another proposal ---------------------------------------------------------


def test_a_run_that_finds_another_runs_challenge_reports_already_done(port, strong_case):
    from awx_k4_dispute.cases import record_attempt, take_lock
    from awx_k4_dispute.provider import parse_dispute

    raw = challenge_input(strong_case)
    decision = CHALLENGE.policy(CHALLENGE.validate(raw), make_ctx(port, CHALLENGE))
    # Approve this run; while it uploads, another run records, sends and lands its challenge.
    uploads = []
    up = port.port.upload

    def upload(data, **kw):
        uploads.append(1)
        r = up(data, **kw)
        if len(uploads) == 2:
            view = parse_dispute(port.disputes[strong_case])
            store = port.kit_store
            item = store.get(f"case/{strong_case}")
            lock_owner = item.value["lock"]["owner"]
            store.put(
                f"case/{strong_case}", {**item.value, "lock": None}, expected_version=item.version
            )
            take_lock(
                store, strong_case, "pid-other", "challenge", view, port.now(), timedelta(hours=1)
            )
            record_attempt(
                store,
                strong_case,
                "pid-other",
                "challenge",
                CHALLENGE.validate(raw),
                decision,
                challenge_request_id(strong_case, "RFI"),
            )
            port.disputes[strong_case]["status"] = "CHALLENGED"
            port.disputes[strong_case]["challenge_details"] = [{"stage": "RFI"}]
            assert lock_owner
        return r

    port.port.upload = upload
    out = run(CHALLENGE, raw, port=port, decide=approve("a-1"))
    assert "already done by proposal pid-other" in out.postcondition.detail
    assert not out.postcondition.ok
    assert port.writes("disputes.challenge") == []


# ---- who answered ---------------------------------------------------------------------------


def _clear_then_land_late(port, dispute_id):
    port.timeout("disputes.challenge", landed=False)
    first = run(CHALLENGE, challenge_input(dispute_id), port=port, decide=approve("a-1"))
    past_clear_bound(port)
    ref = challenge_request_id(dispute_id, "RFI")
    run(
        CLEAR,
        {"dispute_id": dispute_id, "request_ref": ref, "note": "n"},
        port=port,
        decide=approve("a-1"),
    )
    return first.proposal.proposal_id


def test_already_done_names_the_landed_uncleared_attempt(port, strong_case):
    cleared = _clear_then_land_late(port, strong_case)
    second = run(
        CHALLENGE,
        challenge_input(strong_case),
        port=port,
        decide=approve("a-1"),
        now=NOW + timedelta(minutes=5),
    )
    assert second.state == "SUCCEEDED", second.detail
    # A third run built before the landing was visible would report who answered: simulate
    # its postcondition directly.
    from awx_common.workflow import StepContext

    raw = challenge_input(strong_case)
    intent = CHALLENGE.validate(raw)
    decision = CHALLENGE.policy(intent, make_ctx(port, CHALLENGE, NOW))
    post = CHALLENGE.postcondition(
        StepContext(intent, decision, make_ctx(port, CHALLENGE), "a" * 64, {}, "pid-third")
    )
    assert not post.ok
    assert f"proposal {second.proposal.proposal_id}" in post.detail
    assert cleared not in post.detail


def test_own_refusal_while_another_run_answered_records_refused(port, strong_case):
    _clear_then_land_late(port, strong_case)
    # The second run's challenge is refused (Airwallex already holds the first, landed late).
    original = port.fake.execute

    def first_lands_then_refuse(op):
        if op.name == "disputes.challenge":
            port.disputes[strong_case]["status"] = "CHALLENGED"
            port.disputes[strong_case]["challenge_details"] = [{"stage": "RFI"}]
        return original(op)

    port.fake.execute = first_lands_then_refuse
    out = run(
        CHALLENGE,
        challenge_input(strong_case),
        port=port,
        decide=approve("a-1"),
        now=NOW + timedelta(minutes=5),
    )
    port.fake.execute = original
    assert "answered by another run" in out.postcondition.detail
    mine = [
        r for r in port.case(strong_case)["responses"] if r["owner"] == out.proposal.proposal_id
    ]
    assert mine[0]["state"] == "REFUSED"
    assert "lock" not in port.case(strong_case)


# ---- numbering under the lock ---------------------------------------------------------------


def test_two_proposals_after_a_clear_only_the_first_sends(port, strong_case):
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    past_clear_bound(port)
    run(
        CLEAR,
        {
            "dispute_id": strong_case,
            "request_ref": challenge_request_id(strong_case, "RFI"),
            "note": "n",
        },
        port=port,
        decide=approve("a-1"),
    )
    from awx_common.workflow import StepContext
    from awx_k4_dispute.workflow import StaleDispute

    later = NOW + timedelta(minutes=5)
    raw_b = dict(challenge_input(strong_case), narrative="Person B's challenge.")
    intent_b = CHALLENGE.validate(raw_b)
    decision_b = CHALLENGE.policy(intent_b, make_ctx(port, CHALLENGE, later))  # numbered :2
    assert decision_b.computed["response_attempt"] == 2
    a = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"), now=later)
    assert a.state == "SUCCEEDED", a.detail
    port.disputes[strong_case]["status"] = "REQUIRES_RESPONSE"  # let B's build get that far
    port.disputes[strong_case]["challenge_details"] = []
    item = port.kit_store.get(f"case/{strong_case}")
    port.kit_store.put(
        f"case/{strong_case}",
        {k: v for k, v in item.value.items() if k != "lock"},
        expected_version=item.version,
    )
    from awx_k4_dispute.provider import parse_dispute
    from awx_k4_dispute.workflow import _numbered

    view = parse_dispute(port.disputes[strong_case])
    ctx_b = StepContext(intent_b, decision_b, make_ctx(port, CHALLENGE), "b" * 64, {}, "pid-b")
    with pytest.raises(StaleDispute, match="attempt 3, approved 2"):  # the check under the lock
        _numbered(ctx_b, "challenge", view)
    assert len(port.writes("disputes.challenge")) == 2  # the first, then A under :2
