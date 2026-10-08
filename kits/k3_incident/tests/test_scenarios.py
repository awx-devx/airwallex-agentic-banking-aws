"""Kit 3 scenarios through the real runner (awx_spine_aws.local_runner) against FakeAirwallex.

Every test wires the fake clock into the runner, executor and stores (one clock) and shares one
operation store and one kit store across runs, as the deployed tables do.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from awx_common import Operation, OperationResult, OperationStatus
from awx_common.approvals import Origin
from awx_common.workflow import StepContext
from awx_k3_incident import SPECS, ledger
from awx_k3_incident import airwallex as awx
from awx_k3_incident.workflow import STAGING_SPECS, _settle_lock, build_specs, replacement_op
from awx_spine.fake import Fault
from k3_fixtures import BANK_RETURN, Kit3

DAY = 86400


def lock(kit: Kit3, oid: str) -> ledger.Lock | None:
    return ledger.get_lock(kit.store, oid)


def assessment(kit: Kit3, oid: str):
    return kit.read("read_incident_assessment", {"transfer_id": oid})


# ---- the required acceptance test -------------------------------------------------------


def acceptance(kit: Kit3) -> None:
    """The kit 3 acceptance scenario: at SENT assert WAIT, no duplicate lock and zero new
    transfers. Move the original SENT -> FAILED with a failure type, approve replacement, then
    assert one new request ID, one replacement and a retained original failure record."""
    oid = kit.staged_original(f"inv-{kit.now().timestamp():.0f}")
    original = kit.transfer(oid)
    assert original["reference"].startswith(awx.POC_LABEL)
    assert original["reference"].endswith(awx.NOSETTLE_MARKER)
    kit.fake.simulate("transfer_transition", transfer_id=oid, next_status="SENT")

    a = assessment(kit, oid)
    assert a["decision"] == "WAIT"
    assert a["duplicate_lock_held"] is False
    assert kit.replace(oid).state == "BLOCKED"
    assert kit.replacements_of(oid) == []  # zero new transfers

    sim = kit.run("simulate_transfer_failure", {"transfer_id": oid, "failure_type": BANK_RETURN})
    assert sim.state == "SUCCEEDED", sim.detail
    assert sim.proposal.risk_tier.value == "SINGLE_APPROVER"  # model-started: a person approves
    assert kit.transfer(oid)["status"] == "CANCELLED"
    assert kit.transfer(oid)["failure"]["code"] == "90802"

    out = kit.replace(oid)
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert out.proposal.risk_tier.value == "SINGLE_APPROVER"
    (rep,) = kit.replacements_of(oid)
    assert rep["status"] == "PAID"  # the sandbox driver settled it (rule 10)
    assert rep["request_id"] == awx.replacement_request_id(oid, 1)
    assert rep["request_id"] != kit.transfer(oid)["request_id"]
    assert awx.NOSETTLE_MARKER not in rep["reference"]
    assert kit.transfer(oid)["failure"]["code"] == "90802"  # original failure retained
    after = assessment(kit, oid)
    assert after["duplicate_lock_held"] is True
    assert after["closure"]["original_failure"]["code"] == "90802"
    assert after["closure"]["replacement_status"] == "PAID"
    assert lock(kit, oid).state == ledger.LANDED
    # Clean end: nothing left at SENT.
    assert not [t for t in kit.fake.objects("transfers") if t["status"] == "SENT"]


def test_required_acceptance_sent_waits_then_failed_replaces_exactly_once():
    acceptance(Kit3.new())


@pytest.mark.sandbox
def test_acceptance_scenario_sandbox():
    """The acceptance scenario on FakeAirwallex under the `sandbox` marker. The run against the
    real sandbox is test_acceptance_live.py (AWX_K3_LIVE=1, `flock /tmp/awx-sandbox.lock`, a
    fresh kit store and so a fresh book per run, M2)."""
    if os.environ.get("AWX_LIVE") == "1":  # pragma: no cover - live run is elsewhere
        pytest.skip("this test runs on FakeAirwallex; the live run is test_acceptance_live.py")
    acceptance(Kit3.new())


# ---- send and the staged original --------------------------------------------------------


def test_staged_original_is_held_at_sent_by_the_driver():
    kit = Kit3.new()
    oid = kit.staged_original()
    adv = SPECS["replace_failed_transfer"].steps[0].finality.sandbox_advance
    kit.fake.simulate("transfer_transition", transfer_id=oid, next_status="SENT")
    body = kit.fake.read(awx.transfer_read(oid))
    op = awx.transition_op(transfer_id=oid, next_status="SENT", intent_hash="a" * 64)
    assert (
        adv(op, OperationResult(op.request_id, OperationStatus.SUCCEEDED), body, kit.now(), 1)
        is None
    )


def test_same_payment_key_pays_once_and_a_fresh_book_never_adopts_it():
    """M2: payment_key is kit-local, so its request_id carries the book id."""
    kit = Kit3.new()
    kit.staged_original("inv-7")
    assert kit.send("inv-7").state == "BLOCKED"
    fresh = Kit3(kit.fake, beneficiary_id=kit.beneficiary_id)  # new kit store, same sandbox
    out = fresh.send("inv-7")
    assert out.state == "SUCCEEDED", out.detail
    rids = [t["request_id"] for t in kit.fake.objects("transfers")]
    assert len(rids) == len(set(rids)) == 2
    assert ledger.book_id(kit.store) != ledger.book_id(fresh.store)


def test_send_in_flight_past_the_margin_goes_to_a_person():
    kit = Kit3.new()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=8 * DAY)
    first = kit.send("inv-8")
    assert first.state == "FAILED"
    sends = len(kit.sends())
    kit.advance(6 * DAY)
    again = kit.send("inv-8")
    assert again.state == "BLOCKED"
    assert len(kit.sends()) == sends
    assert len(kit.fake.objects("transfers")) == 1


# ---- simulation -------------------------------------------------------------------------


@pytest.mark.parametrize("to", ["SCHEDULED", "PROCESSING", "SENT"])
def test_simulated_failure_ends_cancelled_with_code(to):
    kit = Kit3.new()
    oid = kit.staged_original()
    if to != "SCHEDULED":
        kit.advance(2)
    if to == "SENT":
        kit.fake.simulate("transfer_transition", transfer_id=oid, next_status="SENT")
    out = kit.run(
        "simulate_transfer_failure", {"transfer_id": oid, "failure_type": "ACCOUNT_CLOSED"}
    )
    assert out.state == "SUCCEEDED", out.detail
    assert kit.transfer(oid)["status"] == "CANCELLED"
    assert kit.transfer(oid)["failure"]["code"] == "90701"


def test_transition_500_that_still_applies_is_reconciled_not_resent():
    """FAKE-FIDELITY [tr3-sent]: the transition answered 500 and the transfer still moved."""
    kit = Kit3.new()
    oid = kit.staged_original()
    kit.fake.inject("transfers.simulate_transition", Fault.SERVER_ERROR, applied=True, times=2)
    out = kit.run("simulate_transfer_failure", {"transfer_id": oid, "failure_type": BANK_RETURN})
    assert out.state == "SUCCEEDED", out.detail
    transitions = [r for r in kit.fake.requests if r[1].endswith("/transition")]
    assert len(transitions) == 2  # SENT once, FAILED once; nothing resent
    assert kit.transfer(oid)["failure"]["code"] == "90802"


def test_simulation_refuses_a_transfer_kit3_did_not_create():
    kit = Kit3.new()
    other = Kit3(kit.fake, beneficiary_id=kit.beneficiary_id)
    op = awx.create_transfer_op(
        business_key="someone-else",
        request_id=awx.derive_request_id("k1", "someone-else", "x"),
        intent_hash="c" * 64,
        beneficiary_id=kit.beneficiary_id,
        amount=awx.amount_of("5.00", "USD"),
        transfer_method="LOCAL",
        reference="someone-else",
        metadata={},
    )
    tid = kit.fake.execute(op).provider_id
    out = other.run("simulate_transfer_failure", {"transfer_id": tid, "failure_type": BANK_RETURN})
    assert out.state == "BLOCKED"
    assert kit.transfer(tid)["status"] == "SCHEDULED"


# ---- finality of the original ----------------------------------------------------------


@pytest.mark.parametrize(
    "failure_type",
    ["ACCOUNT_CLOSED", None, "SYSTEM_ERROR", "CHANNEL_TIMEOUT", "INVALID_ACCOUNT_NAME_OR_NUMBER"],
)
def test_only_the_bank_return_code_can_be_replaced(failure_type):
    kit = Kit3.new()
    oid = kit.failed_original(failure_type)
    assert assessment(kit, oid)["decision"] == "ESCALATE"
    assert kit.replace(oid).state == "BLOCKED"
    assert kit.replacements_of(oid) == []


def test_bare_cancel_escalates():
    kit = Kit3.new()
    oid = kit.staged_original()
    kit.fake.simulate("transfer_transition", transfer_id=oid, next_status="CANCELLED")
    assert assessment(kit, oid)["decision"] == "ESCALATE"


def test_stale_read_still_showing_sent_waits():
    kit = Kit3.new()
    oid = kit.staged_original()
    kit.fake.simulate("transfer_transition", transfer_id=oid, next_status="SENT")
    kit.fake.simulate(
        "transfer_transition", transfer_id=oid, next_status="FAILED", failure_type=BANK_RETURN
    )
    kit.fake.inject("transfers.get", Fault.STALE_READ, times=1)
    kit.fake.inject("transfers.get", Fault.STALE_READ, times=1)  # previous state was SENT
    assert kit.replace(oid).state == "BLOCKED"
    assert kit.replacements_of(oid) == []


# ---- the replacement under faults --------------------------------------------------------


def test_timeout_after_submit_invisible_for_a_while_settles_on_read_back():
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=20)
    out = kit.replace(oid)
    assert out.state == "SUCCEEDED", out.detail
    assert len(kit.replacements_of(oid)) == 1


def test_list_lag_after_a_successful_create():
    """The create succeeds but its transfer is not readable for 15 s. local_runner's finality
    read raises NotFound and the run errors with the create result missing from the results
    (FINDING, REQUESTS). The kit fails closed: the lock stays IN_FLIGHT, and the next run
    resumes the same request_id and settles the one replacement."""
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=15)
    first = kit.replace(oid)
    assert len(kit.replacements_of(oid)) == 1
    if first.state != "SUCCEEDED":
        assert lock(kit, oid).state in ledger.LIVE_STATES
        kit.advance(20)
        again = kit.replace(oid, "INC-2")
        assert again.state == "SUCCEEDED", (again.detail, again.postcondition)
    assert len(kit.replacements_of(oid)) == 1
    assert kit.replacements_of(oid)[0]["status"] == "PAID"


def test_duplicate_delivery_creates_one_transfer():
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.DUPLICATE_DELIVERY)
    assert kit.replace(oid).state == "SUCCEEDED"
    assert len(kit.replacements_of(oid)) == 1


def test_unknown_outcome_keeps_money_in_flight_and_a_rerun_resumes_the_same_request_id():
    """M1: an unknown replacement stays in flight on its request_id; the lock is kept; a later
    run re-emits the identical Operation (rule 9 reads back first) and never mints attempt 2."""
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    first = kit.replace(oid, "INC-1")
    assert first.state == "FAILED"
    assert first.results["replace"][-1].status is OperationStatus.OUTCOME_UNKNOWN
    held = lock(kit, oid)
    assert (held.state, held.attempt, held.owner) == (
        ledger.IN_FLIGHT,
        1,
        first.proposal.proposal_id,
    )
    early = kit.replace(oid, "INC-2")  # still invisible: a resume, never a new attempt
    assert early.state == "FAILED"
    assert lock(kit, oid).owner == first.proposal.proposal_id  # M4: the resumer took nothing
    assert len(kit.replacements_of(oid)) == 1
    kit.advance(3600)
    later = kit.replace(oid, "INC-3")
    assert later.state == "SUCCEEDED", (later.detail, later.postcondition)
    (rep,) = kit.replacements_of(oid)
    assert rep["request_id"] == awx.replacement_request_id(oid, 1)
    assert rep["status"] == "PAID"
    assert lock(kit, oid).state == ledger.LANDED


def test_past_the_dedupe_margin_in_flight_money_goes_to_a_person():
    """M1 window clause: past ~6 days of the ~7-day window nothing is resent, even once the
    provider would accept the request_id as new (FakeAirwallex expires dedupe at 7 days)."""
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=30 * DAY)
    assert kit.replace(oid).state == "FAILED"
    sends = len(kit.sends())
    kit.advance(6 * DAY)
    out = kit.replace(oid, "INC-2")
    assert out.state == "BLOCKED"
    assert any("a person decides" in r for r in out.proposal.reasons)
    assert assessment(kit, oid)["decision"] == "ESCALATE"
    kit.advance(DAY + 3600)  # past the provider's 7-day dedupe
    assert kit.replace(oid, "INC-3").state == "BLOCKED"
    assert len(kit.sends()) == sends
    assert len(kit.replacements_of(oid)) == 1


def test_definitive_refusal_at_first_send_releases_the_lock_and_allows_attempt_2():
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    first = kit.replace(oid)
    assert first.state == "FAILED"
    assert lock(kit, oid).state == ledger.RELEASED
    assert kit.replacements_of(oid) == []
    second = kit.replace(oid, "INC-2")
    assert second.state == "SUCCEEDED", second.detail
    (rep,) = kit.replacements_of(oid)
    assert rep["request_id"] == awx.replacement_request_id(oid, 2)
    assert lock(kit, oid).owner == second.proposal.proposal_id


def test_refused_resend_after_an_invisible_landing_keeps_the_money_in_flight():
    """The first send lands but stays invisible; the resend is refused. That is NOT
    definitive (only a first send's refusal is). The result is OUTCOME_UNKNOWN
    with body["refused"], the lock stays IN_FLIGHT and attempt 2 never happens."""
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    kit.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    first = kit.replace(oid)
    last = first.results["replace"][-1]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN  # uncertain send, not FAILED
    assert lock(kit, oid).state == ledger.IN_FLIGHT
    kit.replace(oid, "INC-2")
    assert len(kit.replacements_of(oid)) == 1
    assert kit.replacements_of(oid)[0]["request_id"] == awx.replacement_request_id(oid, 1)


class CrashAfterSend:
    """An adapter that dies after its call left (rule 9: treated as an unknown outcome)."""

    def __init__(self, fake, op_name: str, times: int = 1) -> None:
        self.fake, self.op_name, self.times = fake, op_name, times

    def execute(self, op: Operation) -> OperationResult:
        result = self.fake.execute(op)
        if op.name == self.op_name and self.times:
            self.times -= 1
            raise RuntimeError("adapter Lambda timed out")
        return result

    def read(self, req):
        return self.fake.read(req)

    def __getattr__(self, name):  # clock, advance, objects, ... from the fake
        return getattr(self.fake, name)


def test_rate_limited_then_a_crash_after_send_still_one_replacement():
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.RATE_LIMIT)
    real = kit.fake
    kit.fake = CrashAfterSend(real, "transfers.create")  # type: ignore[assignment]
    try:
        out = kit.run(
            "replace_failed_transfer", {"original_transfer_id": oid, "incident_ref": "INC-1"}
        )
    finally:
        kit.fake = real
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert len(kit.replacements_of(oid)) == 1


def test_crash_in_the_build_before_sending_releases_the_lock(monkeypatch):
    """The build took the lock, then the runner refused what it built (here an Operation not
    bound to the proposal hash): the step never dispatched, so the owner releases it."""
    import awx_k3_incident.workflow as wf

    kit = Kit3.new()
    oid = kit.failed_original()
    good = wf.replacement_op
    monkeypatch.setattr(wf, "replacement_op", lambda c, h: good(c, "e" * 64))
    out = kit.replace(oid)
    assert out.state == "FAILED"
    assert lock(kit, oid).state == ledger.RELEASED
    assert kit.sends() == kit.sends()[:1]  # only the original's create
    monkeypatch.setattr(wf, "replacement_op", good)
    again = kit.replace(oid, "INC-2")
    assert again.state == "SUCCEEDED", again.detail
    assert len(kit.replacements_of(oid)) == 1


def test_adapter_refusal_before_send_keeps_money_in_flight():
    """The adapter refuses before anything leaves (RequestIdReuse), but the kit cannot prove
    nothing left from what the runner hands it, so the lock stays IN_FLIGHT (M1)."""
    kit = Kit3.new()
    oid = kit.failed_original()
    plan = (
        SPECS["replace_failed_transfer"]
        .policy({"original_transfer_id": oid, "incident_ref": "x"}, kit.context())
        .computed
    )
    kit.ops.begin(replacement_op({**plan, "reference": "poc-i3-other"}, "d" * 64))
    out = kit.replace(oid)
    assert out.state == "FAILED"
    assert kit.replacements_of(oid) == []
    assert lock(kit, oid).state == ledger.IN_FLIGHT


def test_two_incidents_for_one_transfer_make_one_replacement():
    kit = Kit3.new()
    oid = kit.failed_original()
    assert kit.replace(oid, "INC-1").state == "SUCCEEDED"
    other = kit.replace(oid, "INC-2")
    assert other.state == "BLOCKED"
    assert any("duplicate lock held" in r for r in other.proposal.reasons)
    assert len(kit.replacements_of(oid)) == 1


def test_a_fresh_kit_store_still_sees_the_replacement_on_airwallex():
    """The replacement's request_id is keyed on the Airwallex transfer id, not the book, so a
    lost or fresh store cannot pay the same transfer twice."""
    kit = Kit3.new()
    oid = kit.failed_original()
    assert kit.replace(oid).state == "SUCCEEDED"
    fresh = Kit3(kit.fake, ops=kit.ops, beneficiary_id=kit.beneficiary_id)
    assert fresh.replace(oid, "INC-9").state == "BLOCKED"
    assert len(kit.replacements_of(oid)) == 1


# ---- approvals, ownership, abandonment ---------------------------------------------------


def test_rejected_or_expired_proposals_send_nothing_and_release_nothing():
    """M4/rule 8: an abandoned proposal never ran a build, so it releases no lock."""
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    kit.replace(oid, "INC-1")
    before = lock(kit, oid)
    sends = len(kit.sends())
    assert kit.replace(oid, "INC-2", approve=False).state == "REJECTED"
    assert kit.replace(oid, "INC-3", approve=None).state == "EXPIRED"
    assert lock(kit, oid) == before
    assert len(kit.sends()) == sends


def test_original_replaced_while_approval_pending_turns_stale():
    """Rule 1: the policy re-runs before the first step; a second run's replacement lands
    while this approval sits, so this run sends nothing."""
    kit = Kit3.new()
    oid = kit.failed_original()

    def another_run_replaces_it():
        assert kit.replace(oid, "INC-OTHER").state == "SUCCEEDED"

    out = kit.replace(oid, "INC-1", between=another_run_replaces_it)
    assert out.state == "FAILED"
    assert "changed since the proposal" in out.detail
    assert "replace" not in out.results
    assert len(kit.replacements_of(oid)) == 1


def test_lock_owner_is_the_proposal_id_never_the_intent_hash():
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    out = kit.replace(oid)
    held = lock(kit, oid)
    assert held.owner == out.proposal.proposal_id
    assert held.owner != out.proposal.intent_hash


def _executor_result(kit: Kit3, oid: str, shape: str) -> tuple[Operation, OperationResult]:
    """The real awx_spine MutationExecutor (one attempt per call, as the adapter runs it)
    producing each uncertain-send shape for this incident's attempt-1 replacement."""
    from awx_spine.executor import MutationExecutor

    plan = (
        SPECS["replace_failed_transfer"]
        .policy({"original_transfer_id": oid, "incident_ref": "x"}, kit.context())
        .computed
    )
    op = replacement_op(plan, "a" * 64)
    ex = MutationExecutor(kit.fake, kit.ops, sleep=kit.fake.advance, max_attempts=1)
    if shape == "multiple_objects":
        kit.fake.execute(op)  # two transfers under one request_id: past the provider's
        kit.advance(8 * DAY)  # dedupe window the same body creates a second object
        kit.fake.execute(op)
        kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT)
    else:
        kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
        fault = {"refused": Fault.INSUFFICIENT_FUNDS, "retryable": Fault.RATE_LIMIT}[shape]
        kit.fake.inject("transfers.create", fault, times=3)
    first = ex.run(op)
    assert first.status is OperationStatus.OUTCOME_UNKNOWN
    return op, ex.run(op)  # the resend after the uncertain send


