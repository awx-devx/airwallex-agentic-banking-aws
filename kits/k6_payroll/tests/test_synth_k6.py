"""Kit 6 through the generic KitStack: what is specific to kit 6."""

import os

import pytest

pytestmark = pytest.mark.synth


@pytest.fixture(scope="module")
def k6():
    import sys
    from pathlib import Path

    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    root = str(Path(__file__).resolve().parents[3])  # the repo root holds the infra package
    if root not in sys.path:
        sys.path.insert(0, root)
    from aws_cdk import App

    from infra.app import build_app

    assembly = build_app(App(), kits=("k6",)).synth()
    return {s.stack_name: s for s in assembly.stacks}["AwxKitK6"].template


def _machines(template):
    return {
        k: v
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::StepFunctions::StateMachine"
    }


def test_one_machine_per_action_and_one_child_machine_per_fan_out(k6):
    names = sorted(_machines(k6))
    assert {n.split("Machine")[0] for n in names} == {
        "LockRatesChild",
        "LockRatesRoot",
        "ReleaseRunClaimRoot",
        "RunPayrollChild",
        "RunPayrollRoot",
    }


def test_kit_6_declares_no_extra_functions(k6):
    import awx_k6_payroll

    assert not hasattr(awx_k6_payroll, "functions")
    with pytest.raises(ModuleNotFoundError):
        __import__("awx_k6_payroll.functions")
