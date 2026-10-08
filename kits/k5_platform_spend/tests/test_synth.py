"""Kit 5 synthesised through the generic KitStack. The infra tests cover every kit's manifest,
functions and IAM pairs; these add only what is specific to kit 5."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.synth


@pytest.fixture(scope="module")
def assembly():
    import os

    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    from aws_cdk import App

    from infra.app import build_app

    return build_app(App(context={"kits": "k5"})).synth()


def _template(assembly, name: str) -> dict:
    return next(s for s in assembly.stacks if s.stack_name == name).template


def test_only_kit_5_is_built(assembly):
    assert {s.stack_name for s in assembly.stacks} == {"AwxSpine", "AwxKitK5", "AwxAgentStack"}


def test_one_state_machine_per_action_and_no_extra_functions(assembly):
    from awx_k5_platform_spend import SPECS

    res = _template(assembly, "AwxKitK5")["Resources"]
    machines = [k for k, v in res.items() if v["Type"] == "AWS::StepFunctions::StateMachine"]
    expected = {a.replace("_", "").lower() for a in SPECS}
    assert {m.lower().split("rootmachine")[0] for m in machines} == expected
    assert len(machines) == len(SPECS) == 7  # kit 5 declares no FUNCTIONS of its own


def test_every_step_is_laid_out_in_its_machine(assembly):
    """One Route/Dispatch/finality block per StepSpec, named after the step (the generic layout
    is the same for a zero-operation step; the engine skips its finality at run time)."""
    res = _template(assembly, "AwxKitK5")["Resources"]

    def states(prefix: str) -> str:
        (key,) = [
            k
            for k, v in res.items()
            if k.startswith(f"{prefix}RootMachine")
            and v["Type"] == "AWS::StepFunctions::StateMachine"
        ]
        return json.dumps(res[key]["Properties"]["DefinitionString"])

    for machine, steps in (
        ("BridgeShortfall", ("Bridge",)),
        ("CollectFee", ("Charge",)),
        ("IssueCard", ("CreateCardholder", "CreateCard")),
        ("RegisterShortfall", ("Register",)),
        ("ReconcileBridge", ("Apply",)),
    ):
        text = states(machine)
        for i, step in enumerate(steps, start=1):
            assert f"Dispatch{i}{step}" in text, (machine, step)


def test_the_reader_allows_kit_5_platform_reads(assembly):
    """Platform objects (transfers, charges, the platform wallet) are read without a header,
    so the reader's platform allowlist must carry them."""
    spine = json.dumps(_template(assembly, "AwxSpine"))
    for pattern in ("connected_account_transfers$", "connected_account_transfers/[A-Za-z0-9_-]+$"):
        assert pattern in spine
    assert "charges" in spine