@pytest.mark.parametrize("shape", ["refused", "retryable", "multiple_objects"])
def test_uncertain_send_shapes_from_the_real_executor_keep_the_lock_in_flight(shape):
    """Refused or retryable resends after an uncertain send are
    OUTCOME_UNKNOWN with body["last_error"]; two objects under one request_id are
    OUTCOME_UNKNOWN multiple_objects. The kit keeps the lock IN_FLIGHT and never mints attempt
    2 for any of them."""
    kit = Kit3.new()
    oid = kit.failed_original()
    op, result = _executor_result(kit, oid, shape)
    assert result.status is OperationStatus.OUTCOME_UNKNOWN, result
    if shape == "multiple_objects":
        assert result.error_code == "multiple_objects"
    else:
        assert "last_error" in (result.body or {})
    plan = SPECS["replace_failed_transfer"].policy(
        {"original_transfer_id": oid, "incident_ref": "x"}, kit.context()
    )
    ledger.take_lock(
        kit.store,
        original_transfer_id=oid,
        owner="p-1",
        attempt=1,
        request_id=op.request_id,
        now=kit.now(),
        over=None,
    )
    ctx = StepContext(
        {"original_transfer_id": oid, "incident_ref": "x"},
        plan,
        kit.context(),
        "a" * 64,
        {"replace": (result,)},
        "p-1",
    )
    if shape == "multiple_objects":
        kit.advance(3600)  # both now listed: the kit sees them, LANDED-or-escalate
    _settle_lock(ctx, oid)
    assert lock(kit, oid).state != ledger.RELEASED
    nxt = kit.replace(oid, "INC-NEXT")
    rids = {t["request_id"] for t in kit.replacements_of(oid)}
    assert rids == {awx.replacement_request_id(oid, 1)}  # never attempt 2
    if shape == "multiple_objects":
        assert nxt.state == "BLOCKED"  # two objects: a person decides


