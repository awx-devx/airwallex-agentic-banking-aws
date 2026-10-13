"""PolicyContext.run_status: the status of another action's root run of the same kit.

A policy of one action (a person's tool) can read the run of another action it guards, at
local_runner level, in the deployed kit step (DescribeExecution on awx-<kit>-<action>) and
through the synthesised state machine. A stopped run reads terminal only once it can send
nothing more: the adapter timeout plus a margin after its stop, plus the child machine timeout
after an abnormal end. A child proposal ID, a wrong action or an action outside the kit is
refused.
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
from awx_common.workflow import RUN_RUNNING, RUN_TERMINAL
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.handlers import kit_step
from awx_spine_aws.local_runner import RunRegistry, run_kit_workflow

import toy
from aws_harness import MODULE, sfn_json
from test_asl_equivalence import Interpreter

GUARDED = "pay_supplier"  # the run a person's tool guards


RELEASE = "release_hold"  # the person's tool: another action of the same kit


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


CFG = {
    "prefix": "arn:aws:states:ap-southeast-2:111122223333:execution:awx-k3-",
    "actions": [GUARDED, RELEASE],
    "adapter_timeout_s": 135,
    "margin_s": 60,
    "child_timeout_s": {GUARDED: 1000, RELEASE: 0},
}


def _local(spec, clock, runs, **kw):
    return run_kit_workflow(
        spec,
        kw.pop("raw", toy.RAW),
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=toy.ToyPort(),
        store=kw.pop("store", InMemoryOperationStore()),
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.SCHEDULE,
        runs=runs,
        **kw,
    )


def _release_spec(seen: list, guarded_pid: str, action: str = GUARDED):
    """A person's tool of another action whose policy asks for the guarded run's status."""
    spec = toy.toy_spec(action=RELEASE)
    base = spec.policy

    def policy(intent, ctx):
        if ctx.proposal_id is not None:  # recheck: inside the release run
            seen.append(ctx.run_status(guarded_pid, action))
        return base(intent, ctx)

    return replace(spec, policy=policy)


# --- the guarded action's run, at local_runner level ---------------------------------------------


def test_a_policy_of_one_action_reads_another_actions_live_run():
    runs = RunRegistry()
    runs.set("k3", GUARDED, "pid-live", RUN_RUNNING, "platform")  # the guarded run is still going
    seen: list = []
    _local(_release_spec(seen, "pid-live"), toy.Clock(), runs)
    assert seen == [RUN_RUNNING]


def test_a_policy_of_one_action_reads_another_actions_finished_run():
    clock, runs = toy.Clock(), RunRegistry()
    guarded = _local(toy.toy_spec(action=GUARDED), clock, runs)
    seen: list = []
    _local(_release_spec(seen, guarded.proposal.proposal_id), clock, runs)
    assert seen == ["SUCCEEDED"] and "SUCCEEDED" in RUN_TERMINAL


def test_the_wrong_action_for_a_known_run_is_refused():
    clock, runs = toy.Clock(), RunRegistry()
    guarded = _local(toy.toy_spec(action=GUARDED), clock, runs)
    seen: list = []
    out = _local(_release_spec(seen, guarded.proposal.proposal_id, RELEASE), clock, runs)
    assert seen == [] and out.state == "FAILED"  # ValueError in recheck: fails closed
    assert "is a k3.pay_supplier proposal" in out.detail


def test_an_action_that_is_not_the_kits_is_refused():
    seen: list = []
    out = _local(_release_spec(seen, "pid-x", "pay_someone_else"), toy.Clock(), RunRegistry())
    assert seen == [] and "not one of k3's actions" in out.detail


def test_a_declared_action_with_no_run_reads_none():
    seen: list = []
    _local(
        _release_spec(seen, "pid-none", "other_action"),
        toy.Clock(),
        RunRegistry(),
        actions=("other_action",),
    )
    assert seen == [None]


def test_a_child_proposal_id_is_refused():
    clock, runs = toy.Clock(), RunRegistry()
    parent = run_kit_workflow(
        toy.fan_out_spec(),
        {"payees": ["ben_1", "ben_2", "ben_3"], "amount": "10.00"},
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=toy.ToyPort(),
        store=InMemoryOperationStore(),
        decide=toy.approve_by("approver"),
        now=clock.now,
        sleep=clock.sleep,
        origin=Origin.AGENT,
        runs=runs,
    )
    assert parent.state == "SUCCEEDED", parent.detail
    child = parent.child_outcomes[0].proposal
    with pytest.raises(ValueError, match="child proposal"):
        runs.status("k3", child.action, child.proposal_id, "platform")


# --- terminal means the run can send nothing more ------------------------------------------------


@pytest.mark.parametrize(
    ("status", "age_s", "want"),
    [
        ("RUNNING", 10_000, RUN_RUNNING),
        ("PENDING_REDRIVE", 10_000, RUN_RUNNING),  # not terminal
        ("SUCCEEDED", 100, RUN_RUNNING),  # an adapter Lambda may still be in flight
        ("SUCCEEDED", 194, RUN_RUNNING),
        ("SUCCEEDED", 195, "SUCCEEDED"),  # adapter timeout 135 + margin 60
        ("FAILED", 195, RUN_RUNNING),  # abnormal: a child may still be sending
        ("TIMED_OUT", 1194, RUN_RUNNING),
        ("ABORTED", 1194, RUN_RUNNING),
        ("FAILED", 1195, "FAILED"),  # + the guarded action's child timeout (1000)
        ("TIMED_OUT", 1195, "TIMED_OUT"),
        ("ABORTED", 5000, "ABORTED"),
    ],
)
def test_a_stopped_run_reads_terminal_only_once_it_can_send_nothing_more(status, age_s, want):
    desc = {"status": status, "stopDate": NOW - timedelta(seconds=age_s)}
    if status in ("RUNNING", "PENDING_REDRIVE"):
        desc.pop("stopDate")
    assert kit_step.settled_status(desc, CFG, GUARDED, NOW) == want


def test_a_terminal_status_without_a_stop_date_reads_running():
    assert kit_step.settled_status({"status": "SUCCEEDED"}, CFG, GUARDED, NOW) == RUN_RUNNING


def test_an_action_without_children_waits_out_the_adapter_only():
    desc = {"status": "ABORTED", "stopDate": NOW - timedelta(seconds=195)}
    assert kit_step.settled_status(desc, CFG, RELEASE, NOW) == "ABORTED"


class _Stored:
    def __init__(
        self, action, parent=None, kit="k3", tenant="platform", lost=(), outcome="SUCCEEDED"
    ):
        self.proposal = types.SimpleNamespace(kit=kit, action=action)
        self.parent_proposal_id = parent
        self.tenant_id = tenant
        self.lost_children = tuple(lost)
        self.outcome = outcome  # Audit's set_outcome (read for actions with children)


class _Repo:
    def __init__(self, rows):
        self.rows = rows

    def get(self, pid):
        return self.rows.get(pid)


class _Sfn:
    class exceptions:
        class ExecutionDoesNotExist(Exception):
            pass

    def __init__(self, known):
        self.known, self.described = known, []

    def describe_execution(self, executionArn):
        self.described.append(executionArn)
        got = self.known.get(executionArn)
        if got is None:
            raise self.exceptions.ExecutionDoesNotExist(executionArn)
        if isinstance(got, Exception):
            raise got
        return got


class _Denied(Exception):
    response = {"Error": {"Code": "AccessDeniedException"}}  # noqa: RUF012


@pytest.fixture
def deployed(monkeypatch):
    monkeypatch.setenv("AWX_RUN_STATUS", json.dumps(CFG))
    arn = CFG["prefix"] + GUARDED + ":"
    sfn = _Sfn(
        {
            arn + "pid-live": {"status": "RUNNING"},
            arn + "pid-done": {"status": "SUCCEEDED", "stopDate": NOW - timedelta(hours=1)},
            arn + "pid-fresh": {"status": "SUCCEEDED", "stopDate": NOW - timedelta(seconds=30)},
            arn + "pid-denied": _Denied("no"),
        }
    )
    rows = {p: _Stored(GUARDED) for p in ("pid-live", "pid-done", "pid-fresh", "pid-denied")}
    rows["pid-gone"] = _Stored(GUARDED)
    rows["pid-child"] = _Stored(GUARDED, parent="pid-live")
    monkeypatch.setattr(kit_step, "boto_client", lambda name: sfn)
    monkeypatch.setattr(kit_step, "_repo", lambda cls, var: _Repo(rows))
    lenv = kit_step.LambdaEnv()
    monkeypatch.setattr(lenv, "now", lambda: NOW)
    return lenv, sfn, arn


def test_the_deployed_run_status_describes_the_guarded_actions_machine(deployed):
    lenv, sfn, arn = deployed
    assert lenv.run_status("k3", GUARDED, "pid-live", "platform") == RUN_RUNNING
    assert lenv.run_status("k3", GUARDED, "pid-done", "platform") == "SUCCEEDED"
    assert (
        lenv.run_status("k3", GUARDED, "pid-fresh", "platform") == RUN_RUNNING
    )  # stopped 30 s ago
    assert sfn.described == [arn + "pid-live", arn + "pid-done", arn + "pid-fresh"]


def test_the_deployed_run_status_none_only_for_no_such_root_run(deployed):
    lenv, _sfn, _ = deployed
    assert lenv.run_status("k3", GUARDED, "pid-unknown", "platform") is None  # no proposal row
    assert lenv.run_status("k3", GUARDED, "pid-gone", "platform") is None  # ExecutionDoesNotExist
    with pytest.raises(ValueError, match="child proposal"):
        lenv.run_status("k3", GUARDED, "pid-child", "platform")
    with pytest.raises(ValueError, match=r"not k3\.release_hold"):
        lenv.run_status("k3", RELEASE, "pid-live", "platform")
    with pytest.raises(_Denied):  # any other error fails closed
        lenv.run_status("k3", GUARDED, "pid-denied", "platform")


def test_the_deployed_kit_actions_come_from_the_environment(deployed, monkeypatch):
    lenv, _, _ = deployed
    monkeypatch.setattr(kit_step, "root_spec", lambda: toy.toy_spec(action=RELEASE))
    assert lenv.kit_actions("k3") == frozenset({GUARDED, RELEASE})
    assert lenv.kit_actions("k9") == frozenset()


# --- the release machine describes the pay_supplier execution ------------------------------------


def _asl(spec, action, aws, machines, monkeypatch, specs):
    module = types.ModuleType(MODULE)
    module.SPECS = specs
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", action)
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
    return interp.execute("root", data, proposal.proposal_id)["run"], proposal


@pytest.mark.synth
def test_asl_a_policy_of_one_action_describes_another_actions_execution(aws, machines, monkeypatch):
    monkeypatch.setenv("AWX_RUN_STATUS", json.dumps(CFG))
    aws.port = toy.ToyPort()
    guarded_spec = toy.toy_spec(action=GUARDED)
    seen: list = []
    specs = {GUARDED: guarded_spec}
    _, guarded = _asl(guarded_spec, GUARDED, aws, machines, monkeypatch, specs)
    stopped = datetime.now(UTC) - timedelta(hours=1)
    aws.sfn.describe_execution = lambda executionArn: (
        aws.sfn.described.append(executionArn) or {"status": "SUCCEEDED", "stopDate": stopped}
    )
    release = _release_spec(seen, guarded.proposal_id)
    specs = {GUARDED: guarded_spec, RELEASE: release}
    final, _ = _asl(release, RELEASE, aws, machines, monkeypatch, specs)
    assert final["state"] == "SUCCEEDED", final["detail"]
    assert seen == ["SUCCEEDED"]
    assert aws.sfn.described == [CFG["prefix"] + GUARDED + ":" + guarded.proposal_id]


# --- synth: names, environment and the one grant -------------------------------------------------


@pytest.mark.synth
def test_root_machines_are_named_per_action_and_the_grant_covers_only_this_kit(monkeypatch):
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from awx_common.regions import DEFAULT_REGION
    from infra.app import build_app
    from infra.constructs.kit_workflow import KitWorkflow, root_machine_name, run_timeout
    from infra.stacks.spine_stack import ADAPTER_TIMEOUT_S

    specs = {"pay_many": toy.fan_out_spec(), RELEASE: toy.toy_spec(action=RELEASE)}
    module = types.ModuleType(MODULE)
    module.SPECS = specs
    monkeypatch.setitem(sys.modules, MODULE, module)
    app = cdk.App(context={"account": "111122223333", "region": DEFAULT_REGION})
    build_app(app)
    env = cdk.Environment(account="111122223333", region=DEFAULT_REGION)
    stack = cdk.Stack(app, "RunStatusKit", env=env)
    spine = app.node.find_child("AwxSpine")
    from infra.kit_manifest import parse_workflow

    manifests = {a: parse_workflow(s) for a, s in specs.items()}  # manifests, not specs
    for action in specs:
        KitWorkflow(
            stack,
            f"Wf{action}",
            spine=spine,
            kit="k3",
            action=action,
            spec_module=MODULE,
            workflow=manifests[action],
            kit_workflows=manifests,
            allow_stand_in=True,
        )
    t = Template.from_stack(stack).to_json()["Resources"]
    names = sorted(
        str(r["Properties"].get("StateMachineName"))
        for r in t.values()
        if r["Type"] == "AWS::StepFunctions::StateMachine"
    )
    # The child is named too, under the same awx-<kit>-* grant
    assert names == ["awx-k3-pay_many", "awx-k3-pay_many-child", "awx-k3-release_hold"]
    assert root_machine_name("k3", "pay_many") == "awx-k3-pay_many"
    steps = [
        r
        for r in t.values()
        if r["Type"] == "AWS::Lambda::Function"
        and r["Properties"]["Handler"] == "awx_spine_aws.handlers.kit_step.handler"
    ]
    assert len(steps) == 2
    for step in steps:
        raw = step["Properties"]["Environment"]["Variables"]["AWX_RUN_STATUS"]
        if not isinstance(raw, str):  # the ARN prefix holds partition/region/account tokens
            sep, parts = raw["Fn::Join"]
            raw = sep.join(p if isinstance(p, str) else "TOKEN" for p in parts)
        cfg = json.loads(raw)
        assert cfg["prefix"].endswith(":execution:awx-k3-")
        assert cfg["actions"] == ["pay_many", RELEASE]
        assert cfg["adapter_timeout_s"] == ADAPTER_TIMEOUT_S
        child = int(run_timeout(specs["pay_many"].child, child=True).to_seconds())
        assert cfg["child_timeout_s"] == {"pay_many": child, "pay_many-child": 0, RELEASE: 0}
    grants = [
        stmt
        for k, r in t.items()
        if r["Type"] == "AWS::IAM::Policy" and "StepRunStatus" in k
        for stmt in r["Properties"]["PolicyDocument"]["Statement"]
    ]
    assert len(grants) == 2
    for stmt in grants:
        assert stmt["Action"] == "states:DescribeExecution"
        parts = stmt["Resource"]["Fn::Join"][1]
        assert parts[-1].endswith(":execution:awx-k3-*:*")  # this kit's root machines only
