"""Uncertain sends: every OUTCOME_UNKNOWN shape the spine can return keeps a dispute response
in flight with the lock held, the real executor never resends an uncertain dispute response,
and a first send refused at create is definitive."""

from __future__ import annotations

import pytest
from awx_common import OperationResult, OperationStatus
from awx_common.workflow import StepContext
from awx_k4_dispute import SPECS
from awx_k4_dispute.cases import record_attempt, take_lock
from awx_k4_dispute.provider import (
    ChallengeReason,
    challenge_operation,
    challenge_request_id,
    parse_dispute,
)
from k4_harness import NOW, approve, challenge_input, make_ctx, run

CHALLENGE = SPECS["challenge_dispute"]


def _post_with(port, dispute_id, result: OperationResult):
    """Run the challenge postcondition as this run's owner, with `result` as its send."""
    intent = CHALLENGE.validate(challenge_input(dispute_id))
    ctx = make_ctx(port, CHALLENGE)
    decision = CHALLENGE.policy(intent, ctx)
    view = parse_dispute(port.disputes[dispute_id])
    take_lock(port.kit_store, dispute_id, "pid-1", "challenge", view, NOW, CHALLENGE_TTL)
    record_attempt(
        port.kit_store, dispute_id, "pid-1", "challenge", intent, decision, result.request_id
    )
    sctx = StepContext(intent, decision, ctx, "a" * 64, {"submit_challenge": (result,)}, "pid-1")
    return CHALLENGE.postcondition(sctx)


CHALLENGE_TTL = __import__("datetime").timedelta(hours=1)


@pytest.mark.parametrize(
    "result_kw",
    [
        # While uncertain, a FAILED resend comes back OUTCOME_UNKNOWN
        # with the error in body["last_error"], and also body["refused"] when definitive; no
        # top-level error_code or http_status.
        {
            "error_type": "OutcomeUnknown",
            "body": {
                "last_error": {
                    "error_type": "ValidationFailed",
                    "error_code": "validation_error",
                    "http_status": 400,
                    "error_message": "x",
                },
                "refused": {
                    "error_type": "ValidationFailed",
                    "error_code": "validation_error",
                    "http_status": 400,
                    "error_message": "x",
                },
            },
        },
        {
            "error_type": "OutcomeUnknown",
            "body": {
                "last_error": {
                    "error_type": "RateLimited",
                    "error_code": "too_many_requests",
                    "http_status": 429,
                    "error_message": "x",
                }
            },
        },
        # Two objects under one request_id.
        {
            "error_type": "OutcomeUnknown",
            "error_code": "multiple_objects",
            "body": {"provider_ids": ["dst_a", "dst_b"]},
        },
    ],
    ids=["refused-resend", "retryable-resend", "multiple-objects"],
)
def test_uncertain_send_shapes_keep_the_response_in_flight_and_the_lock_held(
    port, strong_case, result_kw
):
    """The executor cannot produce these for kit 4's dispute endpoints today (see the next
    test), but the shapes are the executor's for every endpoint; the postcondition must keep
    them in flight with the lock held whatever endpoint returns them."""
    rid = challenge_request_id(strong_case, "RFI")
    result = OperationResult(rid, OperationStatus.OUTCOME_UNKNOWN, **result_kw)
    post = _post_with(port, strong_case, result)
    assert not post.ok
    case = port.case(strong_case)
    assert case["lock"]["owner"] == "pid-1"
    assert case["responses"][0]["state"] == "IN_FLIGHT"
    blocked = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert blocked.state == "BLOCKED"
    assert port.writes() == []


