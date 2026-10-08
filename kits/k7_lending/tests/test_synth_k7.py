"""Kit 7 specifics on the synthesised KitStack (marker `synth`). The generic KitStack's own
tests already cover the manifest, the exact IAM pairs and the role pins for every kit; these
pin only what kit 7 itself relies on."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.synth

ACTIONS = ("collect_weekly_repayment", "disburse_advance", "reconcile_portfolio")


@pytest.fixture(scope="module")
def template():
    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    repo = str(Path(__file__).resolve().parents[3])  # the repo root holds the infra package
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from aws_cdk import App

    from infra.app import build_app

    assembly = build_app(App(), kits=["k7"]).synth()
    (stack,) = [s for s in assembly.stacks if s.stack_name == "AwxKitK7"]
    return stack.template


def _machines(template):
    out = {}
    for name, res in template["Resources"].items():
        if res["Type"] == "AWS::StepFunctions::StateMachine":
            parts = res["Properties"]["DefinitionString"]["Fn::Join"][1]
            text = "".join(p if isinstance(p, str) else "ARN" for p in parts)
            out[name] = json.loads(text)
    return out


def test_one_machine_per_action_and_no_kit_functions(template):
    machines = _machines(template)
    assert len(machines) == len(ACTIONS)
    camel = {"".join(w.title() for w in a.split("_")) for a in ACTIONS}
    assert {next(c for c in camel if n.startswith(c)) for n in machines} == camel
    handlers = {
        r["Properties"]["Handler"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::Lambda::Function"
    }
    # Kit 7 declares no FUNCTIONS: only the generic kit-step Lambda, one per action.
    assert handlers == {"awx_spine_aws.handlers.kit_step.handler"}


def test_build_states_retry_only_service_errors(template):
    """Rule 0 matters because Build retries: kit 7's builds are idempotent for the same
    proposal (tested through the runner). Pin that the retry never covers States.ALL, so a
    kit exception (a fail-closed ApprovalBindingError) is never retried."""
    for name, machine in _machines(template).items():
        builds = {k: v for k, v in machine["States"].items() if k.startswith("Build")}
        assert builds, name
        for state, body in builds.items():
            for retry in body.get("Retry", []):
                assert "States.ALL" not in retry["ErrorEquals"], (name, state)
                assert all(e.startswith("Lambda.") for e in retry["ErrorEquals"]), (name, state)
