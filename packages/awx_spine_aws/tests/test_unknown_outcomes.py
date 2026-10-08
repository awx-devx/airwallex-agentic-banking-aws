"""Rule 9 on OUTCOME_UNKNOWN codes, and stored mismatches in the older FAILED format.

Rule 9 stops at once on an OUTCOME_UNKNOWN code a resend cannot change (one adapter call, no
resend waits); in_progress_elsewhere stays resendable. A mismatch row stored as FAILED
request_id_reused is never definitive: a COMPLETE returns it in the one mismatch shape.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from awx_common import Origin
from awx_common.ops import OperationResult, OperationStatus
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import adapter_core, engine
from awx_spine_aws import store as aws_store
from awx_spine_aws.engine import Action
from awx_spine_aws.local_runner import run_kit_workflow
from awx_spine_aws.store import DynamoDbOperationStore

import toy

FEE = toy.fee_op().request_id


def _local(spec, port, store=None, clock=None):
    clock = clock or toy.Clock()
    return run_kit_workflow(
        spec,
        toy.RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=port,
        store=store if store is not None else InMemoryOperationStore(),
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.SCHEDULE,
        sandbox=True,
    )


# --- codes a resend cannot change ----------------------------------------------------------------


def _unknown_with(code):
    def dispatch(executor, store, op, action, result=None):
        calls.append(Action(action))
        if op.request_id != FEE:
            return real(executor, store, op, action, result)
        if Action(action) is Action.COMPLETE:
            return real(executor, store, op, action, result)
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.OUTCOME_UNKNOWN,
            error_type="OutcomeUnknown",
            error_code=code,
            error_message=f"executor said {code}",
        )

    real = adapter_core.dispatch
    calls: list = []
    return dispatch, calls


@pytest.mark.parametrize("code", sorted(engine.FINAL_UNKNOWN_CODES))
def test_rule9_stops_at_once_on_a_code_a_resend_cannot_change(monkeypatch, code):
    dispatch, calls = _unknown_with(code)
    monkeypatch.setattr(adapter_core, "dispatch", dispatch)
    clock = toy.Clock()
    out = _local(toy.toy_spec(), toy.ToyPort(), clock=clock)
    assert calls == [Action.SEND]  # one adapter call for the fee
    assert clock.sleeps == []  # no 5 + 10 + 20 s resend waits
    assert out.state == "FAILED"
    [fee] = out.results["fee"]
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN and fee.error_code == code


def test_the_final_codes_are_the_executors_names():
    from awx_spine import executor

    assert {
        executor.MISMATCH_CODE,
        executor.FIRST_ATTEMPT_UNKNOWN_CODE,
        executor.PAST_MARGIN_CODE,
    } <= engine.FINAL_UNKNOWN_CODES
    assert executor.IN_PROGRESS_CODE not in engine.FINAL_UNKNOWN_CODES


def test_in_progress_elsewhere_is_still_resent(monkeypatch):
    # Guard: these resends are spaced past the attempt lease (test_attempt_lease.py).
    dispatch, calls = _unknown_with("in_progress_elsewhere")
    monkeypatch.setattr(adapter_core, "dispatch", dispatch)
    clock = toy.Clock()
    _local(toy.toy_spec(), toy.ToyPort(), clock=clock)
    assert calls == [Action.SEND] + [Action.RESEND] * engine.MAX_RESENDS
    assert len(clock.sleeps) == engine.MAX_RESENDS


# --- a mismatch stored as FAILED -----------------------------------------------------------------


def _legacy_failed(op):
    return OperationResult(
        request_id=op.request_id,
        status=OperationStatus.FAILED,
        http_status=200,
        provider_id="tr_other",
        provider_status="PAID",
        error_type="StateConflict",
        error_code="request_id_reused",
        error_message="the object holding request_id does not match (transfer_amount)",
        body={"details": {"fields": ["transfer_amount"], "provider_id": "tr_other"}},
    )


def test_a_legacy_failed_mismatch_is_never_definitive():
    assert adapter_core.definitive(_legacy_failed(toy.fee_op())) is False


def test_a_legacy_failed_mismatch_in_dynamodb_never_returns_failed(aws, monkeypatch):
    store = DynamoDbOperationStore("ops", client=aws.db)
    op = toy.fee_op()
    store.begin(op)
    store.record_attempt(op.request_id)
    with monkeypatch.context() as m:  # a row in the older format: final = true
        m.setattr(aws_store, "definitive", lambda r: True)
        store.complete(op.request_id, _legacy_failed(op))
    unknown = OperationResult(
        request_id=op.request_id,
        status=OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_message="crash",
    )
    got = adapter_core.dispatch(None, store, op, Action.COMPLETE, unknown)
    assert got.status is OperationStatus.OUTCOME_UNKNOWN
    assert got.error_code == "request_id_reused"
    assert got.provider_id is None and got.provider_status is None
    assert got.body["details"] == {"fields": ["transfer_amount"], "provider_id": "tr_other"}
    assert got.body["last_error"]["http_status"] is None


def test_a_legacy_failed_mismatch_in_memory_is_overwritten_by_complete():
    store = InMemoryOperationStore()
    op = toy.fee_op()
    store.begin(op)
    store.complete(op.request_id, _legacy_failed(op))
    found = replace(toy.fee_op(), body=op.body)
    settled = OperationResult(
        request_id=found.request_id, status=OperationStatus.SUCCEEDED, provider_id="tr_1"
    )
    got = adapter_core.dispatch(None, store, op, Action.COMPLETE, settled)
    assert got.status is OperationStatus.SUCCEEDED  # the legacy FAILED did not win
