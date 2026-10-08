"""What StepContext.prior holds when a dispatch, a reconcile or a finality read fails.

Only a refusal at an operation's first dispatch (a SEND, or a sandbox ADVANCE, which is never
resent) leaves it absent from prior. A refusal on a later dispatch (RESEND, COMPLETE) keeps the
operation, because the first send may have landed. A reconciled object that does not match the
operation is OUTCOME_UNKNOWN request_id_reused, never a definitive FAILED. A failing finality
read keeps what earlier reads in the same check found. Each case runs through local_runner and,
where marked synth, through the synthesised state machine.
"""

from __future__ import annotations

import sys
import types
from collections import Counter
from dataclasses import replace

import pytest
from awx_common import Origin
from awx_common.errors import TenantScopeViolation
from awx_common.ops import OperationStatus
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import decode, encode
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import adapter_core, engine
from awx_spine_aws.engine import Action
from awx_spine_aws.handlers import adapter, kit_step
from awx_spine_aws.local_runner import run_kit_workflow
from awx_spine_aws.store import DynamoDbOperationStore

import toy
from aws_harness import MODULE, sfn_json
from test_asl_equivalence import Interpreter

FEE = toy.fee_op().request_id


def _recording(spec, seen: list):
    base = spec.postcondition

    def post(ctx):
        seen.append({k: tuple(v) for k, v in ctx.prior.items()})
        return base(ctx)

    return replace(spec, postcondition=post)


def _local(spec, port, store=None):
    clock = toy.Clock()
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


def _refuse_local(monkeypatch, mode: Action, exc: Exception) -> list:
    """The adapter refuses the fee's dispatch in `mode` (it re-authorises on every mode)."""
    real, modes = adapter_core.dispatch, []

    def dispatch(executor, store, op, action, result=None):
        modes.append(Action(action))
        if op.request_id == FEE and Action(action) is mode:
            raise exc
        return real(executor, store, op, action, result)

    monkeypatch.setattr(adapter_core, "dispatch", dispatch)
    return modes


def _refuse_sfn(monkeypatch, mode: str, exc: Exception) -> list:
    real, modes = adapter.handler, []

    def handler(event, context):
        payload = decode(event)
        modes.append(payload.get("mode") or "send")
        if payload["operation"]["request_id"] == FEE and (payload.get("mode") or "send") == mode:
            raise exc
        return real(event, context)

    monkeypatch.setattr(adapter, "handler", handler)
    return modes


def _sfn(spec, aws, machines, monkeypatch):
    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_supplier": spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", "pay_supplier")
    kit_step.root_spec.cache_clear()
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
    return interp, interp.execute("root", data, proposal.proposal_id)["run"]


def _fee(seen: list) -> list:
    assert len(seen) == 1
    return list(seen[0].get("fee", ()))


# --- a refusal after the first send keeps the operation in prior ---------------------------------


def test_not_authorised_on_a_resend_after_a_crash_keeps_the_fee_unknown(monkeypatch):
    seen: list = []
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))  # sent, then crashed
    modes = _refuse_local(monkeypatch, Action.RESEND, adapter.NotAuthorised("proposal is EXPIRED"))
    out = _local(_recording(toy.toy_spec(), seen), port)
    assert modes == [Action.SEND, Action.RESEND]
    assert out.state == "FAILED"
    [fee] = _fee(seen)  # never absent: the first send may have landed
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN
    assert len(port.sends) == 1 and FEE in port.created


def test_tenant_scope_refusal_at_complete_keeps_the_established_result(monkeypatch):
    seen: list = []
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    modes = _refuse_local(monkeypatch, Action.COMPLETE, TenantScopeViolation("unknown tenant"))
    out = _local(_recording(toy.toy_spec(reconcile=True), seen), port)
    assert modes == [Action.SEND, Action.COMPLETE]
    assert out.state == "FAILED"
    [fee] = _fee(seen)
    assert fee.status is OperationStatus.SUCCEEDED  # the reconciled object, not dropped
    assert fee.provider_id == port.created[FEE]["id"]


def test_a_refusal_at_the_first_send_still_leaves_the_fee_absent(monkeypatch):
    # Guard: nothing was sent, so absence means not sent.
    seen: list = []
    port = toy.ToyPort()
    _refuse_local(monkeypatch, Action.SEND, adapter.NotAuthorised("no such proposal"))
    out = _local(_recording(toy.toy_spec(), seen), port)
    assert out.state == "FAILED"
    assert _fee(seen) == []
    assert port.sends == []


@pytest.mark.synth
def test_asl_not_authorised_at_adapter_on_a_resend_after_a_crash(aws, machines, monkeypatch):
    seen: list = []
    aws.port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    modes = _refuse_sfn(monkeypatch, "resend", adapter.NotAuthorised("proposal is EXPIRED"))
    interp, final = _sfn(_recording(toy.toy_spec(), seen), aws, machines, monkeypatch)
    assert modes == ["send", "resend"]
    assert final["state"] == "FAILED"
    assert {"Crashed1Fee", "Error", "Postcondition"} <= set(interp.visited)
    [fee] = _fee(seen)
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN
    assert final["results"]["fee"][0]["status"] == "OUTCOME_UNKNOWN"


