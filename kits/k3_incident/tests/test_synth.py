"""Kit 3's synthesised stack through the generic KitStack (marker `synth`). The generic
KitStack's own tests cover manifests, exact IAM pairs and role pins for every kit; these are
only what is specific to kit 3."""

from __future__ import annotations

import json
import os

import pytest
from awx_k3_incident import SPECS
from test_tool_surface import EXPECTED_TOOLS

pytestmark = pytest.mark.synth


@pytest.fixture(scope="module")
def assembly():
    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    from aws_cdk import App

    from infra.app import build_app

    return build_app(App(context={"kits": "k3"})).synth()


def _stack(assembly, name):
    (stack,) = [s for s in assembly.stacks if s.stack_name == name]
    return stack.template["Resources"]


def _of(resources, kind):
    return {k: v for k, v in resources.items() if v["Type"] == kind}


def test_kits_k3_builds_only_kit_3(assembly):
    assert {s.stack_name for s in assembly.stacks} == {"AwxSpine", "AwxAgentStack", "AwxKitK3"}


def test_one_state_machine_per_action_and_no_declared_functions(assembly):
    res = _stack(assembly, "AwxKitK3")
    machines = _of(res, "AWS::StepFunctions::StateMachine")
    assert len(machines) == len(SPECS) == 3
    assert "SendSupplierTransfer" not in " ".join(machines)  # staging is operator-only
    names = " ".join(machines)
    for action in SPECS:
        assert "".join(p.title() for p in action.split("_")) in names, action
    # Kit 3 declares no FUNCTIONS: its only Lambdas are the workflow step Lambdas, one per action.
    fns = _of(res, "AWS::Lambda::Function")
    assert len(fns) == len(SPECS)
    assert {f["Properties"]["Handler"] for f in fns.values()} == {
        "awx_spine_aws.handlers.kit_step.handler"
    }


def test_no_kit3_role_can_read_the_airwallex_secret(assembly):
    res = _stack(assembly, "AwxKitK3")
    blob = json.dumps(
        [v for v in res.values() if v["Type"] in ("AWS::IAM::Policy", "AWS::IAM::Role")]
    )
    assert "secretsmanager:GetSecretValue" not in blob


def _names(obj, out):
    if isinstance(obj, dict):
        if isinstance(obj.get("Name"), str):
            out.add(obj["Name"])
        for v in obj.values():
            _names(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _names(v, out)
    return out


def test_the_gateway_registers_exactly_the_pinned_kit3_tools(assembly):
    """The Gateway target's tool Name entries equal the pinned set,
    so a dropped or added tool (including the operator-only send) fails."""
    res = _stack(assembly, "AwxAgentStack")
    (target,) = _of(res, "AWS::BedrockAgentCore::GatewayTarget").values()
    names = _names(target["Properties"], set()) - {"k3"}
    assert names == EXPECTED_TOOLS
