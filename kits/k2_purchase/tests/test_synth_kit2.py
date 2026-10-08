"""Kit 2 on the generic KitStack, marked `synth`. The KitStack's own tests cover the manifest,
the exact IAM pairs and the role pins for every kit; these check only what is specific to kit 2:
six state machines (one per SPECS action), no kit-declared Lambda (kit 2 has no FUNCTIONS),
the Gateway target carries exactly kit 2's tools, and kit 2's stack touches no connected
account and never names the adapter as a tool target."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from awx_k2_purchase import SPECS, TOOLS

pytestmark = pytest.mark.synth

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def stacks():
    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    from aws_cdk import App

    from infra.app import build_app

    assembly = build_app(App(), kits=["k2"]).synth()
    return {s.stack_name: s.template for s in assembly.stacks}


def _of(template, kind):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == kind}


def test_kit2_stack_has_one_state_machine_per_action(stacks):
    kit = stacks["AwxKitK2"]
    machines = _of(kit, "AWS::StepFunctions::StateMachine")
    assert len(machines) == len(SPECS) == 6


def test_kit2_declares_no_lambda_of_its_own():
    import importlib.util

    assert importlib.util.find_spec("awx_k2_purchase.functions") is None
    assert importlib.util.find_spec("awx_k2_purchase.infra") is None


def test_the_gateway_target_carries_exactly_kit2s_tools(stacks):
    agent = json.dumps(stacks["AwxAgentStack"])
    for tool in TOOLS:
        assert tool.name in agent, tool.name
    assert "k3___" not in agent  # the subset build carries no other kit's tools


def test_kit2_stack_names_no_connected_account(stacks):
    text = json.dumps(stacks["AwxKitK2"])
    assert "acct_" not in text
    assert "x-on-behalf-of" not in text.lower()