@pytest.mark.parametrize(
    "result",
    [
        OperationResult(
            "r",
            OperationStatus.FAILED,
            400,
            body={"last_error": {"error_type": "InsufficientFunds"}},
            error_type="InsufficientFunds",
        ),
        OperationResult(
            "r", OperationStatus.FAILED, 400, body={"refused": {}}, error_type="InsufficientFunds"
        ),
        OperationResult(
            "r",
            OperationStatus.FAILED,
            200,
            error_type="StateConflict",
            error_code="request_id_reused",
        ),
    ],
)
def test_kit_defence_markers_keep_the_lock_in_flight_even_as_failed(result):
    """Kit-side defences: a FAILED result that carries an uncertain-send marker, or a
    read-back mismatch, is never read as a definitive refusal."""
    kit = Kit3.new()
    oid = kit.failed_original()
    plan = SPECS["replace_failed_transfer"].policy(
        {"original_transfer_id": oid, "incident_ref": "x"}, kit.context()
    )
    rid = plan.computed["replacement_request_id"]
    ledger.take_lock(
        kit.store,
        original_transfer_id=oid,
        owner="p-1",
        attempt=1,
        request_id=rid,
        now=kit.now(),
        over=None,
    )
    ctx = StepContext(
        {"original_transfer_id": oid, "incident_ref": "x"},
        plan,
        kit.context(),
        "a" * 64,
        {"replace": (replace(result, request_id=rid),)},
        "p-1",
    )
    _settle_lock(ctx, oid)
    assert lock(kit, oid).state == ledger.IN_FLIGHT