@pytest.mark.parametrize("action", ["accept", "challenge"])
def test_real_executor_never_resends_an_uncertain_dispute_response(port, strong_case, action):
    """Through awx_spine's MutationExecutor: once a dispute response is uncertain,
    a later call with the identical Operation reconciles first, finds nothing, and stops at
    OUTCOME_UNKNOWN without sending, even with a refusal queued for the next send. So the
    refused / last_error resend shapes cannot arise on disputes.accept or disputes.challenge,
    and multiple_objects cannot either (the kit's reconcile hook returns one dispute)."""
    from awx_k4_dispute.finality import dispute_reconcile
    from awx_k4_dispute.provider import (
        AcceptReason,
        ChallengeReason,
        accept_operation,
        challenge_operation,
    )
    from awx_spine.executor import MutationExecutor

    if action == "accept":
        op = accept_operation(strong_case, "RFI", AcceptReason.INSUFFICIENT_EVIDENCE, "a" * 64)
    else:
        fid = port.fake.upload(b"%PDF-1.4 x", file_name="a.pdf", content_type="application/pdf")[
            "file_id"
        ]
        op = challenge_operation(
            strong_case,
            "RFI",
            ChallengeReason.CARDHOLDER_AUTHORISED,
            [("PRIMARY", fid)],
            "PHYSICAL_GOODS",
            "d",
            "a" * 64,
        )
    executor = MutationExecutor(port.port, port.ops, sleep=port.sleep, max_attempts=1)
    port.timeout(op.name, landed=False)
    rec = lambda o: dispute_reconcile(o, port.fake.read)  # noqa: E731
    first = executor.run(op, reconcile=rec)
    assert first.status is OperationStatus.OUTCOME_UNKNOWN
    assert port.ops.get(op.request_id).may_have_landed
    port.port.refuse[op.name] = 1
    second = executor.run(op, reconcile=rec)
    assert second.status is OperationStatus.OUTCOME_UNKNOWN
    assert "refused" not in (second.body or {})
    assert len(port.writes(op.name)) == 1  # never resent
    assert port.disputes[strong_case]["status"] == "REQUIRES_RESPONSE"


def test_a_first_send_refused_at_create_is_definitive(port, strong_case):
    rid = challenge_request_id(strong_case, "RFI")
    result = OperationResult(rid, OperationStatus.FAILED, 400, error_type="ValidationFailed")
    _post_with(port, strong_case, result)
    case = port.case(strong_case)
    assert "lock" not in case
    assert case["responses"][0]["state"] == "REFUSED"


# ---- reuse and mismatch shapes stay in flight -----------------------------------------------


def test_request_id_bound_to_another_body_through_the_runner_stays_in_flight(port, strong_case):
    """RequestIdReuse is recorded as OUTCOME_UNKNOWN request_id_bound_to_another_body.
    The operation store already holds the stage key with another body: the run stops, the
    attempt stays IN_FLIGHT with the lock held, and nothing is sent."""
    fid = port.fake.upload(b"%PDF-1.4 x", file_name="a.pdf", content_type="application/pdf")[
        "file_id"
    ]
    squat = challenge_operation(
        strong_case, "RFI", ChallengeReason.GOODS_DELIVERED, [("OTHER", fid)], "X", "Y", "b" * 64
    )
    port.ops.begin(squat)
    out = run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert out.state == "FAILED"
    last = out.results["submit_challenge"][-1]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN
    assert last.error_code == "request_id_bound_to_another_body"
    case = port.case(strong_case)
    assert case["lock"]["owner"] == out.proposal.proposal_id
    assert case["responses"][0]["state"] == "IN_FLIGHT"
    assert port.writes("disputes.challenge") == []


@pytest.mark.parametrize(
    "code",
    ["request_id_reused", "first_attempt_unknown", "past_dedupe_margin", "in_progress_elsewhere"],
)
def test_spine_stop_codes_stay_in_flight_at_the_postcondition(port, strong_case, code):
    rid = challenge_request_id(strong_case, "RFI")
    result = OperationResult(
        rid,
        OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_code=code,
        body={"details": {"provider_id": "dst_other"}},
    )
    post = _post_with(port, strong_case, result)
    assert not post.ok
    assert port.case(strong_case)["lock"]["owner"] == "pid-1"
    assert port.case(strong_case)["responses"][0]["state"] == "IN_FLIGHT"
