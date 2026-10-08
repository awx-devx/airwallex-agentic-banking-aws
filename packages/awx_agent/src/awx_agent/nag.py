"""cdk-nag suppressions for agent-plane resources, each naming the exact finding it covers.

Every suppression is resource-level with `appliesTo`, so a NEW wildcard or managed policy on
the same resource is still reported. Account- and region-dependent ARNs are matched by an
anchored regex that pins every other part of the ARN.
"""

from __future__ import annotations

import re
from typing import Any

BASIC_EXECUTION = (
    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)
_ACCT = r"\d{12}"
_REGION = r"[a-z]{2}(-[a-z]+)+-\d"


def regex(pattern: str) -> dict[str, str]:
    """An anchored appliesTo regex (cdk-nag wants the /.../ form)."""
    re.compile(pattern)
    return {"regex": f"/^{pattern}$/"}


def suppress(construct: Any, findings: list[tuple[str, str, list[Any]]]) -> None:
    """findings: (rule id, reason, appliesTo list; [] means the whole rule, used only for L1)."""
    try:
        from cdk_nag import NagPackSuppression, NagSuppressions
    except ImportError:  # pragma: no cover - cdk-nag is an infra dependency
        return
    NagSuppressions.add_resource_suppressions(
        construct,
        [
            NagPackSuppression(id=rule, reason=reason, applies_to=applies or None)
            for rule, reason, applies in findings
        ],
        apply_to_children=True,
    )


def lambda_basics(fn: Any) -> list[tuple[str, str, list[Any]]]:
    return [
        (
            "AwsSolutions-IAM4",
            "AWSLambdaBasicExecutionRole only lets the function write its own logs.",
            [BASIC_EXECUTION],
        ),
        ("AwsSolutions-L1", "Python 3.12 is the pinned runtime for the whole codebase.", []),
    ]


ACCT = _ACCT
REGION = _REGION