# ---- closure ----------------------------------------------------------------------------


def test_without_the_driver_close_incident_records_closure_once_paid():
    kit = Kit3.new()
    oid = kit.failed_original()
    nodrive = build_specs(None)["replace_failed_transfer"]
    out = kit.run(
        "replace_failed_transfer",
        {"original_transfer_id": oid, "incident_ref": "INC-1"},
        spec=nodrive,
    )
    assert out.state == "FAILED"
    assert "not closable yet" in out.postcondition.detail
    assert kit.run("close_incident", {"original_transfer_id": oid}).state == "BLOCKED"
    (rep,) = kit.replacements_of(oid)
    kit.fake.simulate("transfer_transition", transfer_id=rep["id"], next_status="SENT")
    kit.fake.simulate("transfer_transition", transfer_id=rep["id"], next_status="PAID")
    closed = kit.run("close_incident", {"original_transfer_id": oid})
    assert closed.state == "SUCCEEDED", closed.postcondition
    assert closed.proposal.risk_tier.value == "AUTO"
    assert lock(kit, oid).state == ledger.LANDED
    assert kit.run("close_incident", {"original_transfer_id": oid}).state == "SUCCEEDED"


def test_replacement_that_also_fails_goes_to_a_person():
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.run(
        "replace_failed_transfer",
        {"original_transfer_id": oid, "incident_ref": "INC-1"},
        spec=build_specs(None)["replace_failed_transfer"],
    )
    (rep,) = kit.replacements_of(oid)
    kit.fail(rep["id"], "ACCOUNT_CLOSED")
    out = kit.run("close_incident", {"original_transfer_id": oid})
    assert out.state == "POSTCONDITION_FAILED"
    assert kit.replace(oid, "INC-2").state == "BLOCKED"
    assert len(kit.replacements_of(oid)) == 1


