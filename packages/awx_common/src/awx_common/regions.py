"""Deploy Region and Amazon Bedrock model defaults, defined once for infra/ and the packages.

`DEFAULT_REGION` is the AWS Region a deployment targets when none is given (`make deploy` with
no REGION, `awx-deploy` with no `--region` and no outputs file, the Runtime's local fallback).

The agent's models are Claude Sonnet 5 (primary) and Claude Sonnet 4.5 (fallback). Their
default IDs are the geographic cross-Region inference profiles of the deploy Region. The
prefix comes from `INFERENCE_PROFILE_PREFIX`. That table lists only Regions that offer Amazon
Bedrock AgentCore Runtime and where `aws bedrock list-inference-profiles` returned the
primary and fallback profile IDs on 13 Oct 2026. A Region outside the table needs the model
IDs passed explicitly (MODEL_ID and FALLBACK_MODEL_ID).
"""

from __future__ import annotations

DEFAULT_REGION = "us-west-2"

# A Region code: lower-case letters and hyphens ending in a number (us-west-2, ap-southeast-2).
# The Makefile's require-region target uses the same pattern (a test pins them together).
REGION_CODE = r"^[a-z]{2}(-[a-z]+)+-[0-9]+$"
REGION_CODE_MESSAGE = "REGION={region} is not an AWS Region code such as us-west-2"

# Region code prefixes of the partitions other than the commercial `aws` partition (China,
# AWS GovCloud (US) and the isolated Regions). Their Amazon Resource Names (ARNs) use another
# partition name, and the stacks build every ARN with `aws`.
OTHER_PARTITION_PREFIXES = ("cn-", "us-gov-", "us-iso-", "us-isob-", "us-isof-", "eu-isoe-")
OTHER_PARTITION_MESSAGE = (
    "REGION={region} is not in the commercial AWS partition (aws). "
    "This app supports only that partition"
)

PRIMARY_MODEL = "anthropic.claude-sonnet-5"
FALLBACK_MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"

# Deploy Region -> geographic inference profile prefix of the primary and fallback models.
INFERENCE_PROFILE_PREFIX: dict[str, str] = {
    "us-east-1": "us",
    "us-east-2": "us",
    "us-west-1": "us",
    "us-west-2": "us",
    "ca-central-1": "us",
    "eu-central-1": "eu",
    "eu-north-1": "eu",
    "eu-west-1": "eu",
    "eu-west-2": "eu",
    "eu-west-3": "eu",
    "ap-southeast-2": "au",
}

# First segment of a system-defined cross-Region inference profile ID. A model ID that starts
# with one of these is a profile; any other model ID is a plain foundation-model ID.
PROFILE_PREFIXES = frozenset(
    {
        "us",
        "us-gov",
        "eu",
        "apac",
        "au",
        "jp",
        "ca",
        "in",
        "global",
        *INFERENCE_PROFILE_PREFIX.values(),
    }
)


class UnsupportedRegionError(ValueError):
    """The Region has no default model IDs; MODEL_ID and FALLBACK_MODEL_ID must be given."""


def default_model_ids(region: str) -> tuple[str, str]:
    """(primary, fallback) inference profile IDs for a deploy Region in the table."""
    prefix = INFERENCE_PROFILE_PREFIX.get(region)
    if prefix is None:
        raise UnsupportedRegionError(
            f"REGION={region} has no default model IDs (Regions with defaults: "
            f"{', '.join(sorted(INFERENCE_PROFILE_PREFIX))}). Pass MODEL_ID and "
            "FALLBACK_MODEL_ID (CDK context model_id and fallback_model_id) set to model or "
            "inference profile IDs that this Region offers."
        )
    return f"{prefix}.{PRIMARY_MODEL}", f"{prefix}.{FALLBACK_MODEL}"


def is_inference_profile(model_id: str) -> bool:
    """True for a geographic or global inference profile ID such as `us.anthropic.x`."""
    head, sep, _ = model_id.partition(".")
    return bool(sep) and head in PROFILE_PREFIXES


def foundation_model(model_id: str) -> str:
    """The foundation-model ID behind a model ID: `us.anthropic.x` -> `anthropic.x`."""
    return model_id.split(".", 1)[1] if is_inference_profile(model_id) else model_id
