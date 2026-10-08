"""DynamoDbOperationStore against moto: conditional writes, reuse and first-final-wins."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from awx_common import derive_request_id
from awx_common.ops import Operation, OperationResult, OperationStatus
from awx_spine.executor import MutationExecutor
from awx_spine.store import RequestIdReuse, UnknownRequestId
from awx_spine_aws.store import DynamoDbOperationStore
from moto import mock_aws

HASH = "a" * 64


def op(body=None, request_id=None) -> Operation:
    return Operation(
        kit="k3",
        name="transfers.create",
        method="POST",
        path="/api/v1/transfers/create",
        body=body or {"amount": Decimal("10.10"), "currency": "AUD"},
        request_id=request_id or derive_request_id("k3", "bk-1", "pay"),
        business_key="bk-1",
        intent_hash=HASH,
    )


@pytest.fixture
def store():
    with mock_aws():
        db = boto3.client("dynamodb")
        db.create_table(
            TableName="ops",
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDbOperationStore(
            "ops", client=db, clock=lambda: datetime(2026, 10, 6, tzinfo=UTC)
        )


def test_begin_writes_pending_before_the_call(store):
    rec = store.begin(op())
    assert rec.status == "PENDING" and rec.attempts == 0 and rec.result is None
    assert store.get(rec.request_id) == rec


def test_begin_is_idempotent_for_the_same_body(store):
    first = store.begin(op())
    store.record_attempt(first.request_id)
    again = store.begin(op())
    assert again.attempts == 1  # the stored record, not a fresh one


def test_reused_request_id_with_a_different_body_is_refused(store):
    store.begin(op())
    with pytest.raises(RequestIdReuse):
        store.begin(op(body={"amount": Decimal("10.11"), "currency": "AUD"}))


def test_attempts_are_atomic_increments(store):
    rid = store.begin(op()).request_id
    for _ in range(3):
        rec = store.record_attempt(rid)
    assert rec.attempts == 3


def test_complete_round_trips_the_result_exactly(store):
    rid = store.begin(op()).request_id
    result = OperationResult(
        request_id=rid,
        status=OperationStatus.SUCCEEDED,
        http_status=201,
        provider_id="tfr_1",
        provider_status="PROCESSING",
        body={"amount": Decimal("10.10"), "fee": Decimal("0.000001")},
    )
    rec = store.complete(rid, result)
    assert rec.status == "SUCCEEDED"
    assert store.get(rid).result == result


def test_unknown_can_be_completed_later_but_a_final_result_wins(store):
    rid = store.begin(op()).request_id
    unknown = OperationResult(rid, OperationStatus.OUTCOME_UNKNOWN, error_type="OutcomeUnknown")
    assert store.complete(rid, unknown).status == "OUTCOME_UNKNOWN"
    ok = OperationResult(rid, OperationStatus.SUCCEEDED, provider_id="tfr_1")
    assert store.complete(rid, ok).result == ok
    late = OperationResult(rid, OperationStatus.FAILED, error_type="ValidationFailed")
    assert store.complete(rid, late).result == ok  # first final result wins


def test_record_attempt_and_complete_need_an_existing_record(store):
    with pytest.raises(UnknownRequestId):
        store.record_attempt("missing")
    with pytest.raises(UnknownRequestId):
        store.complete("missing", OperationResult("missing", OperationStatus.SUCCEEDED))


def test_a_retryable_failure_is_overwritten_by_a_later_success(store):
    """429 then SUCCEEDED in one run of awx_spine's real executor must leave
    SUCCEEDED in the table, so a later run of the same request_id sends nothing."""

    class Port:
        def __init__(self):
            self.sends = 0

        def execute(self, op):
            self.sends += 1
            if self.sends == 1:
                return OperationResult(
                    op.request_id,
                    OperationStatus.FAILED,
                    http_status=429,
                    error_type="RateLimited",
                    error_code="too_many_requests",
                )
            return OperationResult(op.request_id, OperationStatus.SUCCEEDED, provider_id="t1")

        def read(self, req):
            return {"items": []}

    port = Port()
    executor = MutationExecutor(port, store, sleep=lambda s: None)
    assert executor.run(op()).status is OperationStatus.SUCCEEDED
    assert store.get(op().request_id).status == "SUCCEEDED"
    assert executor.run(op()).status is OperationStatus.SUCCEEDED
    assert port.sends == 2  # the second run was answered by the store


def test_a_non_retryable_failure_is_final(store):
    rid = store.begin(op()).request_id
    nsf = OperationResult(rid, OperationStatus.FAILED, error_type="InsufficientFunds")
    store.complete(rid, nsf)
    late = OperationResult(rid, OperationStatus.SUCCEEDED, provider_id="t1")
    assert store.complete(rid, late).result == nsf


def test_get_missing_is_none(store):
    assert store.get("nope") is None


# --- 429, then a send that leaves and crashes ------------------------------------------------


class _Port:
    """429 on the first send, then a send that lands at the provider and crashes."""

    def __init__(self, landed_list_item=True):
        self.sends = 0
        self.reads = 0
        self.landed = None
        self.list_item = landed_list_item

    def execute(self, op):
        self.sends += 1
        if self.sends == 1:
            return OperationResult(
                op.request_id,
                OperationStatus.FAILED,
                http_status=429,
                error_type="RateLimited",
                error_code="too_many_requests",
            )
        if self.sends == 2:
            self.landed = {"id": "obj-1", "request_id": op.request_id, **dict(op.body)}
            raise RuntimeError("adapter died after the request left")
        return OperationResult(op.request_id, OperationStatus.SUCCEEDED, provider_id="obj-2")

    def read(self, req):
        self.reads += 1
        items = [self.landed] if self.landed is not None and self.list_item else []
        return {"items": items}


def _rate_limited_then_crashed(store, op):
    port = _Port()
    executor = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1)
    assert executor.run(op).status is OperationStatus.FAILED  # the 429
    with pytest.raises(RuntimeError):
        executor.run(op)  # the resend leaves and the process dies
    rec = store.get(op.request_id)
    # The executor records its own crashed send as OUTCOME_UNKNOWN before the
    # exception propagates (record_attempt still reopened it to PENDING first).
    assert rec.status == "OUTCOME_UNKNOWN"
    assert rec.may_have_landed
    assert rec.attempts == 2
    return port


def test_429_then_crash_on_a_non_deduplicating_endpoint_is_never_resent(store):
    from awx_common.intent import derive_request_id as rid_of

    rid = rid_of("k2", "card:1", "auth")
    op = Operation(
        kit="k2",
        name="issuing.simulate_authorization",
        method="POST",
        path="/api/v1/simulation/issuing/create",
        body={"card_id": "c1", "transaction_amount": Decimal("5.00")},
        request_id=rid,
        business_key="card:1",
        intent_hash=HASH,
    )
    port = _rate_limited_then_crashed(store, op)
    later = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1)  # M1 re-emit
    result = later.run(op)
    assert result.status is OperationStatus.OUTCOME_UNKNOWN
    assert port.sends == 2  # nothing sent by the later run


def test_429_then_crash_on_a_deduplicating_endpoint_reads_back_before_any_resend(store):
    body = {
        "request_id": derive_request_id("k3", "bk-1", "pay"),
        "transfer_amount": Decimal("10.10"),
        "transfer_currency": "AUD",
    }
    port = _rate_limited_then_crashed(store, op(body=body))
    later = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1)
    result = later.run(op(body=body))
    assert port.reads >= 1
    assert port.sends == 2  # the read-back found the landed write; no resend
    assert result.status is OperationStatus.SUCCEEDED
    assert result.provider_id == "obj-1"
    assert store.get(op().request_id).status == "SUCCEEDED"


def test_record_attempt_never_reopens_a_definitive_record(store):
    rid = store.begin(op()).request_id
    store.complete(rid, OperationResult(rid, OperationStatus.SUCCEEDED, provider_id="t1"))
    with pytest.raises(ValueError, match="definitive"):
        store.record_attempt(rid)
    assert store.get(rid).status == "SUCCEEDED"


# --- may_have_landed -------------------------------------------------------------------------


def _unknown(o: Operation) -> OperationResult:
    return OperationResult(
        request_id=o.request_id,
        status=OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_message="timeout after submit",
    )


def test_may_have_landed_is_set_by_complete_in_one_update_and_never_cleared(store):
    o = op()
    store.begin(o)
    store.record_attempt(o.request_id)
    calls: list[dict] = []
    real = store._db.update_item

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    store._db.update_item = spy
    rec = store.complete(o.request_id, _unknown(o))
    assert rec.may_have_landed
    assert len(calls) == 1  # status, result and the flag in one UpdateItem
    assert "may_have_landed" in calls[0]["ExpressionAttributeNames"].values()
    assert "#s = :status" in calls[0]["UpdateExpression"]
    after_attempt = store.record_attempt(o.request_id)
    assert after_attempt.may_have_landed  # record_attempt keeps it
    rate_limited = OperationResult(
        request_id=o.request_id,
        status=OperationStatus.FAILED,
        http_status=429,
        error_type="RateLimited",
    )
    assert store.complete(o.request_id, rate_limited).may_have_landed
    ok = OperationResult(
        request_id=o.request_id, status=OperationStatus.SUCCEEDED, provider_id="t1"
    )
    assert store.complete(o.request_id, ok).may_have_landed  # never cleared
    assert store.get(o.request_id).may_have_landed


def test_a_record_never_unknown_has_no_flag(store):
    o = op()
    store.begin(o)
    store.record_attempt(o.request_id)
    rec = store.complete(
        o.request_id,
        OperationResult(
            request_id=o.request_id, status=OperationStatus.SUCCEEDED, provider_id="t1"
        ),
    )
    assert not rec.may_have_landed
    assert (
        "may_have_landed"
        not in store._db.get_item(TableName="ops", Key={"request_id": {"S": o.request_id}})["Item"]
    )


def test_executor_on_dynamodb_turns_a_refused_resend_into_unknown(store):
    """The adapter runs one send per call; the flag carries the uncertainty between calls."""
    o = op(
        body={
            "request_id": derive_request_id("k3", "bk-2", "pay"),
            "transfer_amount": Decimal("10.10"),
            "transfer_currency": "AUD",
        },
        request_id=derive_request_id("k3", "bk-2", "pay"),
    )

    class Port:
        def __init__(self, results):
            self.results, self.sent = list(results), 0

        def execute(self, op_):
            self.sent += 1
            return self.results.pop(0)(op_)

        def read(self, req):
            return {"items": []}

    def refused(o_):
        return OperationResult(
            request_id=o_.request_id,
            status=OperationStatus.FAILED,
            http_status=400,
            error_type="InsufficientFunds",
            error_code="balance_insufficient",
        )

    port = Port([_unknown, refused])
    first = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1).run(o)
    assert first.status is OperationStatus.OUTCOME_UNKNOWN
    second = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1).run(o)
    assert second.status is OperationStatus.OUTCOME_UNKNOWN
    assert second.body["refused"]["error_code"] == "balance_insufficient"
    rec = store.get(o.request_id)
    assert rec.status == "OUTCOME_UNKNOWN"
    assert rec.may_have_landed
    assert port.sent == 2


# --- first_attempt_at and attempt_started_at ------------------------------------------------


def test_attempt_times_are_written_in_the_record_attempt_update(aws_db_clock):
    store, clock = aws_db_clock
    o = op()
    store.begin(o)
    calls: list[dict] = []
    real = store._db.update_item

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    store._db.update_item = spy
    first = store.record_attempt(o.request_id)
    assert len(calls) == 1
    assert "attempt_started_at = :now" in calls[0]["UpdateExpression"]
    assert "if_not_exists(first_attempt_at, :now)" in calls[0]["UpdateExpression"]
    assert first.first_attempt_at == first.attempt_started_at == clock[0]
    clock[0] = clock[0].replace(hour=5)
    second = store.record_attempt(o.request_id)
    assert second.first_attempt_at == first.first_attempt_at  # written once
    assert second.attempt_started_at == clock[0]
    assert store.get(o.request_id).first_attempt_at == first.first_attempt_at


@pytest.fixture
def aws_db_clock():
    with mock_aws():
        db = boto3.client("dynamodb")
        db.create_table(
            TableName="ops",
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        clock = [datetime(2026, 10, 6, tzinfo=UTC)]
        yield DynamoDbOperationStore("ops", client=db, clock=lambda: clock[0]), clock