@pytest.mark.parametrize(("extra", "ok"), [("0.00", True), ("-0.01", False)])
def test_swift_replacement_budgets_the_second_fee(extra, ok):
    """The original's fee is not refunded on CANCELLED (the fake refunds amount + fee, so the
    wallet is set after the failure); the replacement needs amount + one more fee."""
    kit = Kit3.new()
    op = awx.create_transfer_op(
        business_key="poc-i3-swift",
        request_id=awx.derive_request_id("k3", "poc-i3-swift", "original"),
        intent_hash="c" * 64,
        beneficiary_id=kit.beneficiary_id,
        amount=awx.amount_of("100.00", "USD"),
        transfer_method="SWIFT",
        reference="poc-i3-swift-nosettle",
        metadata={"poc_label": "poc-i3-original"},
    )
    oid = kit.fake.execute(op).provider_id
    kit.fail(oid)
    fee = Decimal(str(kit.transfer(oid)["fee_amount"]))
    assert fee > 0
    have = kit.balance()
    need = Decimal("100.00") + fee + Decimal(extra)
    kit.fake.credit("USD", need - have)  # set the wallet (the fake's public API)
    d = SPECS["replace_failed_transfer"].policy(
        {"original_transfer_id": oid, "incident_ref": "I"}, kit.context()
    )
    assert (d.risk_tier.value != "BLOCKED") is ok
    if ok:
        assert d.computed["required_funds"].amount == Decimal("100.00") + fee
        assert d.computed["fee"].amount == fee


