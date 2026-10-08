"""Finality polling survives transient read errors, and an error after a send keeps the
step's results for the postcondition.

Each case runs through local_runner and, where marked, through the synthesised KitWorkflow ASL
interpreted against the real Lambda handlers (test_asl_equivalence's interpreter).
"""

from __future__ import annotations

import sys
import types

import pytest
from awx_common import Origin
from awx_common.errors import NotFound, RateLimited, TransientServerError, ValidationFailed
from awx_common.ops import ReadRequest
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import encode
from awx_common.workflow import KitWorkflowSpec, StepSpec
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.handlers import kit_step
from awx_spine_aws.local_runner import run_kit_workflow

import toy
from aws_harness import MODULE, sfn_json
from test_asl_equivalence import Interpreter

TRANSFER_BY_ID = "/api/v1/transfers/"


def _err(cls, status):
    return cls(f"HTTP {status}", http_status=status)


class FaultyPort(toy.ToyPort):
    """Finality reads (GET a transfer by id) raise the scripted errors first, in order."""

    def __init__(self, faults=(), *, forever=None, **kw):
        super().__init__(**kw)
        self.faults = list(faults)
        self.forever = forever
        self.finality_reads = 0

    def read(self, req: ReadRequest):
        if req.path.startswith(TRANSFER_BY_ID):
            self.finality_reads += 1
            if self.faults:
                raise self.faults.pop(0)
            if self.forever is not None:
                raise self.forever()
        return super().read(req)


def _spec(calls: list, *, read_path=None) -> KitWorkflowSpec:
    """The toy kit, its postcondition recording the `prior` it sees."""
    spec = toy.toy_spec()
    base_post = spec.postcondition

    def post(ctx):
        calls.append({k: [r.status.value for r in v] for k, v in ctx.prior.items()})
        return base_post(ctx)

    steps = spec.steps
    if read_path is not None:  # a finality read the kit is not allowed to make
        fin = steps[1].finality
        bad = type(fin)(**{**fin.__dict__, "read_request": lambda r: ReadRequest(read_path)})
        steps = (steps[0], StepSpec("pay", steps[1].build, bad))
    return type(spec)(**{**spec.__dict__, "steps": steps, "postcondition": post})


def _local(spec, port):
    clock = toy.Clock()
    out = run_kit_workflow(
        spec,
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
    )
    return out


def _transient_faults():
    """Poll 1: four 429s (the read and its three retries) leave it not final; poll 2: a 500
    (retried) then a 404 under lag (not retried) leave it not final; poll 3 reads the object."""
    return [
        *[_err(RateLimited, 429) for _ in range(engine.READ_RETRIES + 1)],
        _err(TransientServerError, 500),
        NotFound("Transfer not found", http_status=404),  # list lag
    ]


# --- local_runner ---------------------------------------------------------------------------


def test_transient_finality_reads_poll_again_and_the_run_succeeds():
    calls: list = []
    port = FaultyPort(_transient_faults())
    out = _local(_spec(calls), port)
    assert out.state == "SUCCEEDED", out.detail
    assert port.finality_reads == 7  # 4 x 429, a 500, a 404 under lag, then the object
    assert out.results["pay"][0].provider_status == "PAID"


def test_reads_failing_past_the_deadline_time_out_naming_the_read_failure():
    calls: list = []
    spec = _spec(calls)
    fin = spec.steps[1].finality
    short = type(fin)(**{**fin.__dict__, "timeout_s": 20})
    spec = type(spec)(
        **{**spec.__dict__, "steps": (spec.steps[0], StepSpec("pay", spec.steps[1].build, short))}
    )
    port = FaultyPort(forever=lambda: _err(RateLimited, 429))
    out = _local(spec, port)
    assert out.state == "FAILED"
    assert "not final by its finality timeout" in out.detail
    assert "RateLimited" in out.detail
    assert "outcome is unknown" in out.detail
    assert calls == [{"fee": ["SUCCEEDED"], "pay": ["SUCCEEDED"]}]  # money still in flight


def test_a_not_found_past_the_lag_grace_fails_closed():
    calls: list = []
    port = FaultyPort(forever=lambda: NotFound("Transfer not found", http_status=404))
    out = _local(_spec(calls), port)
    assert out.state == "FAILED"
    assert "NotFound" in out.detail
    assert "finality timeout" not in out.detail
    assert port.finality_reads <= engine.NOT_FOUND_GRACE_S // 5 + 2


