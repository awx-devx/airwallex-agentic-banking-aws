"""A toy kit spec that uses every KitWorkflow feature: fan_out with a child spec, a step
with a finality loop, a step without one, approval tiers and a postcondition."""

from __future__ import annotations

from datetime import timedelta

from awx_common import RiskTier
from awx_common.ops import ReadRequest
from awx_common.workflow import (
    FinalitySpec,
    KitWorkflowSpec,
    PolicyDecision,
    PostconditionResult,
    StepSpec,
)

_READS = (r"^/api/v1/transfers/[A-Za-z0-9_-]+$", r"^/api/v1/balances/current$")

CHILD = KitWorkflowSpec(
    kit="k6",
    action="pay_employer",
    validate=dict,
    policy=lambda intent, ctx: PolicyDecision(RiskTier.AUTO, ("child run",)),
    steps=(
        StepSpec(
            "fund",
            lambda ctx: (),
            FinalitySpec(
                read_request=lambda r: ReadRequest(f"/api/v1/transfers/{r.provider_id}"),
                status_of=lambda body: body["status"],
                terminal=frozenset({"PAID", "CANCELLED"}),
                success=frozenset({"PAID"}),
                interval_s=10,
                timeout_s=600,
            ),
        ),
        StepSpec("pay", lambda ctx: ()),
    ),
    approval_ttl=timedelta(hours=1),
    read_allowlist=_READS,
    platform_read_allowlist=(r"^/api/v1/transfers/[A-Za-z0-9_-]+$",),
    postcondition=lambda ctx: PostconditionResult(True, "ok"),
)

SPECS = {
    "pay_all": KitWorkflowSpec(
        kit="k6",
        action="pay_all",
        validate=dict,
        policy=lambda intent, ctx: PolicyDecision(RiskTier.DUAL, ("payroll run",)),
        steps=(),
        approval_ttl=timedelta(hours=4),
        read_allowlist=(r"^/api/v1/balances/current$",),
        fan_out=lambda intent, decision: [{"employer": e} for e in intent["employers"]],
        child=CHILD,
    ),
    "pay_employer": CHILD,
}
