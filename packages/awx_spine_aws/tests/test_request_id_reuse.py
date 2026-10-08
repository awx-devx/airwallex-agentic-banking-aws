"""A RequestIdReuse is recorded in StepContext.prior, never left absent.

The request_id is bound to a different body in the operation store, and that body may be in
flight or landed, so the operation is OUTCOME_UNKNOWN with body["last_error"] naming the reuse
and the stored record's status. A tenant scope refusal, by contrast, was never dispatched and
stays absent.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace

import pytest
from awx_common import Money, Origin
from awx_common.ops import OperationResult, OperationStatus
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import encode
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.handlers import kit_step
from awx_spine_aws.local_runner import run_kit_workflow
from awx_spine_aws.store import DynamoDbOperationStore

import toy
from aws_harness import MODULE, sfn_json
from test_asl_equivalence import Interpreter


def _recording(spec, seen: list):
    """The toy spec, its postcondition recording the full prior it sees."""
    base = spec.postcondition

    def post(ctx):
        seen.append({k: tuple(v) for k, v in ctx.prior.items()})
        return base(ctx)

    return replace(spec, postcondition=post)


def _seed_other_body(store) -> str:
    """An earlier run bound the fee's request_id to another amount, and its outcome is unknown."""
    op = toy.fee_op()
    other = replace(op, body={**op.body, "transfer_amount": Money.of("9.99", "AUD").amount})
    store.begin(other)
    store.record_attempt(other.request_id)
    store.complete(
        other.request_id,
        OperationResult(
            request_id=other.request_id,
            status=OperationStatus.OUTCOME_UNKNOWN,
            error_type="OutcomeUnknown",
            error_message="earlier run: timeout after submit",
        ),
    )
    return op.request_id


def _assert_reuse_in_prior(seen: list, rid: str) -> None:
    assert len(seen) == 1
    [fee] = seen[0]["fee"]
    assert fee.request_id == rid
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN  # never a definitive FAILED
    last = fee.body["last_error"]
    assert last["error_type"] == "RequestIdReuse"
    assert last["stored_status"] == "OUTCOME_UNKNOWN"
    assert "pay" not in seen[0]  # the run stopped before the next step


LAST_ERROR_KEYS = {"error_type", "error_code", "http_status", "error_message", "stored_status"}


def _assert_last_error_shape(seen: list) -> None:
    """The one last_error shape (as awx_spine.executor writes it), plus stored_status."""
    last = seen[0]["fee"][0].body["last_error"]
    assert set(last) == LAST_ERROR_KEYS
    assert last["error_type"] == "RequestIdReuse"
    assert last["error_code"] == "request_id_bound_to_another_body"
    assert last["http_status"] is None
    assert "RequestIdReuse" in last["error_message"]
    assert last["stored_status"] == "OUTCOME_UNKNOWN"


def _local(spec, port, store=None, *, clock=None):
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
    )


def test_a_request_id_reuse_is_in_prior_as_outcome_unknown():
    store = InMemoryOperationStore()
    rid = _seed_other_body(store)
    seen: list = []
    port = toy.ToyPort()
    out = _local(_recording(toy.toy_spec(), seen), port, store)
    assert out.state == "FAILED"
    assert "RequestIdReuse" in out.detail
    assert port.sends == []  # nothing sent under the reused request_id
    _assert_reuse_in_prior(seen, rid)
    _assert_last_error_shape(seen)
    assert store.get(rid).body_hash != ""  # the earlier record is untouched


@pytest.mark.synth
def test_asl_a_request_id_reuse_is_in_prior_as_outcome_unknown(aws, machines, monkeypatch):
    rid = _seed_other_body(DynamoDbOperationStore("ops", client=aws.db))
    seen: list = []
    spec = _recording(toy.toy_spec(), seen)
    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_supplier": spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", "pay_supplier")
    kit_step.root_spec.cache_clear()
    aws.port = toy.ToyPort()
    lenv = kit_step.LambdaEnv()
    proposal, decision = engine.propose(
        spec,
        (),
        toy.RAW,
        proposed_by="requester-sub",
        tenant=lenv.resolve_tenant("platform"),
        env=lenv,
        origin=Origin.SCHEDULE,
    )
    data = sfn_json(encode(engine.execution_input(proposal, decision, toy.RAW, None)))
    interp = Interpreter(machines["pay_supplier"], toy.no_decision, aws)
    final = interp.execute("root", data, proposal.proposal_id)["run"]
    assert final["state"] == "FAILED"
    assert {"Adapter1Fee", "Error", "AfterStop", "Postcondition"} <= set(interp.visited)
    assert "Crashed1Fee" not in interp.visited  # a refusal, routed to Error, never resent
    assert aws.port.sends == []
    _assert_reuse_in_prior(seen, rid)
    _assert_last_error_shape(seen)  # the deployed path builds the same shape


def test_a_tenant_scope_refusal_stays_absent():
    spec = toy.toy_spec(fee_build=toy.transfer_ops("fee", bind=False, on_behalf_of="acct_X"))
    seen: list = []
    out = _local(_recording(spec, seen), toy.ToyPort())
    assert out.state == "FAILED"
    assert "TenantScopeViolation" in out.detail
    assert seen[0].get("fee", ()) == ()  # never dispatched, so no result