def test_a_tenant_scope_violation_on_a_finality_read_fails_at_once():
    calls: list = []
    port = FaultyPort()
    out = _local(_spec(calls, read_path="/api/v1/beneficiaries/x"), port)
    assert out.state == "FAILED"
    assert "TenantScopeViolation" in out.detail
    assert port.finality_reads == 0


def test_an_error_after_a_send_keeps_the_steps_results_for_the_postcondition():
    """The pay transfer moved, then a finality read fails closed; the
    postcondition must see the pay result, never an empty prior."""
    calls: list = []
    port = FaultyPort([ValidationFailed("bad read", http_status=400)])
    out = _local(_spec(calls), port)
    assert out.state == "FAILED"
    assert "ValidationFailed" in out.detail
    assert calls == [{"fee": ["SUCCEEDED"], "pay": ["SUCCEEDED"]}]
    assert [r.status.value for r in out.results["pay"]] == ["SUCCEEDED"]


# --- the synthesised state machine ----------------------------------------------------------


def _sfn(aws, machines, monkeypatch, spec, port):
    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_supplier": spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", "pay_supplier")
    kit_step.root_spec.cache_clear()
    aws.port = port
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
    return final, interp


@pytest.mark.synth
def test_asl_transient_finality_reads_loop_like_local_runner(aws, machines, monkeypatch):
    sfn_calls: list = []
    port = FaultyPort(_transient_faults())
    final, interp = _sfn(aws, machines, monkeypatch, _spec(sfn_calls), port)
    local_calls: list = []
    local = _local(_spec(local_calls), FaultyPort(_transient_faults()))
    assert final["state"] == local.state == "SUCCEEDED", final["detail"]
    assert port.finality_reads == 7
    assert interp.visited.count("FinalityCheck2Pay") == 3
    assert sfn_calls == local_calls


@pytest.mark.synth
def test_asl_error_after_a_send_keeps_the_steps_results(aws, machines, monkeypatch):
    sfn_calls: list = []
    port = FaultyPort([ValidationFailed("bad read", http_status=400)])
    final, interp = _sfn(aws, machines, monkeypatch, _spec(sfn_calls), port)
    local_calls: list = []
    local = _local(_spec(local_calls), FaultyPort([ValidationFailed("bad read", http_status=400)]))
    assert final["state"] == local.state == "FAILED"
    assert "Postcondition" in interp.visited
    assert sfn_calls == local_calls == [{"fee": ["SUCCEEDED"], "pay": ["SUCCEEDED"]}]


# --- every dispatched operation is in prior ------------------------------------------------

DISPATCHED_UNKNOWN = [{"fee": ["SUCCEEDED"], "pay": ["OUTCOME_UNKNOWN"]}]


def test_a_failure_after_the_adapter_returned_keeps_the_op_as_unknown(monkeypatch):
    """The pay send left and the adapter answered, then the runner failed handling the answer:
    the pay op is in prior as OUTCOME_UNKNOWN (may have landed), never absent."""
    real = engine.result
    state = {"n": 0}

    def flaky(root, run, outcome, env):
        state["n"] += 1
        if state["n"] == 2:  # the pay step's result
            raise RuntimeError("the Result task's Lambda died")
        return real(root, run, outcome, env)

    monkeypatch.setattr(engine, "result", flaky)
    calls: list = []
    port = FaultyPort()
    out = _local(_spec(calls), port)
    assert out.state == "FAILED"
    assert calls == DISPATCHED_UNKNOWN
    assert len([o for o in port.sends if o.body["reference"] == "poc-s2-pay"]) == 1


def test_an_op_refused_before_sending_is_absent_from_prior():
    calls: list = []
    spec = _spec(calls)
    unbound = toy.transfer_ops("pay", bind=False, on_behalf_of="acct_X")
    spec = type(spec)(
        **{
            **spec.__dict__,
            "steps": (spec.steps[0], StepSpec("pay", unbound, spec.steps[1].finality)),
        }
    )
    out = _local(spec, FaultyPort())
    assert out.state == "FAILED"
    assert calls == [{"fee": ["SUCCEEDED"]}]  # pay was never dispatched, so it is absent


