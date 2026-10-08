"""A raise in kit finality code fails the run closed and keeps what the poll found.

select, status_of, sandbox_advance and the check on an advance can each raise after a read
succeeded; the run fails closed with the results the same poll already established, and no
advance goes out.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from awx_common import Origin
from awx_common.ops import OperationStatus
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import run_kit_workflow

import toy


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


def _two_pay_ops(ctx):
    [op] = toy.transfer_ops("pay")(ctx)
    rid = op.request_id + "-b"
    return op, replace(op, request_id=rid, body={**op.body, "request_id": rid})


@pytest.mark.parametrize("where", ["select", "status_of"])
def test_a_raise_after_the_read_keeps_what_the_poll_found(where):
    base = toy.transfer_finality()

    def boom_for_b(result):
        if result.request_id.endswith("-b"):
            raise ValueError(f"kit bug in {where}")

    if where == "select":

        def select(body, result):
            boom_for_b(result)
            return body

        fin = replace(base, select=select)
    else:
        seen_b: list = []

        def read_request(result):
            seen_b.append(result.request_id.endswith("-b"))
            return base.read_request(result)

        def status_of(body):
            if seen_b and seen_b[-1]:
                raise ValueError("kit bug in status_of")
            return base.status_of(body)

        fin = replace(base, read_request=read_request, status_of=status_of)
    spec = toy.toy_spec(pay_build=_two_pay_ops)
    spec = replace(spec, steps=(spec.steps[0], replace(spec.steps[1], finality=fin)))
    out = _local(spec, toy.ToyPort())
    assert out.state == "FAILED"
    assert "finality ValueError" in out.detail
    assert out.results["pay"][0].provider_status == "PAID"  # the first op's finding is kept


def test_a_raise_in_sandbox_advance_keeps_what_the_poll_found():
    base = toy.transfer_finality(sandbox=True)

    def advance(op, result, body, now, attempt):
        if result.request_id.endswith("-b"):
            raise ValueError("kit bug in sandbox_advance")
        return base.sandbox_advance(op, result, body, now, attempt)

    spec = toy.toy_spec(pay_build=_two_pay_ops, sandbox_finality=True)
    fin = replace(base, sandbox_advance=advance)
    spec = replace(spec, steps=(spec.steps[0], replace(spec.steps[1], finality=fin)))
    port = toy.ToyPort(auto_move=False)
    out = _local(spec, port)
    assert out.state == "FAILED"
    assert "finality ValueError" in out.detail
    assert [r.status for r in out.results["pay"]] == [OperationStatus.SUCCEEDED] * 2
    assert out.results.get("pay:sandbox", ()) == ()  # the advance never went out
