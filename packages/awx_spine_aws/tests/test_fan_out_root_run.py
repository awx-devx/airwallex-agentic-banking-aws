"""The root run of a fan-out tree, as kit code and run_status see it.

StepContext.root_proposal_id and root_action name the ROOT run, so a kit records the root in
any claim a person's tool guards, at local and state machine level. run_status keeps a root
that lost a child RUNNING until the child timeout even when the lost-child record was never
written (a failing set_lost_children, a ChildrenDone Lambda that dies): only an outcome of
SUCCEEDED with no lost child reads terminal early.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from awx_common import Origin
from awx_common.tenancy import TenantContext
from awx_common.transport import encode
from awx_common.workflow import RUN_RUNNING
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.handlers import kit_step
from awx_spine_aws.local_runner import run_kit_workflow
from awx_spine_aws.repos import ProposalRepo

import toy
from aws_harness import MODULE, sfn_json
from test_asl_equivalence import Interpreter, TaskFailed

RAW = {"payees": ["ben_1", "ben_2", "ben_3"], "amount": "10.00"}


CHILD_TIMEOUT = 5000


def _recording_fan_out(seen: list):
    spec = toy.fan_out_spec()
    child = spec.child

    def rec(ctx):
        seen.append((ctx.proposal_id, ctx.root_proposal_id, ctx.root_action))
        return toy.transfer_ops("fee")(ctx)

    steps = (replace(child.steps[0], build=rec), *child.steps[1:])
    return replace(spec, child=replace(child, steps=steps))


# --- the root of the run -------------------------------------------------------------------------


def test_a_root_runs_step_context_names_itself_as_root():
    seen: list = []

    def rec(ctx):
        seen.append((ctx.proposal_id, ctx.root_proposal_id, ctx.root_action))
        return toy.transfer_ops("fee")(ctx)

    clock = toy.Clock()
    out = run_kit_workflow(
        toy.toy_spec(fee_build=rec),
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
    pid = out.proposal.proposal_id
    assert seen == [(pid, pid, "pay_supplier")]


def test_a_childs_step_context_names_the_root_run():
    seen: list = []
    clock = toy.Clock()
    out = run_kit_workflow(
        _recording_fan_out(seen),
        RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=toy.ToyPort(),
        store=InMemoryOperationStore(),
        decide=toy.approve_by("approver"),
        now=clock.now,
        sleep=clock.sleep,
        origin=Origin.AGENT,
    )
    assert out.state == "SUCCEEDED", out.detail
    root = out.proposal.proposal_id
    assert len(seen) == 3
    for own, root_id, root_action in seen:
        assert own != root  # the child's own proposal ID still owns its holds
        assert (root_id, root_action) == (root, "pay_many")


def test_a_child_execution_must_name_its_root():
    with pytest.raises(engine.WorkflowDefinitionError, match="root"):
        engine.execution_input(
            toy_proposal(),
            engine.PolicyDecision(toy_proposal().risk_tier, ()),
            {},
            None,
            parent_proposal_id="p",
            spec_path=("child",),
        )


def toy_proposal():
    clock = toy.Clock()
    return run_kit_workflow(
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
    ).proposal


def _asl(spec, aws, machines, monkeypatch, *, dies=frozenset(), lose=None):
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
    interp = Interpreter(machines["pay_many"], toy.approve_by("approver"), aws, dies)
    real, calls = interp.execute, []

    def execute(which, data, name):
        if which == "child":
            calls.append(name)
            if lose is not None and len(calls) == lose:  # a .sync task failure
                raise TaskFailed("States.TaskFailed", "the StartExecution.sync poll failed")
        return real(which, data, name)

    interp.execute = execute
    final = real("root", data, proposal.proposal_id)["run"]
    return interp, final, proposal, calls


@pytest.mark.synth
def test_asl_a_childs_step_context_names_the_root_run(aws, machines, monkeypatch):
    seen: list = []
    _, final, proposal, _ = _asl(_recording_fan_out(seen), aws, machines, monkeypatch)
    assert final["state"] == "SUCCEEDED", final["detail"]
    assert len(seen) == 3
    assert {(r, a) for _, r, a in seen} == {(proposal.proposal_id, "pay_many")}


# --- a lost-child record that was never written --------------------------------------------------


def _deployed_status(aws, monkeypatch, pid, stopped_s_ago):
    """run_status on the moto proposal row, with DescribeExecution faked to SUCCEEDED."""
    cfg = {
        "prefix": "arn:aws:states:ap-southeast-2:111122223333:execution:awx-k3-",
        "actions": ["pay_many"],
        "adapter_timeout_s": 135,
        "margin_s": 60,
        "child_timeout_s": {"pay_many": CHILD_TIMEOUT, "pay_many-child": 0},
    }
    monkeypatch.setenv("AWX_RUN_STATUS", json.dumps(cfg))
    now = datetime.now(UTC)
    aws.sfn.describe_execution = lambda executionArn: {
        "status": "SUCCEEDED",
        "stopDate": now - timedelta(seconds=stopped_s_ago),
    }
    lenv = kit_step.LambdaEnv()
    monkeypatch.setattr(lenv, "now", lambda: now)
    return lenv.run_status("k3", "pay_many", pid, "platform")


def _assert_lost_record_is_gone_but_root_stays_live(aws, monkeypatch, final, proposal):
    row = ProposalRepo("proposals", client=aws.db).get(proposal.proposal_id)
    assert final["state"] == "FAILED"
    assert final["lost_children"] == [] and row.lost_children == ()  # the record is lost
    assert row.outcome == "FAILED"  # Audit wrote the outcome
    assert _deployed_status(aws, monkeypatch, proposal.proposal_id, 200) == RUN_RUNNING
    assert _deployed_status(aws, monkeypatch, proposal.proposal_id, 195 + CHILD_TIMEOUT) == (
        "SUCCEEDED"
    )


@pytest.mark.synth
def test_asl_a_failing_set_lost_children_still_keeps_the_root_live(aws, machines, monkeypatch):
    def boom(self, proposal_id, child_ids):
        raise RuntimeError("UpdateItem failed")

    monkeypatch.setattr(ProposalRepo, "set_lost_children", boom)
    interp, final, proposal, _ = _asl(toy.fan_out_spec(), aws, machines, monkeypatch, lose=2)
    assert "ChildLost" in interp.visited
    _assert_lost_record_is_gone_but_root_stays_live(aws, monkeypatch, final, proposal)


@pytest.mark.synth
def test_asl_a_dying_children_done_still_keeps_the_root_live(aws, machines, monkeypatch):
    interp, final, proposal, _ = _asl(
        toy.fan_out_spec(), aws, machines, monkeypatch, dies=frozenset({"ChildrenDone"}), lose=2
    )
    assert "ChildLost" in interp.visited and "ChildrenDone" in interp.visited
    _assert_lost_record_is_gone_but_root_stays_live(aws, monkeypatch, final, proposal)


@pytest.mark.synth
def test_asl_a_clean_fan_out_reads_terminal_after_the_adapter_wait(aws, machines, monkeypatch):
    """The fast path still works: outcome SUCCEEDED and no lost child."""
    _, final, proposal, _ = _asl(toy.fan_out_spec(), aws, machines, monkeypatch)
    assert final["state"] == "SUCCEEDED"
    assert _deployed_status(aws, monkeypatch, proposal.proposal_id, 200) == "SUCCEEDED"
