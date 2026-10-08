"""A record left PENDING by a crash after a 429 is reconciled before any resend, and is never
resent on an endpoint that ignores request_id."""

from __future__ import annotations

import pytest
from awx_common import Operation, OperationResult, OperationStatus, ReadRequest
from awx_spine import InMemoryOperationStore, MutationExecutor
from awx_spine.testing import after_lease, make_op, transfer_op

OK = OperationStatus.SUCCEEDED
FAILED = OperationStatus.FAILED
UNKNOWN = OperationStatus.OUTCOME_UNKNOWN
USD = "USD"
WEEK = 7 * 86400


class CountingPort:
    def __init__(self, reads=None) -> None:
        self.executes: list[Operation] = []
        self.reads: list[ReadRequest] = []
        self._reads = reads or (lambda req: {"items": []})

    def execute(self, op: Operation) -> OperationResult:
        self.executes.append(op)
        return OperationResult(request_id=op.request_id, status=OK, provider_id="x")

    def read(self, req: ReadRequest):
        self.reads.append(req)
        return self._reads(req)


def rate_limited_then_crashed(store: InMemoryOperationStore, op: Operation) -> None:
    """Attempt 1 ended 429; attempt 2's send left and the process died before complete."""
    store.begin(op)
    store.record_attempt(op.request_id)
    store.complete(
        op.request_id,
        OperationResult(
            request_id=op.request_id, status=FAILED, http_status=429, error_type="RateLimited"
        ),
    )
    store.record_attempt(op.request_id)


def test_record_attempt_sets_pending():
    store = InMemoryOperationStore()
    op = transfer_op()
    rate_limited_then_crashed(store, op)
    rec = store.get(op.request_id)
    assert rec.status == "PENDING"
    assert rec.attempts == 2


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("issuing.simulate_authorization", "/api/v1/simulation/issuing/create"),
        ("deposits.simulate", "/api/v1/simulation/deposit/create"),
        ("transfers.simulate_transition", "/api/v1/simulation/transfers/t1/transition"),
        ("beneficiaries.create", "/api/v1/beneficiaries/create"),
    ],
)
def test_429_then_crash_on_a_non_deduplicating_endpoint_is_never_resent(name, path):
    op = make_op("k2", name, path, {"x": 1}, f"crash:{name}", with_request_id=False)
    store = InMemoryOperationStore()
    rate_limited_then_crashed(store, op)
    port = CountingPort()
    result = MutationExecutor(port, store, sleep=lambda s: None, clock=after_lease).run(op)
    assert result.status is UNKNOWN
    assert port.executes == []


def test_429_then_crash_on_transfers_reads_back_first():
    op = transfer_op()
    store = InMemoryOperationStore()
    rate_limited_then_crashed(store, op)
    landed = {**op.body, "id": "t1", "status": "PROCESSING"}
    port = CountingPort(reads=lambda req: {"items": [landed]})
    result = MutationExecutor(port, store, sleep=lambda s: None, clock=after_lease).run(op)
    assert result.status is OK
    assert result.provider_id == "t1"
    assert port.executes == []
    assert port.reads[0].query == {"request_id": op.request_id}


def test_429_then_crash_on_transfers_not_found_resends_same_request_id():
    op = transfer_op()
    store = InMemoryOperationStore()
    rate_limited_then_crashed(store, op)
    port = CountingPort()
    assert (
        MutationExecutor(port, store, sleep=lambda s: None, clock=after_lease).run(op).status is OK
    )
    assert len(port.reads) >= 1
    assert [o.request_id for o in port.executes] == [op.request_id]
