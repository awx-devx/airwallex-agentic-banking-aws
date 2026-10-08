"""propose_release_run_claim: the person-approved release of a stuck run claim.

A run claim (one run per employer and pay period) is released by its
owner's postcondition. A run that dies before its postcondition leaves it held, and every later
payroll for that employer and period is BLOCKED ("another run of this period is in flight").

Releasing a claim moves no money and changes no attempt: money the dead run left in flight
stays on its request_id (rule M1). The only risk is releasing a run that can still send, so
the release is gated on the claim's ROOT payroll run: each employer child records
`root_proposal_id` and `root_action` in its claim, and this tool asks
`run_status(root_proposal_id, root_action)` as the operator tenant.

- a member of RUN_TERMINAL: SINGLE, a person approves; re-checked at apply;
- RUN_RUNNING: BLOCKED;
- None (no such run: purged after Step Functions' 90 days, or a local run with no
  registry): BLOCKED;
- the callable itself is None (the tool plane, at propose): decided at recheck (SINGLE
  now; BLOCKED, so STALE, if it is not terminal then);
- the claim names no root: BLOCKED.

run_status reads a deployed root terminal only once it can send nothing more: after it
stops plus the adapter wait (195 s), and, for an abnormal end, plus the child machine
timeout. A refused lookup (a child's id, another tenant's proposal) raises, so the run fails
closed. Where run_status cannot help (None), a person with the evidence works in the store
directly (DESIGN.md). The approval binds the owner, the time it was taken and the root run, so
a claim that moved makes the run STALE. Never below SINGLE.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from awx_common import Operation, RiskTier
from awx_common.workflow import (
    RUN_TERMINAL,
    ApproverScope,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    StepContext,
    StepSpec,
)

from .books import force_release_claim, read_claim
from .model import KIT, PayrollInputError, parse_pay_period
from .model import _code as parse_code
from .workflow import keys_for, require_operator

ACTION = "release_run_claim"


def validate(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"employer", "pay_period", "reason"}:
        raise PayrollInputError("a release needs employer, pay_period and reason")
    reason = raw["reason"]
    if not isinstance(reason, str) or not 1 <= len(reason) <= 300:
        raise PayrollInputError("reason must be 1 to 300 characters")
    return {
        "employer": parse_code(raw["employer"], "employer"),
        "pay_period": parse_pay_period(raw["pay_period"]),
        "reason": reason,  # recorded only; never decides anything
    }


def _root_status(ctx: PolicyContext, claim: Mapping[str, Any]) -> tuple[bool, str]:
    """(may release, why not). A None callable defers to recheck."""
    root, action = claim.get("root_proposal_id"), claim.get("root_action")
    if not root or not action:
        return False, "the claim names no root payroll run, so its status cannot be read"
    if ctx.run_status is None:
        return True, ""
    status = ctx.run_status(str(root), str(action))  # raises on a child id or another tenant
    if status is None:
        return False, (
            f"no root run {root} can be found (purged, or never deployed); a person with the "
            "evidence must work in the store directly"
        )
    if status not in RUN_TERMINAL:
        return False, f"root payroll run {root} is still {status}; it may still send"
    return True, ""


def policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    require_operator(ctx.tenant)
    k = keys_for(ctx.platform_store, intent)
    claim = read_claim(ctx.platform_store, k)
    who = f"employer {intent['employer']}, pay period {intent['pay_period']}"
    if claim is None:
        return PolicyDecision(RiskTier.BLOCKED, (f"{who}: no run claim is held",))
    ok, why = _root_status(ctx, claim)
    if not ok:
        return PolicyDecision(RiskTier.BLOCKED, (f"{who}: {why}",))
    # The same reasons and computed whether the status was read now or is read at recheck,
    # so rule 1's re-run hash moves only if the status does (then BLOCKED, so STALE).
    return PolicyDecision(
        RiskTier.SINGLE,
        (
            f"{who}: release the claim of proposal {claim['owner']} taken at "
            f"{claim['taken_at'].isoformat()}, once its root payroll run "
            f"{claim['root_proposal_id']} reads terminal; money it left in flight stays on "
            "its request_id",
            f"operator reason: {intent['reason']}",
        ),
        {
            "owner": claim["owner"],
            "taken_at": claim["taken_at"],
            "root_proposal_id": claim["root_proposal_id"],
            "root_action": claim["root_action"],
        },
    )


class RootStillRunning(Exception):
    """At apply the root run did not read terminal; nothing released."""


def build_release(sctx: StepContext) -> Sequence[Operation]:
    """Writes the store only; releases only the exact claim the approval names, and only
    while its root run still reads terminal (re-checked at apply)."""
    k = keys_for(sctx.policy.platform_store, sctx.intent)
    if k is None:
        raise PayrollInputError("no book")
    c = sctx.decision.computed
    if sctx.policy.run_status is None:
        raise RootStillRunning("no run status at apply; nothing released")
    status = sctx.policy.run_status(c["root_proposal_id"], c["root_action"])
    if status not in RUN_TERMINAL:
        raise RootStillRunning(f"root payroll run reads {status}; nothing released")
    force_release_claim(
        sctx.policy.platform_store, k, owner=c["owner"], taken_at=c["taken_at"], by=sctx.proposal_id
    )
    return ()


def postcondition(sctx: StepContext) -> PostconditionResult:
    k = keys_for(sctx.policy.platform_store, sctx.intent)
    claim = read_claim(sctx.policy.platform_store, k)
    if claim is not None and claim["owner"] == sctx.decision.computed["owner"]:
        return PostconditionResult(False, "the claim is still held")
    return PostconditionResult(True, f"released the claim of {sctx.decision.computed['owner']}")


RELEASE_SPEC = KitWorkflowSpec(
    approver=ApproverScope.OWN_TENANT,  # the operator tenant's own approvers
    kit=KIT,
    action=ACTION,
    validate=validate,
    policy=policy,
    steps=(StepSpec("release", build_release),),
    approval_ttl=timedelta(hours=1),
    read_allowlist=(),
    postcondition=postcondition,
)
