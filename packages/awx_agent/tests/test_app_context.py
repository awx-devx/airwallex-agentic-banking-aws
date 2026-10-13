"""The CDK app's Region and model ID context, checked before any stack is built."""

from __future__ import annotations

import pytest
from aws_cdk import App
from awx_common.regions import DEFAULT_REGION

from infra.app import target_model_ids, target_region


def _app(**context):
    return App(context=context)


def test_an_absent_region_context_uses_the_default():
    assert target_region(_app()) == DEFAULT_REGION


@pytest.mark.parametrize("value", ["", " ", "\t"], ids=["empty", "space", "tab"])
def test_an_empty_region_context_is_refused_not_defaulted(value):
    with pytest.raises(ValueError, match="REGION is empty") as caught:
        target_region(_app(region=value))
    assert "export REGION=us-west-2" in str(caught.value)


def test_a_region_with_stray_whitespace_is_stripped():
    assert target_region(_app(region=" eu-west-1 ")) == "eu-west-1"


@pytest.mark.parametrize(
    "value", ["US-WEST-2", "us_west_2", "uswest2", "us-west", "secretsmanager"]
)
def test_a_value_that_is_not_a_region_code_is_refused(value):
    with pytest.raises(ValueError, match="is not an AWS Region code") as caught:
        target_region(_app(region=value))
    assert str(caught.value) == f"REGION={value} is not an AWS Region code such as us-west-2"


@pytest.mark.parametrize(
    ("context", "missing", "given"),
    [
        ({}, ["MODEL_ID (CDK context model_id)", "FALLBACK_MODEL_ID"], []),
        (
            {"model_id": "global.anthropic.claude-sonnet-5"},
            ["FALLBACK_MODEL_ID (CDK context fallback_model_id)"],
            [" MODEL_ID (CDK context model_id)"],
        ),
        (
            {"fallback_model_id": "jp.anthropic.claude-sonnet-4-5-20250929-v1:0"},
            ["MODEL_ID (CDK context model_id)"],
            ["FALLBACK_MODEL_ID"],
        ),
    ],
    ids=["no-override", "only-model-id", "only-fallback-model-id"],
)
def test_a_region_outside_the_table_names_the_missing_overrides(context, missing, given):
    with pytest.raises(
        ValueError, match="REGION=ap-northeast-1 has no default model IDs"
    ) as caught:
        target_model_ids(_app(**context), "ap-northeast-1")
    message = str(caught.value)
    for name in missing:
        assert name in message
    for name in given:
        assert name not in message


@pytest.mark.parametrize(
    ("key", "variable"), [("model_id", "MODEL_ID"), ("fallback_model_id", "FALLBACK_MODEL_ID")]
)
def test_an_arn_model_id_is_refused(key, variable):
    arn = "arn:aws:bedrock:us-west-2:111122223333:application-inference-profile/abc"
    with pytest.raises(ValueError, match="is an ARN") as caught:
        target_model_ids(_app(**{key: arn}), DEFAULT_REGION)
    message = str(caught.value)
    assert message.startswith(f"{variable}={arn} is an ARN")
    assert "model ID or an inference profile ID" in message
