"""Synth the agent stack per deploy Region: model IDs follow the Region and the Runtime role
reaches the models only through its own inference profiles."""

from __future__ import annotations

import json
import os

import pytest
from awx_common.regions import DEFAULT_REGION

pytestmark = pytest.mark.synth

ACCOUNT = "111122223333"
SONNET_5 = "anthropic.claude-sonnet-5"
SONNET_4_5 = "anthropic.claude-sonnet-4-5-20250929-v1:0"


def _agent(**context):
    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"  # stage source only; no network in synth tests
    from aws_cdk import App
    from aws_cdk.assertions import Annotations, Match, Template

    from infra.app import build_app

    app = build_app(App(context={"account": ACCOUNT, **context}), kits=())
    stack = app.node.find_child("AwxAgentStack")
    template = Template.from_stack(stack)
    errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp("AwsSolutions"))
    assert errors == [], [e.entry.data for e in errors]
    return stack, template


def _runtime_env(template):
    (rt,) = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    return rt["Properties"]["EnvironmentVariables"]


def _model_statements(template):
    out = {}
    for res in template.find_resources("AWS::IAM::Policy").values():
        if "AgentRuntime" not in json.dumps(res["Properties"].get("Roles", [])):
            continue
        for s in res["Properties"]["PolicyDocument"]["Statement"]:
            if s.get("Sid", "").startswith("InvokeAgent"):
                out[s["Sid"]] = s
    return out


def _profile(region, model_id):
    return f"arn:aws:bedrock:{region}:{ACCOUNT}:inference-profile/{model_id}"


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _assert_profile_grant(template, region, prefix):
    primary, fallback = f"{prefix}.{SONNET_5}", f"{prefix}.{SONNET_4_5}"
    env = _runtime_env(template)
    assert (env["AWX_MODEL_ID"], env["AWX_FALLBACK_MODEL_ID"]) == (primary, fallback)
    assert env["AWX_REGION"] == region
    stmts = _model_statements(template)
    assert set(stmts) == {"InvokeAgentModel", "InvokeAgentModelThroughProfile"}
    profiles = [_profile(region, primary), _profile(region, fallback)]
    assert _as_list(stmts["InvokeAgentModel"]["Resource"]) == profiles
    routed = stmts["InvokeAgentModelThroughProfile"]
    assert _as_list(routed["Resource"]) == [
        f"arn:aws:bedrock:*::foundation-model/{SONNET_5}*",
        f"arn:aws:bedrock:*::foundation-model/{SONNET_4_5}*",
    ]
    assert routed["Condition"] == {"StringEquals": {"bedrock:InferenceProfileArn": profiles}}
    assert "Condition" not in stmts["InvokeAgentModel"]


def test_the_default_synth_targets_the_default_region_with_us_profiles():
    stack, template = _agent()
    assert stack.region == DEFAULT_REGION == "us-west-2"
    _assert_profile_grant(template, DEFAULT_REGION, "us")


def test_sydney_gets_the_au_profiles():
    stack, template = _agent(region="ap-southeast-2")
    assert stack.region == "ap-southeast-2"
    _assert_profile_grant(template, "ap-southeast-2", "au")


def test_ireland_gets_the_eu_profiles():
    _stack, template = _agent(region="eu-west-1")
    _assert_profile_grant(template, "eu-west-1", "eu")


def test_a_region_outside_the_table_fails_synth_naming_the_overrides():
    with pytest.raises(ValueError, match="no default model IDs") as caught:
        _agent(region="ap-northeast-1")
    message = str(caught.value)
    for name in ("REGION=ap-northeast-1", "MODEL_ID", "FALLBACK_MODEL_ID"):
        assert name in message


def test_a_region_outside_the_table_synthesises_with_both_overrides():
    primary = f"global.{SONNET_5}"
    fallback = f"jp.{SONNET_4_5}"
    _stack, template = _agent(region="ap-northeast-1", model_id=primary, fallback_model_id=fallback)
    env = _runtime_env(template)
    assert (env["AWX_MODEL_ID"], env["AWX_FALLBACK_MODEL_ID"]) == (primary, fallback)
    profiles = [_profile("ap-northeast-1", primary), _profile("ap-northeast-1", fallback)]
    assert _as_list(_model_statements(template)["InvokeAgentModel"]["Resource"]) == profiles


def test_a_model_id_override_is_used_and_granted():
    override = f"global.{SONNET_5}"
    _stack, template = _agent(model_id=override)
    env = _runtime_env(template)
    assert env["AWX_MODEL_ID"] == override
    assert env["AWX_FALLBACK_MODEL_ID"] == f"us.{SONNET_4_5}"
    stmts = _model_statements(template)
    profiles = [_profile(DEFAULT_REGION, override), _profile(DEFAULT_REGION, f"us.{SONNET_4_5}")]
    assert _as_list(stmts["InvokeAgentModel"]["Resource"]) == profiles
    condition = stmts["InvokeAgentModelThroughProfile"]["Condition"]
    assert condition["StringEquals"]["bedrock:InferenceProfileArn"] == profiles


def test_a_plain_foundation_model_override_is_granted_in_the_stack_region_only():
    _stack, template = _agent(fallback_model_id=SONNET_4_5)
    assert _runtime_env(template)["AWX_FALLBACK_MODEL_ID"] == SONNET_4_5
    stmts = _model_statements(template)
    assert _as_list(stmts["InvokeAgentFoundationModel"]["Resource"]) == [
        f"arn:aws:bedrock:{DEFAULT_REGION}::foundation-model/{SONNET_4_5}"
    ]
    assert "Condition" not in stmts["InvokeAgentFoundationModel"]
    # The profile statements cover the primary profile only.
    assert _as_list(stmts["InvokeAgentModel"]["Resource"]) == [
        _profile(DEFAULT_REGION, f"us.{SONNET_5}")
    ]
    assert _as_list(stmts["InvokeAgentModelThroughProfile"]["Resource"]) == [
        f"arn:aws:bedrock:*::foundation-model/{SONNET_5}*"
    ]
