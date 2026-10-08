"""Kit 1's synthesised stack through the generic KitStack (marker `synth`). The infra tests
cover manifests, exact IAM pairs and role pins for every kit; these are only what is specific to
kit 1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from awx_k1_treasury import SPECS, TOOLS

pytestmark = pytest.mark.synth


@pytest.fixture(scope="module")
def assembly():
    """AWX_SKIP_RUNTIME_DEPS and the repo root on sys.path are scoped
    to this fixture and restored afterwards."""
    root = str(Path(__file__).resolve().parents[3])  # the repo root holds the infra package
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AWX_SKIP_RUNTIME_DEPS", "1")
        mp.syspath_prepend(root)
        from aws_cdk import App

        from infra.app import build_app

        yield build_app(App(context={"kits": "k1"})).synth()


def _stack(assembly, name):
    (stack,) = [s for s in assembly.stacks if s.stack_name == name]
    return stack.template["Resources"]


def _of(resources, kind):
    return {k: v for k, v in resources.items() if v["Type"] == kind}


def test_kits_k1_builds_only_kit_1(assembly):
    assert {s.stack_name for s in assembly.stacks} == {"AwxSpine", "AwxAgentStack", "AwxKitK1"}


def test_three_state_machines_and_no_declared_functions(assembly):
    res = _stack(assembly, "AwxKitK1")
    machines = _of(res, "AWS::StepFunctions::StateMachine")
    assert len(machines) == len(SPECS) == 3  # lock_rate, execute_plan, release_obligation
    names = " ".join(machines)
    for action in SPECS:
        assert "".join(p.title() for p in action.split("_")) in names, action
    fns = _of(res, "AWS::Lambda::Function")
    assert len(fns) == len(SPECS)  # kit 1 declares no FUNCTIONS
    assert {f["Properties"]["Handler"] for f in fns.values()} == {
        "awx_spine_aws.handlers.kit_step.handler"
    }


def test_no_kit1_role_can_read_the_airwallex_secret(assembly):
    res = _stack(assembly, "AwxKitK1")
    blob = json.dumps(
        [v for v in res.values() if v["Type"] in ("AWS::IAM::Policy", "AWS::IAM::Role")]
    )
    assert "secretsmanager:GetSecretValue" not in blob


def test_the_gateway_registers_exactly_kit1s_tools(assembly):
    """Structural, from the k1 Gateway target's tool schema, not a
    substring of the stack JSON."""
    res = _stack(assembly, "AwxAgentStack")
    targets = [
        v["Properties"]
        for v in _of(res, "AWS::BedrockAgentCore::GatewayTarget").values()
        if v["Properties"].get("Name") == "k1"
    ]
    assert len(targets) == 1
    schema = targets[0]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
    assert sorted(t["Name"] for t in schema) == sorted(t.name for t in TOOLS)
