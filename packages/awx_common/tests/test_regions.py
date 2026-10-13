"""The deploy Region default and the model IDs that follow the Region."""

from __future__ import annotations

import pytest
from awx_common.regions import (
    DEFAULT_REGION,
    INFERENCE_PROFILE_PREFIX,
    UnsupportedRegionError,
    default_model_ids,
    foundation_model,
    is_inference_profile,
)


def test_the_default_region_has_default_model_ids():
    assert DEFAULT_REGION in INFERENCE_PROFILE_PREFIX
    assert default_model_ids(DEFAULT_REGION) == (
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    )


@pytest.mark.parametrize(
    ("region", "prefix"),
    [("ap-southeast-2", "au"), ("eu-central-1", "eu"), ("ca-central-1", "us"), ("us-east-1", "us")],
)
def test_model_ids_carry_the_regions_profile_prefix(region, prefix):
    primary, fallback = default_model_ids(region)
    assert primary == f"{prefix}.anthropic.claude-sonnet-5"
    assert fallback == f"{prefix}.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_a_region_outside_the_table_names_the_overrides():
    with pytest.raises(UnsupportedRegionError, match="REGION=sa-east-1") as caught:
        default_model_ids("sa-east-1")
    assert "MODEL_ID" in str(caught.value)
    assert "FALLBACK_MODEL_ID" in str(caught.value)


@pytest.mark.parametrize(
    ("model_id", "profile", "model"),
    [
        ("us.anthropic.claude-sonnet-5", True, "anthropic.claude-sonnet-5"),
        ("global.anthropic.claude-sonnet-5", True, "anthropic.claude-sonnet-5"),
        ("apac.amazon.nova-pro-v1:0", True, "amazon.nova-pro-v1:0"),
        ("anthropic.claude-sonnet-5", False, "anthropic.claude-sonnet-5"),
        ("amazon.nova-pro-v1:0", False, "amazon.nova-pro-v1:0"),
    ],
)
def test_profile_ids_and_plain_model_ids_are_told_apart(model_id, profile, model):
    assert is_inference_profile(model_id) is profile
    assert foundation_model(model_id) == model
