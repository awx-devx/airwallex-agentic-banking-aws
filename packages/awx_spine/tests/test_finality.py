from collections.abc import Mapping
from typing import Any

import pytest
from awx_common import OperationResult, OperationStatus, ReadRequest
from awx_common.errors import NotFound, RateLimited
from awx_common.workflow import FinalitySpec
from awx_spine.finality import FinalityTimeout, poll_until_terminal

TRANSFER = FinalitySpec(
    read_request=lambda r: ReadRequest(f"/api/v1/transfers/{r.provider_id}"),
    status_of=lambda body: body["status"],
    terminal=frozenset({"PAID", "CANCELLED"}),
    success=frozenset({"PAID"}),
    interval_s=5,
    timeout_s=20,
)
OK = OperationResult(request_id="r", status=OperationStatus.SUCCEEDED, provider_id="t1")


class SeqPort:
    def __init__(self, seq: list[Any]) -> None:
        self.seq = seq
        self.reads: list[ReadRequest] = []

    def execute(self, op):  # pragma: no cover - not used
        raise AssertionError

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        self.reads.append(req)
        item = self.seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_polls_through_in_flight_states_until_terminal():
    clock = Clock()
    port = SeqPort(
        [
            {"status": "PROCESSING"},
            {"status": "SENT"},
            {"status": "CANCELLED", "failure": {"code": "90802"}},
        ]
    )
    status, body = poll_until_terminal(port, OK, TRANSFER, clock=clock.now, sleep=clock.sleep)
    assert status == "CANCELLED"
    assert body["failure"]["code"] == "90802"
    assert clock.sleeps == [5, 5]
    assert port.reads[0].path == "/api/v1/transfers/t1"


def test_times_out_with_last_seen_status():
    clock = Clock()
    port = SeqPort([{"status": "SENT"}] * 10)
    with pytest.raises(FinalityTimeout) as info:
        poll_until_terminal(port, OK, TRANSFER, clock=clock.now, sleep=clock.sleep)
    assert info.value.last_status == "SENT"
    assert clock.t <= TRANSFER.timeout_s


def test_retryable_read_errors_are_tolerated_others_raise():
    clock = Clock()
    port = SeqPort([RateLimited("slow"), {"status": "PAID"}])
    assert poll_until_terminal(port, OK, TRANSFER, clock=clock.now, sleep=clock.sleep)[0] == "PAID"
    port = SeqPort([NotFound("gone")])
    with pytest.raises(NotFound):
        poll_until_terminal(
            port, OK, TRANSFER, clock=clock.now, sleep=clock.sleep, not_found_grace_s=0
        )


def test_not_found_is_retried_inside_the_grace_window_only():
    clock = Clock()
    port = SeqPort([NotFound("lagging"), {"status": "PAID"}])
    assert poll_until_terminal(port, OK, TRANSFER, clock=clock.now, sleep=clock.sleep)[0] == "PAID"
    clock = Clock()
    port = SeqPort([NotFound("lagging"), NotFound("lagging"), NotFound("gone")])
    with pytest.raises(NotFound):
        poll_until_terminal(
            port, OK, TRANSFER, clock=clock.now, sleep=clock.sleep, not_found_grace_s=8
        )


def test_refuses_to_poll_a_failed_operation():
    failed = OperationResult(request_id="r", status=OperationStatus.FAILED)
    with pytest.raises(ValueError, match="cannot poll"):
        poll_until_terminal(SeqPort([]), failed, TRANSFER)
