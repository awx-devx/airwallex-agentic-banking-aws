"""Kit 8 synthesis through the generic KitStack. The KitStack's own tests cover the manifest,
the functions and the IAM pairs; these check what is specific to kit 8: the Build tasks' retry
budget behind the adoption wait, and that kit 8 declares no extra functions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.synth

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def kit_stack():
    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from aws_cdk import App

    from infra.app import build_app

    assembly = build_app(App(), kits=["k8"]).synth()
    stacks = {s.stack_name: s for s in assembly.stacks}
    assert set(stacks) == {"AwxSpine", "AwxKitK8", "AwxAgentStack"}
    return stacks["AwxKitK8"].template


def _render(definition) -> dict:
    if isinstance(definition, str):
        return json.loads(definition)
    parts = definition["Fn::Join"][1]
    return json.loads("".join(p if isinstance(p, str) else "0" for p in parts))


def _machines(template) -> dict[str, dict]:
    return {
        lid: _render(r["Properties"]["DefinitionString"])
        for lid, r in template["Resources"].items()
        if r["Type"] == "AWS::StepFunctions::StateMachine"
    }


def test_one_state_machine_per_action_and_no_extra_functions(kit_stack):
    from awx_k8_settlement import SPECS

    machines = _machines(kit_stack)
    assert len(machines) == len(SPECS)
    import importlib.util

    assert importlib.util.find_spec("awx_k8_settlement.functions") is None  # no FUNCTIONS


def test_every_build_retry_matches_the_budget_the_adoption_wait_assumes(kit_stack):
    from awx_k8_settlement.common import (
        ADOPTION_WAIT,
        BUILD_RETRY_BACKOFF,
        BUILD_RETRY_BUDGET,
        BUILD_RETRY_INTERVAL_S,
        BUILD_TASK_RETRIES,
        STEP_LAMBDA_TIMEOUT_S,
    )

    builds = 0
    for asl in _machines(kit_stack).values():
        for name, state in asl["States"].items():
            if not name.startswith("Build"):
                continue
            builds += 1
            retries = state.get("Retry", [])
            assert retries, name
            errors = {e for r in retries for e in r["ErrorEquals"]}
            assert "States.ALL" not in errors, name  # a kit-code raise is never retried
            assert "States.TaskFailed" not in errors, name
            for r in retries:
                assert r["MaxAttempts"] <= BUILD_TASK_RETRIES, name
                assert r["IntervalSeconds"] <= BUILD_RETRY_INTERVAL_S, name
                assert r.get("BackoffRate", 2) <= BUILD_RETRY_BACKOFF, name
            read = sum(r["MaxAttempts"] for r in retries)
            assert read <= BUILD_TASK_RETRIES, name
    assert builds >= 5
    step_timeouts = {
        r["Properties"]["Timeout"]
        for lid, r in kit_stack["Resources"].items()
        if r["Type"] == "AWS::Lambda::Function" and "StepFn" in lid
    }
    assert step_timeouts
    assert max(step_timeouts) <= STEP_LAMBDA_TIMEOUT_S
    assert BUILD_RETRY_BUDGET < ADOPTION_WAIT


def test_resolve_has_no_step_after_record_decision(kit_stack):
    """The adoption-wait assumption: decided_at is written in the only Build step."""
    from awx_k8_settlement import SPECS

    assert [s.name for s in SPECS["resolve_in_flight_money"].steps] == ["record_decision"]
    (asl,) = [a for lid, a in _machines(kit_stack).items() if lid.startswith("ResolveInFlight")]
    assert [n for n in asl["States"] if n.startswith("Build")] == ["Build1RecordDecision"]