@pytest.mark.synth
def test_asl_tenant_scope_violation_at_adapter_complete(aws, machines, monkeypatch):
    seen: list = []
    aws.port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    modes = _refuse_sfn(monkeypatch, "complete", TenantScopeViolation("unknown tenant"))
    spec = _recording(toy.toy_spec(reconcile=True), seen)
    interp, final = _sfn(spec, aws, machines, monkeypatch)
    assert modes == ["send", "complete"]
    assert final["state"] == "FAILED"
    assert "Error" in interp.visited
    [fee] = _fee(seen)
    assert fee.status is OperationStatus.SUCCEEDED
    assert fee.provider_id == aws.port.created[FEE]["id"]


# --- an advance refused at its first dispatch was never sent -------------------------------------


def test_a_refused_advance_is_never_recorded_as_in_flight(monkeypatch):
    # Guard: a refused ADVANCE is a first dispatch, like a SEND.
    port = toy.ToyPort(auto_move=False)
    real = adapter_core.dispatch

    def dispatch(executor, store, op, action, result=None):
        if Action(action) is Action.ADVANCE:
            raise TenantScopeViolation("unknown tenant")
        return real(executor, store, op, action, result)

    monkeypatch.setattr(adapter_core, "dispatch", dispatch)
    clock = toy.Clock()
    out = run_kit_workflow(
        toy.toy_spec(sandbox_finality=True),
        toy.RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=port,
        store=InMemoryOperationStore(),
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.SCHEDULE,
        sandbox=True,
    )
    assert out.state == "FAILED"
    assert out.results.get("pay:sandbox", ()) == ()
    assert not [op for op in port.sends if "simulation" in op.path]


@pytest.mark.parametrize(
    ("action", "resends", "first"),
    [
        (Action.SEND, 0, True),
        (Action.ADVANCE, 0, True),
        (Action.RESEND, 1, False),
        (Action.COMPLETE, 0, False),
    ],
)
def test_only_a_first_dispatch_counts_as_never_sent(action, resends, first):
    pending = engine.Pending(toy.fee_op(), "step", action, resends=resends)
    assert engine._first(pending) is first


# --- a mismatched reconciled object is in flight, never FAILED -----------------------------------


def _assert_mismatch_unknown(fee, rec, found_id) -> None:
    """The one mismatch shape (engine.mismatch_result), field by field."""
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN
    assert fee.error_type == "OutcomeUnknown"
    assert fee.error_code == "request_id_reused"
    assert fee.http_status is None
    assert fee.provider_id is None  # another object's id never reads as ours
    assert fee.provider_status is None
    assert fee.error_message
    assert set(fee.body) == {"details", "last_error"}
    assert fee.body["details"] == {"fields": ["transfer_amount"], "provider_id": found_id}
    last = fee.body["last_error"]
    assert set(last) == {"error_type", "error_code", "http_status", "error_message"}
    assert last["error_type"] == "StateConflict"
    assert last["error_code"] == "request_id_reused"
    assert last["http_status"] is None
    assert last["error_message"]
    assert rec.status == "OUTCOME_UNKNOWN" and rec.may_have_landed


def test_a_mismatched_reconcile_is_outcome_unknown_in_prior():
    seen: list = []
    store = InMemoryOperationStore()
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    spec = _recording(toy.toy_spec(reconcile=toy.reconcile_wrong_amount), seen)
    out = _local(spec, port, store)
    assert out.state == "FAILED"
    [fee] = _fee(seen)
    _assert_mismatch_unknown(fee, store.get(FEE), port.created[FEE]["id"])
    assert len(port.sends) == 1  # nothing resent


@pytest.mark.synth
def test_asl_a_mismatched_reconcile_is_outcome_unknown(aws, machines, monkeypatch):
    seen: list = []
    aws.port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    spec = _recording(toy.toy_spec(reconcile=toy.reconcile_wrong_amount), seen)
    _, final = _sfn(spec, aws, machines, monkeypatch)
    assert final["state"] == "FAILED"
    [fee] = _fee(seen)
    _assert_mismatch_unknown(
        fee, DynamoDbOperationStore("ops", client=aws.db).get(FEE), aws.port.created[FEE]["id"]
    )
    assert len(aws.port.sends) == 1


def test_an_object_in_its_own_failure_state_stays_failed():
    # Guard: CANCELLED is the provider's own definitive answer (provider_failure_state).
    op = toy.fee_op()
    step = toy.toy_spec().steps[1]
    found = {"id": "tr_1", "status": "CANCELLED", **op.body}
    res = engine._settle_found(op, step, found)
    assert res.status is OperationStatus.FAILED
    assert res.error_code == "provider_failure_state"


# --- a failing finality read keeps the earlier reads' findings -----------------------------------


def test_a_non_transient_finality_read_keeps_earlier_terminal_statuses():
    def two(ctx):
        [op] = toy.transfer_ops("pay")(ctx)
        rid = op.request_id + "-b"
        return op, replace(op, request_id=rid, body={**op.body, "request_id": rid})

    base = toy.transfer_finality()
    second: list = []

    def read_request(result):
        if result.request_id.endswith("-b"):
            second.append(result.request_id)
            raise ValueError("kit bug: no such read")
        return base.read_request(result)

    spec = toy.toy_spec(pay_build=two)
    pay = replace(spec.steps[1], finality=replace(base, read_request=read_request))
    spec = replace(spec, steps=(spec.steps[0], pay))
    out = _local(spec, toy.ToyPort())
    assert second  # the second read failed in the same check
    assert out.state == "FAILED"
    assert "finality read ValueError" in out.detail
    first = out.results["pay"][0]
    assert first.provider_status == "PAID"  # what the first read found is kept