def test_model_started_simulation_needs_a_person_but_a_schedule_does_not():
    kit = Kit3.new()
    oid = kit.staged_original()
    sched = kit.run(
        "simulate_transfer_failure",
        {"transfer_id": oid, "failure_type": BANK_RETURN},
        origin=Origin.SCHEDULE,
    )
    assert sched.proposal.risk_tier.value == "AUTO"


def test_past_margin_uses_the_lock_time_not_the_proposal_time():
    held = ledger.Lock("o", "p", 1, "r", ledger.IN_FLIGHT, Kit3.new().now(), Kit3.new().now())
    assert not held.past_margin(held.taken_at + timedelta(days=5, hours=23))
    assert held.past_margin(held.taken_at + timedelta(days=6))


# ---- a second run sends on the owner's live attempt ------------------------------------


class SecondRunInsideFirstSend:
    """Port wrapper: while run A's replacement create is in flight, run B resumes the same
    attempt and its send lands (hidden by list lag); then A's own send is refused."""

    def __init__(self, kit: Kit3, *, lag_s: float = 600) -> None:
        self.kit, self.fake, self.lag_s = kit, kit.fake, lag_s
        self.inside = False
        self.second = None

    def execute(self, op: Operation) -> OperationResult:
        if op.name == "transfers.create" and not self.inside and self.second is None:
            self.inside = True
            try:
                self.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=self.lag_s)
                self.second = self.kit.replace(op.body["metadata"]["replaces_transfer_id"], "INC-B")
            finally:
                self.inside = False
            self.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
        return self.fake.execute(op)

    def read(self, req):
        return self.fake.read(req)

    def __getattr__(self, name):
        return getattr(self.fake, name)


def test_owner_never_releases_an_attempt_another_run_joined():
    """Through the real runner: A takes the lock and sends;
    inside A's send, B resumes attempt 1 and its send lands unseen; A's own send is refused
    InsufficientFunds. A must NOT release the lock, so C cannot mint attempt 2 and pay twice."""
    kit = Kit3.new()
    oid = kit.failed_original()
    real = kit.fake
    wrapper = SecondRunInsideFirstSend(kit)
    kit.fake = wrapper  # type: ignore[assignment]
    try:
        first = kit.replace(oid, "INC-A")
    finally:
        kit.fake = real
    assert wrapper.second is not None
    assert first.state == "FAILED"
    held = lock(kit, oid)
    assert held.state != ledger.RELEASED, held
    third = kit.replace(oid, "INC-C")
    assert third.state != "SUCCEEDED" or third.proposal.computed["attempt"] == 1
    kit.advance(wrapper.lag_s + 60)
    kit.replace(oid, "INC-D")
    reps = kit.replacements_of(oid)
    assert len(reps) == 1
    assert reps[0]["request_id"] == awx.replacement_request_id(oid, 1)


