"""poll_until_terminal with a FinalitySpec select: the operation's row is picked out of a list
read, and a read without it is a non-terminal poll."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from awx_common import OperationResult, OperationStatus, ReadRequest
from awx_common.workflow import FinalitySpec
from awx_spine.finality import poll_until_terminal

OK = OperationStatus.SUCCEEDED
T0 = datetime(2026, 10, 6, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += timedelta(seconds=s)


class SelectSpec(FinalitySpec):
    """A FinalitySpec carrying a select, set the same way on any FinalitySpec version."""

    def __init__(self, select, **kw: Any) -> None:
        super().__init__(**kw)
        object.__setattr__(self, "select", select)


def test_poll_until_terminal_applies_select_and_waits_while_it_is_none():
    bodies = [
        {"items": []},
        {"items": [{"id": "other", "status": "PAID"}]},
        {"items": [{"id": "t1", "status": "PROCESSING"}, {"id": "x", "status": "PAID"}]},
        {"items": [{"id": "t1", "status": "PAID"}]},
    ]

    class SeqPort:
        def read(self, req):
            return bodies.pop(0)

    def mine(body, result):
        return next((i for i in body["items"] if i["id"] == result.provider_id), None)

    spec = SelectSpec(
        mine,
        read_request=lambda r: ReadRequest("/api/v1/transfers", {"request_id": r.request_id}),
        status_of=lambda body: body["status"],
        terminal=frozenset({"PAID", "CANCELLED"}),
        success=frozenset({"PAID"}),
        interval_s=1,
        timeout_s=10,
    )
    clock = Clock()
    status, body = poll_until_terminal(
        SeqPort(),
        OperationResult(request_id="r", status=OK, provider_id="t1"),
        spec,
        clock=lambda: (clock.t - T0).total_seconds(),
        sleep=clock.sleep,
    )
    assert (status, body["id"]) == ("PAID", "t1")
    assert bodies == []
