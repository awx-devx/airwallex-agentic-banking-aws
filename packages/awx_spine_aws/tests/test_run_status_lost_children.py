"""A run that lost a child execution stays live in run_status until the child is over.

A child that fails as an execution (the state machine's StartChild catches it as ChildLost)
may still be running, so the parent records it in lost_children, on the proposal row and in
the audit, and run_status keeps the root RUNNING until that child reads terminal or the child
timeout has passed. run_status also refuses another tenant's proposal.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from awx_common import Origin
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import encode
from awx_common.workflow import RUN_RUNNING
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine, local_runner
from awx_spine_aws.handlers import kit_step
from awx_spine_aws.local_runner import RunRegistry, run_kit_workflow
from awx_spine_aws.repos import ProposalRepo

import toy
from aws_harness import MODULE, sfn_json
from test_asl_equivalence import Interpreter, TaskFailed

RAW = {"payees": ["ben_1", "ben_2", "ben_3"], "amount": "10.00"}


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


PREFIX = "arn:aws:states:ap-southeast-2:111122223333:execution:awx-k3-"


CFG = {
    "prefix": PREFIX,
    "actions": ["pay_many"],
    "adapter_timeout_s": 135,
    "margin_s": 60,
    "child_timeout_s": {"pay_many": 5000, "pay_many-child": 0},
}


def _fan(clock, runs=None, *, lose: str | None = None, monkeypatch=None, audits=None):
    if lose is not None:
        real = local_runner._run_body

        def body(ctx, execution, tenant):
            if execution.get("spec_path") and execution["raw_input"].get("payee") == lose:
                raise RuntimeError("the child execution failed as an execution")
            return real(ctx, execution, tenant)

        monkeypatch.setattr(local_runner, "_run_body", body)
    return run_kit_workflow(
        toy.fan_out_spec(),
        RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=toy.ToyPort(),
        store=InMemoryOperationStore(),
        decide=toy.approve_by("approver"),
        now=clock.now,
        sleep=clock.sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.AGENT,
        runs=runs,
        audit=audits.append if audits is not None else None,
    )


# --- the lost child, at local_runner level -------------------------------------------------------


def test_a_lost_child_is_recorded_and_the_audit_marks_it(monkeypatch):
    audits: list = []
    out = _fan(toy.Clock(), lose="ben_2", monkeypatch=monkeypatch, audits=audits)
    assert out.state == "FAILED"
    entry = audits[-1]
    assert entry["child_states"] == ["SUCCEEDED", "FAILED", "SUCCEEDED"]
    [lost] = entry["lost_children"]
    assert lost and isinstance(lost, str)
    assert len(out.child_outcomes) == 2  # the lost child has no outcome


def test_no_lost_children_when_every_child_ran():
    audits: list = []
    out = _fan(toy.Clock(), audits=audits)
    assert out.state == "SUCCEEDED"
    assert audits[-1]["lost_children"] == []


def test_children_done_names_the_lost_child_by_its_proposal_id():
    child = {"proposal": {"proposal_id": "child-1"}}
    run = engine.Run(
        spec_path=(),
        proposal=toy_proposal(),
        decision=engine.PolicyDecision(toy_proposal().risk_tier, ()),
        tenant=TenantContext("platform"),
        raw_input={},
        approval="APPROVED",
        children=(child,),
    )
    done = engine.children_done(run, [None])
    assert done.lost_children == ("child-1",)
    assert engine.from_payload(engine.to_payload(done)).lost_children == ("child-1",)


def toy_proposal():
    clock = toy.Clock()
    out = run_kit_workflow(
        toy.toy_spec(),
        toy.RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=toy.ToyPort(),
        store=InMemoryOperationStore(),
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        origin=Origin.SCHEDULE,
    )
    return out.proposal


# --- the lost child, through the state machine ---------------------------------------------------


@pytest.mark.synth
def test_asl_a_lost_child_is_on_the_proposal_row_and_in_the_audit(aws, machines, monkeypatch):
    spec = toy.fan_out_spec()
    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_many": spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", "pay_many")
    kit_step.root_spec.cache_clear()
    aws.port = toy.ToyPort()
    lenv = kit_step.LambdaEnv()
    proposal, decision = engine.propose(
        spec,
        (),
        RAW,
        proposed_by="requester-sub",
        tenant=lenv.resolve_tenant("platform"),
        env=lenv,
        origin=Origin.SCHEDULE,
    )
    data = sfn_json(encode(engine.execution_input(proposal, decision, RAW, None)))
    interp = Interpreter(machines["pay_many"], toy.approve_by("approver"), aws)
    real, calls = interp.execute, []

    def execute(which, data, name):
        if which == "child":
            calls.append(name)
            if len(calls) == 2:  # a .sync task failure while that child may still run
                raise TaskFailed("States.TaskFailed", "the StartExecution.sync poll failed")
        return real(which, data, name)

    interp.execute = execute
    final = real("root", data, proposal.proposal_id)["run"]
    assert "ChildLost" in interp.visited and "ChildrenDone" in interp.visited
    assert final["lost_children"] == [calls[1]]
    row = ProposalRepo("proposals", client=aws.db).get(proposal.proposal_id)
    assert row.lost_children == (calls[1],)
    item = aws.db.get_item(
        TableName="audit", Key={"proposal_id": {"S": proposal.proposal_id}, "at": {"S": "run"}}
    )["Item"]
    assert json.loads(item["doc"]["S"])["lost_children"] == [calls[1]]


# --- the lost child, in the deployed run_status --------------------------------------------------


class _Stored:
    def __init__(self, action="pay_many", parent=None, tenant="platform", lost=(), outcome=None):
        self.proposal = types.SimpleNamespace(kit="k3", action=action)
        self.parent_proposal_id = parent
        self.tenant_id = tenant
        self.lost_children = tuple(lost)
        self.outcome = outcome


class _Repo:
    def __init__(self, rows):
        self.rows = rows

    def get(self, pid):
        return self.rows.get(pid)


class _Sfn:
    def __init__(self, known):
        self.known, self.described = known, []

    def describe_execution(self, executionArn):
        self.described.append(executionArn)
        got = self.known.get(executionArn)
        if got is None:
            err = Exception("missing")
            err.response = {"Error": {"Code": "ExecutionDoesNotExist"}}
            raise err
        return got


def _ago(s):
    return NOW - timedelta(seconds=s)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWX_RUN_STATUS", json.dumps(CFG))
    rows = {
        "root": _Stored(lost=("c1",)),
        "c1": _Stored(parent="root"),
        "clean": _Stored(outcome="SUCCEEDED"),
        "failed": _Stored(outcome="FAILED"),  # a failed ChildrenDone: no lost child recorded
        "no-outcome": _Stored(),  # Audit never wrote the outcome
        "other-tenant": _Stored(tenant="tenant-b"),
    }
    known: dict = {}
    sfn = _Sfn(known)
    monkeypatch.setattr(kit_step, "boto_client", lambda name: sfn)
    monkeypatch.setattr(kit_step, "_repo", lambda cls, var: _Repo(rows))
    lenv = kit_step.LambdaEnv()
    monkeypatch.setattr(lenv, "now", lambda: NOW)
    return lenv, known, sfn


ROOT = PREFIX + "pay_many:root"


CHILD = PREFIX + "pay_many-child:c1"


def test_a_root_that_lost_a_running_child_reads_running(env):
    lenv, known, sfn = env
    known[ROOT] = {"status": "SUCCEEDED", "stopDate": _ago(200)}
    known[CHILD] = {"status": "RUNNING"}
    assert lenv.run_status("k3", "pay_many", "root", "platform") == RUN_RUNNING
    assert sfn.described == [ROOT, CHILD]


def test_a_root_whose_lost_child_is_over_reads_terminal(env):
    lenv, known, _ = env
    known[ROOT] = {"status": "SUCCEEDED", "stopDate": _ago(200)}
    known[CHILD] = {"status": "TIMED_OUT", "stopDate": _ago(300)}
    assert lenv.run_status("k3", "pay_many", "root", "platform") == "SUCCEEDED"


def test_a_root_whose_lost_child_stopped_recently_reads_running(env):
    lenv, known, _ = env
    known[ROOT] = {"status": "SUCCEEDED", "stopDate": _ago(200)}
    known[CHILD] = {"status": "ABORTED", "stopDate": _ago(30)}  # its adapter may still run
    assert lenv.run_status("k3", "pay_many", "root", "platform") == RUN_RUNNING


def test_a_root_whose_lost_child_cannot_be_described_waits_the_child_timeout(env):
    lenv, known, _ = env
    known[ROOT] = {"status": "SUCCEEDED", "stopDate": _ago(200)}  # no child execution known
    assert lenv.run_status("k3", "pay_many", "root", "platform") == RUN_RUNNING
    known[ROOT] = {"status": "SUCCEEDED", "stopDate": _ago(195 + 5000)}
    assert lenv.run_status("k3", "pay_many", "root", "platform") == "SUCCEEDED"


def test_a_clean_succeeded_root_waits_only_the_adapter(env):
    lenv, known, sfn = env
    known[PREFIX + "pay_many:clean"] = {"status": "SUCCEEDED", "stopDate": _ago(200)}
    assert lenv.run_status("k3", "pay_many", "clean", "platform") == "SUCCEEDED"
    assert sfn.described == [PREFIX + "pay_many:clean"]  # no child described


@pytest.mark.parametrize("pid", ["failed", "no-outcome"])
def test_a_root_without_a_succeeded_outcome_waits_the_child_timeout(env, pid):
    """A SUCCEEDED execution of an action with children is terminal
    early only with outcome SUCCEEDED and no lost child; anything else waits the child
    timeout from stopDate, whether or not a lost child was recorded."""
    lenv, known, sfn = env
    known[PREFIX + f"pay_many:{pid}"] = {"status": "SUCCEEDED", "stopDate": _ago(200)}
    assert lenv.run_status("k3", "pay_many", pid, "platform") == RUN_RUNNING
    known[PREFIX + f"pay_many:{pid}"] = {"status": "SUCCEEDED", "stopDate": _ago(195 + 5000)}
    assert lenv.run_status("k3", "pay_many", pid, "platform") == "SUCCEEDED"
    assert all("-child:" not in a for a in sfn.described)  # no child to describe


def test_run_status_needs_the_callers_tenant(env):
    lenv, known, _ = env
    known[PREFIX + "pay_many:clean"] = {"status": "RUNNING"}
    with pytest.raises(ValueError, match="tenant"):
        lenv.run_status("k3", "pay_many", "clean", "")
    with pytest.raises(ValueError, match="tenant"):
        RunRegistry().status("k3", "pay_many", "x", "")


def test_one_now_per_run_status_call(env, monkeypatch):
    """The recursion uses the time of the call, read once."""
    lenv, known, _ = env
    calls = []
    monkeypatch.setattr(lenv, "now", lambda: calls.append(1) or NOW)
    known[ROOT] = {"status": "SUCCEEDED", "stopDate": _ago(200)}
    known[CHILD] = {"status": "TIMED_OUT", "stopDate": _ago(300)}
    assert lenv.run_status("k3", "pay_many", "root", "platform") == "SUCCEEDED"
    assert len(calls) == 1


# --- another tenant's proposal -------------------------------------------------------------------


def test_the_deployed_run_status_refuses_another_tenants_proposal(env):
    lenv, known, sfn = env
    known[PREFIX + "pay_many:other-tenant"] = {"status": "RUNNING"}
    with pytest.raises(ValueError, match="another tenant"):
        lenv.run_status("k3", "pay_many", "other-tenant", "platform")
    assert sfn.described == []  # refused before any describe
    assert lenv.run_status("k3", "pay_many", "other-tenant", "tenant-b") == RUN_RUNNING


def test_a_policy_in_one_tenant_cannot_read_another_tenants_run():
    runs = RunRegistry()
    runs.set("k3", "pay_supplier", "pid-b", RUN_RUNNING, "tenant-b")
    clock = toy.Clock()
    seen: list = []
    spec = toy.toy_spec(action="release_hold")
    base = spec.policy

    def policy(intent, ctx):
        if ctx.proposal_id is not None:
            seen.append(ctx.run_status("pid-b", "pay_supplier"))
        return base(intent, ctx)

    out = run_kit_workflow(
        replace(spec, policy=policy),
        toy.RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=toy.ToyPort(),
        store=InMemoryOperationStore(),
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        origin=Origin.SCHEDULE,
        runs=runs,
    )
    assert seen == [] and "another tenant" in out.detail  # refused: fails closed


# --- names ---------------------------------------------------------------------------------------


@pytest.mark.synth
def test_child_machines_are_named_and_timeouts_keyed_per_machine(monkeypatch):
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from infra.app import build_app
    from infra.constructs.kit_workflow import KitWorkflow, run_timeout
    from infra.kit_manifest import parse_workflow

    spec = toy.fan_out_spec()
    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_many": spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    app = cdk.App(context={"account": "111122223333", "region": "ap-southeast-2"})
    build_app(app)
    stack = cdk.Stack(
        app, "ChildNames", env=cdk.Environment(account="111122223333", region="ap-southeast-2")
    )
    m = parse_workflow(spec)
    wf = KitWorkflow(
        stack,
        "Wf",
        spine=app.node.find_child("AwxSpine"),
        kit="k3",
        action="pay_many",
        spec_module=MODULE,
        workflow=m,
        allow_stand_in=True,
    )
    t = Template.from_stack(stack).to_json()["Resources"]
    names = sorted(
        r["Properties"]["StateMachineName"]
        for r in t.values()
        if r["Type"] == "AWS::StepFunctions::StateMachine"
    )
    assert names == ["awx-k3-pay_many", "awx-k3-pay_many-child"]
    assert wf.run_status_config["child_timeout_s"] == {
        "pay_many": int(run_timeout(m.child, child=True).to_seconds()),
        "pay_many-child": 0,
    }