def test_a_joined_lock_cannot_be_released_and_a_join_racing_a_release_loses():
    kit = Kit3.new()
    held = ledger.take_lock(
        kit.store,
        original_transfer_id="t",
        owner="p-A",
        attempt=1,
        request_id="r",
        now=kit.now(),
        over=None,
    )
    joined = ledger.join_lock(kit.store, held, "p-B", kit.now())
    assert joined.joined_by == ("p-B",)
    assert ledger.join_lock(kit.store, joined, "p-B", kit.now()) == joined  # idempotent
    with pytest.raises(ledger.LedgerConflict, match="never release"):
        ledger.move_lock(kit.store, joined, ledger.RELEASED, kit.now(), "refused")
    # The owner's postcondition read the lock before the join: its release loses on version.
    fresh = ledger.take_lock(
        kit.store,
        original_transfer_id="u",
        owner="p-A",
        attempt=1,
        request_id="r2",
        now=kit.now(),
        over=None,
    )
    ledger.join_lock(kit.store, fresh, "p-B", kit.now())
    with pytest.raises(ledger.LedgerConflict):
        ledger.move_lock(kit.store, fresh, ledger.RELEASED, kit.now(), "stale read")
    assert lock(kit, "u").state == ledger.SENDING


def test_lost_stores_and_a_listed_replacement_past_the_margin_never_resend():
    """With the kit store and the operation table both lost there is
    no lock, so the margin is measured from the listed replacement's created_at."""
    kit = Kit3.new()
    oid = kit.failed_original()
    nodrive = build_specs(None)["replace_failed_transfer"]
    kit.run(
        "replace_failed_transfer",
        {"original_transfer_id": oid, "incident_ref": "INC-1"},
        spec=nodrive,
        sandbox=False,
    )
    assert kit.replacements_of(oid)[0]["status"] in ("SCHEDULED", "PROCESSING")
    kit.advance(7.2 * DAY)  # past the provider's dedupe too
    lost = Kit3(kit.fake, beneficiary_id=kit.beneficiary_id)  # fresh kit store AND op store
    out = lost.replace(oid, "INC-2")
    assert out.state == "BLOCKED"
    assert assessment(lost, oid)["decision"] == "ESCALATE"
    assert len(kit.replacements_of(oid)) == 1


def test_a_listed_replacement_inside_the_margin_is_still_resumed():
    kit = Kit3.new()
    oid = kit.failed_original()
    kit.run(
        "replace_failed_transfer",
        {"original_transfer_id": oid, "incident_ref": "INC-1"},
        spec=build_specs(None)["replace_failed_transfer"],
        sandbox=False,
    )
    kit.advance(DAY)
    out = kit.replace(oid, "INC-2")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert len(kit.replacements_of(oid)) == 1


def test_a_send_refused_before_it_left_is_flagged_for_a_person():
    """An attempt stuck on RequestIdReuse says so in the detail."""
    kit = Kit3.new()
    oid = kit.failed_original()
    plan = (
        SPECS["replace_failed_transfer"]
        .policy({"original_transfer_id": oid, "incident_ref": "x"}, kit.context())
        .computed
    )
    kit.ops.begin(replacement_op({**plan, "reference": "poc-i3-other"}, "d" * 64))
    out = kit.replace(oid)
    (rec,) = out.results["replace"]
    assert rec.status is OperationStatus.OUTCOME_UNKNOWN
    assert rec.error_code == "request_id_bound_to_another_body"
    assert "NEEDS_PERSON: request_id_bound_to_another_body" in out.postcondition.detail
    assert lock(kit, oid).state == ledger.IN_FLIGHT
    assert kit.replacements_of(oid) == []


def test_a_joined_lock_whose_joiner_sent_nothing_is_flagged_for_a_person():
    """The owner cannot release a joined lock, so it says so."""
    kit = Kit3.new()
    oid = kit.failed_original()
    real = kit.fake
    wrapper = SecondRunInsideFirstSend(kit)
    kit.fake = wrapper  # type: ignore[assignment]
    try:
        first = kit.replace(oid, "INC-A")
    finally:
        kit.fake = real
    assert "NEEDS_PERSON: joined by" in (first.postcondition.detail or "")
    assert lock(kit, oid).state == ledger.IN_FLIGHT


def test_listed_past_margin_fails_closed_without_a_time():
    """A listed replacement with no current time counts as past the margin."""
    from awx_k3_incident.policy import listed_past_margin

    assert listed_past_margin({"created_at": "2026-10-06T00:00:00+0000"}, None) is True


def test_kit_margin_is_the_spine_margin():
    from awx_spine.executor import DEDUP_RESEND_MARGIN

    assert ledger.DEDUPE_MARGIN == DEDUP_RESEND_MARGIN


