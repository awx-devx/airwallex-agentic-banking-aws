"""The executor's deduplication margin and attempt lease, and the shape of its uncertain results.

A rule-9 resend relies on Airwallex deduplicating a request_id for about 7 days, so no resend
follows once the first attempt is older than DEDUP_RESEND_MARGIN, or when its age is unknown.
A PENDING attempt younger than the lease may still be in flight elsewhere and is left alone.
A read-back that finds another body under the request_id is OUTCOME_UNKNOWN request_id_reused,
never FAILED."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from awx_common import Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.errors import TransientServerError
from awx_spine import InMemoryOperationStore, MutationExecutor
from awx_spine.executor import (
    DEDUP_RESEND_MARGIN,
    DEFAULT_ATTEMPT_LEASE_S,
    FIRST_ATTEMPT_UNKNOWN_CODE,
    IN_PROGRESS_CODE,
    PAST_MARGIN_CODE,
)
from awx_spine.testing import transfer_op

OK = OperationStatus.SUCCEEDED
FAILED = OperationStatus.FAILED
UNKNOWN = OperationStatus.OUTCOME_UNKNOWN
USD = "USD"
T0 = datetime(2026, 10, 6, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += timedelta(seconds=s)


class Port:
    def __init__(self, results=(), items=()) -> None:
        self.results, self.items = list(results), list(items)
        self.sent: list[Operation] = []
        self.reads: list[ReadRequest] = []

    def execute(self, op):
        self.sent.append(op)
        return self.results.pop(0)(op)

    def read(self, req):
        self.reads.append(req)
        return {"items": list(self.items)}


def _unknown(op):
    return OperationResult(request_id=op.request_id, status=UNKNOWN, error_type="OutcomeUnknown")


def _ok(op):
    return OperationResult(request_id=op.request_id, status=OK, provider_id="t1")


def _429(op):
    return OperationResult(
        request_id=op.request_id,
        status=FAILED,
        http_status=429,
        error_type="RateLimited",
        error_code="too_many_requests",
    )


# ---- deduplication margin ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "resent"),
    [(timedelta(days=5, hours=23), True), (DEDUP_RESEND_MARGIN + timedelta(seconds=1), False)],
)
def test_no_rule9_resend_once_the_first_attempt_is_past_the_margin(age, resent):
    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    port = Port([_unknown, _ok])
    first = MutationExecutor(port, store, sleep=clock.sleep, clock=clock, max_attempts=1)
    assert first.run(op).status is UNKNOWN  # empty read-back follows; nothing landed
    clock.t += age
    later = MutationExecutor(port, store, sleep=clock.sleep, clock=clock, max_attempts=1)
    result = later.run(op)
    assert len(port.sent) == (2 if resent else 1)
    if resent:
        assert result.status is OK
    else:
        assert (result.status, result.error_code) == (UNKNOWN, PAST_MARGIN_CODE)
        assert "6-day" in result.error_message
        assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"


def test_the_margin_counts_from_the_first_attempt_not_the_latest():
    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    port = Port([_unknown, _429, _ok])
    ex = MutationExecutor(port, store, sleep=clock.sleep, clock=clock, max_attempts=1)
    ex.run(op)
    clock.t += timedelta(days=3)
    assert ex.run(op).status is UNKNOWN  # the 429 resend, held as unknown
    rec = store.get(op.request_id)
    assert rec.first_attempt_at == T0
    assert rec.attempt_started_at > T0
    clock.t += timedelta(days=3, seconds=10)
    result = ex.run(op)
    assert result.error_code == PAST_MARGIN_CODE
    assert result.body["last_error"]["error_type"] == "RateLimited"
    assert len(port.sent) == 2


def test_first_attempt_at_is_written_once():
    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    store.begin(op)
    assert store.get(op.request_id).first_attempt_at is None
    store.record_attempt(op.request_id)
    clock.t += timedelta(hours=5)
    rec = store.record_attempt(op.request_id)
    assert rec.first_attempt_at == T0
    assert rec.attempt_started_at == T0 + timedelta(hours=5)


# ---- attempt lease ----------------------------------------------------------------------------


def test_a_live_attempt_elsewhere_is_unknown_without_read_or_send():
    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    store.begin(op)
    store.record_attempt(op.request_id)  # another caller's send is in flight now
    port = Port([_ok], items=[])
    clock.t += timedelta(seconds=120)  # inside the adapter's 135 s timeout, so still live
    result = MutationExecutor(port, store, sleep=clock.sleep, clock=clock).run(op)
    assert (result.status, result.error_code) == (UNKNOWN, IN_PROGRESS_CODE)
    assert port.sent == []
    assert port.reads == []
    assert store.get(op.request_id).status == "PENDING"  # untouched


def test_an_attempt_older_than_the_lease_is_crashed_and_reconciled():
    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    store.begin(op)
    store.record_attempt(op.request_id)
    clock.t += timedelta(seconds=DEFAULT_ATTEMPT_LEASE_S + 1)
    landed = {**op.body, "id": "t1", "status": "PROCESSING"}
    port = Port(items=[landed])
    result = MutationExecutor(port, store, sleep=clock.sleep, clock=clock).run(op)
    assert result.status is OK
    assert port.sent == []
    assert len(port.reads) == 1


def test_a_send_that_raises_in_process_is_recorded_unknown_before_it_propagates():
    store = InMemoryOperationStore()
    op = transfer_op()

    def boom(o):
        raise RuntimeError("adapter died after the request left")

    with pytest.raises(RuntimeError):
        MutationExecutor(Port([boom]), store, sleep=lambda s: None).run(op)
    rec = store.get(op.request_id)
    assert (rec.status, rec.may_have_landed) == ("OUTCOME_UNKNOWN", True)
    landed = {**op.body, "id": "t1", "status": "PROCESSING"}
    port = Port(items=[landed])  # an immediate resume is not blocked by the lease
    assert MutationExecutor(port, store, sleep=lambda s: None).run(op).status is OK
    assert port.sent == []


# ---- uncertain results keep last_error --------------------------------------------------------


def test_an_unreadable_duplicate_answer_is_unknown_with_last_error_and_the_duplicate_details():
    store = InMemoryOperationStore()
    op = transfer_op()

    def dup(o):
        return OperationResult(
            request_id=o.request_id,
            status=FAILED,
            http_status=400,
            error_type="DuplicateRequest",
            error_code="request_id_duplicate",
            error_message="used",
            body={"details": {"source_id": "tr_1"}},
        )

    port = Port([dup], items=[])  # the read-back never shows the object
    result = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1).run(op)
    assert result.status is UNKNOWN
    assert result.error_code == "request_id_duplicate"
    assert result.body["details"] == {"source_id": "tr_1"}
    assert result.body["last_error"]["error_code"] == "request_id_duplicate"
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"


def test_a_held_failed_is_never_stored_raw_while_uncertain():
    completes: list[OperationResult] = []

    class Spy(InMemoryOperationStore):
        def complete(self, request_id, result):
            completes.append(result)
            return super().complete(request_id, result)

    store = Spy()
    op = transfer_op()
    port = Port([_unknown, _429, _429])
    MutationExecutor(port, store, sleep=lambda s: None, max_attempts=3).run(op)
    assert completes
    assert all(r.status is not FAILED for r in completes)
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"


# ---- records with no first-attempt time -------------------------------------------------------


def test_a_record_without_first_attempt_at_is_never_resent():
    """A record with attempts but no first_attempt_at has an unknown age."""
    from dataclasses import replace as dc_replace

    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    port = Port([_unknown, _ok])
    MutationExecutor(port, store, sleep=clock.sleep, clock=clock, max_attempts=1).run(op)
    rec = store.get(op.request_id)
    store._records[op.request_id] = dc_replace(rec, first_attempt_at=None)  # no first-attempt time
    clock.t += timedelta(days=30)
    result = MutationExecutor(port, store, sleep=clock.sleep, clock=clock, max_attempts=1).run(op)
    assert (result.status, result.error_code) == (UNKNOWN, FIRST_ATTEMPT_UNKNOWN_CODE)
    assert len(port.sent) == 1
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"


def test_a_crashed_pending_record_without_attempt_times_is_not_resent():
    from dataclasses import replace as dc_replace

    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    store.begin(op)
    rec = store.record_attempt(op.request_id)
    store._records[op.request_id] = dc_replace(rec, first_attempt_at=None, attempt_started_at=None)
    port = Port([_ok], items=[])
    result = MutationExecutor(port, store, sleep=clock.sleep, clock=clock).run(op)
    assert (result.status, result.error_code) == (UNKNOWN, FIRST_ATTEMPT_UNKNOWN_CODE)
    assert port.sent == []


def test_a_failing_final_read_back_after_a_429_keeps_last_error():
    store = InMemoryOperationStore()
    op = transfer_op()

    class RaisingRead(Port):
        def read(self, req):
            self.reads.append(req)
            if len(self.reads) <= 2:  # run 1's read-back, run 2's read-back before the resend
                return {"items": []}
            raise TransientServerError("read-back failed")

    port = RaisingRead([_unknown, _429])
    ex = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1)
    assert ex.run(op).status is UNKNOWN  # first send: unknown, then an empty read-back
    result = ex.run(op)  # empty read-back, resend gets 429, final read-back raises
    assert result.status is UNKNOWN
    assert result.body["last_error"]["error_type"] == "RateLimited"
    assert result.error_code is None
    assert len(port.sent) == 2
    assert len(port.reads) == 3


# ---- a read-back mismatch is never FAILED -----------------------------------------------------


def _other(op):
    return {**op.body, "id": "t9", "status": "PROCESSING", "transfer_amount": Decimal("99.00")}


def _assert_mismatch_unknown(result, store, op):
    """The one mismatch result shape, field by field."""
    assert result.status is UNKNOWN
    assert result.error_type == "OutcomeUnknown"
    assert result.error_code == "request_id_reused"
    assert result.http_status is None
    assert result.provider_id is None  # another object's id never reads as ours
    assert result.provider_status is None
    assert result.error_message
    assert set(result.body) == {"details", "last_error"}
    assert result.body["details"] == {"fields": ["transfer_amount"], "provider_id": "t9"}
    last = result.body["last_error"]
    assert set(last) == {"error_type", "error_code", "http_status", "error_message"}
    assert last["error_type"] == "StateConflict"
    assert last["error_code"] == "request_id_reused"
    assert last["http_status"] is None
    assert last["error_message"]
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"


def test_mismatch_after_an_unknown_send_is_unknown_and_never_resent():
    store = InMemoryOperationStore()
    op = transfer_op()
    port = Port([_unknown, _ok], items=[_other(op)])
    ex = MutationExecutor(port, store, sleep=lambda s: None)
    _assert_mismatch_unknown(ex.run(op), store, op)
    _assert_mismatch_unknown(ex.run(op), store, op)  # a later run: read back again, no send
    assert len(port.sent) == 1


def test_mismatch_on_a_crashed_record_is_unknown():
    clock = Clock()
    store = InMemoryOperationStore(now=clock)
    op = transfer_op()
    store.begin(op)
    store.record_attempt(op.request_id)
    clock.t += timedelta(seconds=DEFAULT_ATTEMPT_LEASE_S + 1)
    port = Port([_ok], items=[_other(op)])
    result = MutationExecutor(port, store, sleep=clock.sleep, clock=clock).run(op)
    _assert_mismatch_unknown(result, store, op)
    assert port.sent == []


def test_mismatch_behind_a_duplicate_request_answer_on_a_first_send_is_unknown():
    """Another store sent our request_id with another body: certain op, still not FAILED."""
    store = InMemoryOperationStore()
    op = transfer_op()

    def dup(o):
        return OperationResult(
            request_id=o.request_id,
            status=FAILED,
            http_status=400,
            error_type="DuplicateRequest",
            error_code="request_id_duplicate",
        )

    port = Port([dup], items=[_other(op)])
    result = MutationExecutor(port, store, sleep=lambda s: None).run(op)
    _assert_mismatch_unknown(result, store, op)
    assert len(port.sent) == 1


def test_a_certain_operation_keeps_its_refusal_and_its_retryable_failed():
    def refuse(o):
        return OperationResult(
            request_id=o.request_id,
            status=FAILED,
            http_status=400,
            error_type="ValidationFailed",
            error_code="invalid_argument",
        )

    store = InMemoryOperationStore()
    result = MutationExecutor(Port([refuse]), store, sleep=lambda s: None).run(transfer_op())
    assert (result.status, result.error_code) == (FAILED, "invalid_argument")
    port = Port([_429, _429])
    result = MutationExecutor(
        port, InMemoryOperationStore(), sleep=lambda s: None, max_attempts=2
    ).run(transfer_op())
    assert (result.status, result.error_code) == (FAILED, "too_many_requests")
    assert result.error().retryable