@pytest.mark.synth
def test_asl_a_dead_result_lambda_keeps_the_op_as_unknown(aws, machines, monkeypatch):
    sfn_calls: list = []
    module = types.ModuleType(MODULE)
    spec = _spec(sfn_calls)
    module.SPECS = {"pay_supplier": spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", "pay_supplier")
    kit_step.root_spec.cache_clear()
    aws.port = FaultyPort()
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
    interp = Interpreter(machines["pay_supplier"], toy.no_decision, aws, frozenset({"Result2Pay"}))
    final = interp.execute("root", data, proposal.proposal_id)["run"]
    assert final["state"] == "FAILED"
    assert {"Error", "AfterStop", "Postcondition"} <= set(interp.visited)
    assert sfn_calls == DISPATCHED_UNKNOWN


# --- FinalitySpec.select picks this operation's row out of a list read -----------------------


class ListPort(FaultyPort):
    """GET /api/v1/transfers returns every transfer plus a CANCELLED decoy listed first; the
    pay transfer is missing from the first list read (lag) and is PAID from the third."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.list_reads = 0

    def read(self, req: ReadRequest):
        if req.path == "/api/v1/transfers" and not req.query:
            self.list_reads += 1
            rows = [{"id": "decoy", "status": "CANCELLED"}]
            for obj in self.created.values():
                if self.list_reads >= 3:
                    obj["status"] = "PAID"
                if obj.get("reference") == "poc-s2-pay" and self.list_reads == 1:
                    continue  # not visible yet
                rows.append(self._public(obj))
            return {"items": rows}
        return super().read(req)


def _list_spec(calls: list, *, select: bool = True) -> KitWorkflowSpec:
    spec = _spec(calls)
    fin = spec.steps[1].finality

    def pick(body, result):
        return next((r for r in body["items"] if r["id"] == result.provider_id), None)

    listed = type(fin)(
        **{
            **fin.__dict__,
            "read_request": lambda r: ReadRequest("/api/v1/transfers"),
            # without select, a list finality can only assume one row: here the decoy
            "status_of": (lambda b: b["status"]) if select else (lambda b: b["items"][0]["status"]),
            "select": pick if select else None,
        }
    )
    return type(spec)(
        **{**spec.__dict__, "steps": (spec.steps[0], StepSpec("pay", spec.steps[1].build, listed))}
    )


def test_select_picks_this_operations_row_from_a_two_row_list():
    calls: list = []
    port = ListPort()
    out = _local(_list_spec(calls), port)
    assert out.state == "SUCCEEDED", out.detail
    assert out.results["pay"][0].provider_status == "PAID"
    assert port.list_reads == 3  # missing (select None), still SCHEDULED, then PAID


def test_without_select_a_list_finality_reads_the_wrong_row():
    out = _local(_list_spec([], select=False), ListPort())
    assert out.state == "FAILED"
    assert "ended CANCELLED" in out.detail  # the decoy, not this payment


def test_select_returning_none_until_the_deadline_times_out_saying_so():
    calls: list = []
    spec = _list_spec(calls)
    fin = spec.steps[1].finality
    never = type(fin)(**{**fin.__dict__, "select": lambda body, result: None, "timeout_s": 20})
    spec = type(spec)(
        **{**spec.__dict__, "steps": (spec.steps[0], StepSpec("pay", spec.steps[1].build, never))}
    )
    out = _local(spec, ListPort())
    assert out.state == "FAILED"
    assert "not in the read yet" in out.detail
    assert calls == [{"fee": ["SUCCEEDED"], "pay": ["SUCCEEDED"]}]


@pytest.mark.synth
def test_asl_select_picks_the_same_row_as_local_runner(aws, machines, monkeypatch):
    sfn_calls: list = []
    port = ListPort()
    final, interp = _sfn(aws, machines, monkeypatch, _list_spec(sfn_calls), port)
    local_calls: list = []
    local = _local(_list_spec(local_calls), ListPort())
    assert final["state"] == local.state == "SUCCEEDED", final["detail"]
    assert port.list_reads == 3
    assert interp.visited.count("FinalityCheck2Pay") == 3
    assert sfn_calls == local_calls