# ---- a join that lands after the owner read the lock -----------------------------------


class JoinInsideOwnersPostcondition:
    """Port wrapper (probe P1): A's create is refused; on A's first transfers read after the
    create (its postcondition), run B, which joins the live lock while A holds a stale copy."""

    def __init__(self, kit: Kit3) -> None:
        self.kit, self.fake = kit, kit.fake
        self.created = False
        self.second = None

    def execute(self, op: Operation) -> OperationResult:
        if op.name == "transfers.create" and not self.created:
            self.created = True
            self.oid = op.body["metadata"]["replaces_transfer_id"]
            self.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
        return self.fake.execute(op)

    def read(self, req):
        if self.created and self.second is None and req.path == "/api/v1/transfers":
            self.second = "running"
            self.second = self.kit.replace(self.oid, "INC-B")
        return self.fake.read(req)

    def __getattr__(self, name):
        return getattr(self.fake, name)


def test_a_join_after_the_owner_read_the_lock_is_flagged_and_in_flight():
    """No run may leave the lock SENDING and unflagged."""
    kit = Kit3.new()
    oid = kit.failed_original()
    creates_before = len(kit.sends())
    real = kit.fake
    wrapper = JoinInsideOwnersPostcondition(kit)
    kit.fake = wrapper  # type: ignore[assignment]
    try:
        first = kit.replace(oid, "INC-A")
    finally:
        kit.fake = real
    second = wrapper.second
    assert second is not None
    assert second != "running"
    assert "NEEDS_PERSON: lock not moved" in first.postcondition.detail, first.postcondition
    assert "NEEDS_PERSON: joined" in second.postcondition.detail, second.postcondition
    held = lock(kit, oid)
    assert held.state == ledger.IN_FLIGHT
    assert held.joined_by
    third = kit.replace(oid, "INC-C")
    assert "NEEDS_PERSON" in (third.postcondition.detail if third.postcondition else "NEEDS_PERSON")
    assert kit.replacements_of(oid) == []
    assert len(kit.sends()) == creates_before + 1  # A's refused create only


# ---- the operator staging path's guardrails --------------------------------------------


def test_staging_is_capped_at_ten():
    kit = Kit3.new()
    out = kit.send("inv-big", amount="10.01")
    assert out.state == "BLOCKED"
    assert any("at most 10.00" in r for r in out.proposal.reasons)
    assert kit.sends() == []
    assert kit.send("inv-ok", amount="10.00").state == "SUCCEEDED"


def test_staging_pays_usd_only():
    kit = Kit3.new()
    out = kit.run(
        "send_supplier_transfer",
        {
            "payment_key": "inv-eur",
            "beneficiary_id": kit.beneficiary_id,
            "amount": "10.00",
            "currency": "EUR",
            "transfer_method": "LOCAL",
        },
        spec=STAGING_SPECS["send_supplier_transfer"],
        origin=Origin.OPERATOR,
    )
    assert out.state == "BLOCKED"
    assert any("USD only" in r for r in out.proposal.reasons)
    assert kit.sends() == []


def test_staging_refuses_a_beneficiary_kit3_did_not_create_or_that_does_not_exist():
    from dataclasses import replace as dc_replace

    kit = Kit3.new()
    other = dc_replace(kit_beneficiary_other(kit))
    foreign = str(kit.fake.execute(other).provider_id)
    for ben in (foreign, "00000000-0000-0000-0000-000000000000"):
        out = kit.run(
            "send_supplier_transfer",
            {
                "payment_key": f"inv-{ben[:4]}",
                "beneficiary_id": ben,
                "amount": "5.00",
                "currency": "USD",
                "transfer_method": "LOCAL",
            },
            spec=STAGING_SPECS["send_supplier_transfer"],
            origin=Origin.OPERATOR,
        )
        assert out.state == "BLOCKED", ben
        assert any("blocked" in r for r in out.proposal.reasons)
    assert kit.sends() == []


def test_staging_shows_the_approver_who_is_paid():
    kit = Kit3.new()
    out = kit.send("inv-who", amount="5.00")
    payee = out.proposal.computed["beneficiary"]
    assert payee["nickname"].startswith("poc-i3-")
    assert set(payee) == {"nickname", "name", "bank", "account_last4"}


def kit_beneficiary_other(kit):
    op = kit_beneficiary_op_named(kit, "not-kit3")
    return op


def kit_beneficiary_op_named(kit, nickname):
    from awx_common import derive_request_id as rid_of

    rid = rid_of("k3", f"ben:{nickname}", "create")
    return Operation(
        kit="k3",
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body={
            "request_id": rid,
            "nickname": nickname,
            "transfer_methods": ["LOCAL"],
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "address": {"country_code": "US", "state": "US-NY"},
                "bank_details": {
                    "bank_account_category": "Checking",
                    "account_routing_type1": "aba",
                },
            },
        },
        request_id=rid,
        business_key=f"ben:{nickname}",
        intent_hash="b" * 64,
    )
