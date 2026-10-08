"""recalculate_seller_reserve: raise ONE seller's reserve from carrier evidence.

Evidence is read from the seller's own KitStore index, so another seller's event is not even
reachable; the event's `seller` field is checked as well. Exposure counts only this seller's
undelivered, unrefunded orders. The approved hold carries into later cycles and shrinks as those
orders deliver or refund (common.planned_target).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common import RiskTier, canonical_json
from awx_common.errors import AwxError, StateConflict
from awx_common.store import KitStore
from awx_common.workflow import (
    ApproverScope,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    StepContext,
    StepSpec,
)

from . import store as st
from .common import (
    APPROVAL_TTL,
    account_or_block,
    blocked,
    planned_target,
    strict,
    tier,
)
from .evidence import incident_exposure, parse_carrier_event
from .operations import KIT, TENANT_READS
from .reserve import (
    ReservePolicy,
    incident_target,
    is_material_increase,
    needs_dual,
    reserve_increase,
)


def validate_recalculate(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, ("cycle_id", "carrier_event_id"))


def make_recalculate(policy: ReservePolicy) -> KitWorkflowSpec:
    def evaluate(kstore: KitStore, seller: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        event = parse_carrier_event(st.carrier_event(kstore, intent["carrier_event_id"]))
        prof = st.profile(kstore)
        # Exclude this event's own hold, so a re-run after the build recorded it computes the
        # same numbers (rule 1) instead of seeing its own increase as "before".
        _, before = planned_target(
            kstore, policy, seller, intent["cycle_id"], exclude_event=event.event_id
        )
        exposure = incident_exposure(
            event, st.orders(kstore, seller), seller=seller, currency=prof.currency
        )
        return {
            "event_id": event.event_id,
            "before": before,
            "target": incident_target(before, exposure.amount),
            "exposure": exposure.amount,
            "matched": list(exposure.matched),
            "unmatched": list(exposure.unmatched),
        }

    def policy_fn(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        if (bad := account_or_block(ctx)) is not None:
            return bad
        kstore, seller = ctx.store, ctx.tenant.tenant_id
        try:
            state = st.ledger_state(kstore, seller, st.profile(kstore).currency)
            if st.settlement(state, intent["cycle_id"]) is not None:
                return blocked("cycle already settled; name the next cycle instead")
            computed = evaluate(kstore, seller, intent)
        except (AwxError, ValueError) as exc:
            # TenantScopeViolation (evidence naming another seller) and NotFound land here.
            return blocked(f"cannot use this evidence: {exc}")
        before, target = computed["before"], computed["target"]
        increase = reserve_increase(before, target)
        if increase.is_zero():
            reasons = (f"evidenced exposure {computed['exposure']} is within reserve {before}",)
            return PolicyDecision(RiskTier.AUTO, reasons, computed)
        reasons = (
            f"reserve {before} -> {target} on {len(computed['matched'])} undelivered orders",
            f"{len(computed['unmatched'])} named orders are not in this seller's book",
            "the hold carries into later cycles and shrinks as these orders deliver or refund",
        )
        material = is_material_increase(policy, before, target)
        return PolicyDecision(tier(material, needs_dual(policy, increase)), reasons, computed)

    def build(ctx: StepContext) -> tuple:
        kstore = st.kit_store(ctx.policy)
        seller = ctx.policy.tenant.tenant_id
        computed = ctx.decision.computed
        if canonical_json(evaluate(kstore, seller, ctx.intent)) != canonical_json(computed):
            raise StateConflict("evidence, orders or reserve changed since approval")
        if not reserve_increase(computed["before"], computed["target"]).is_zero():
            st.put_override(
                kstore,
                st.ReserveOverride(
                    event_id=computed["event_id"],
                    cycle_id=ctx.intent["cycle_id"],
                    target=computed["target"],
                    order_ids=tuple(computed["matched"]),
                    owner_proposal_id=ctx.proposal_id,  # rule M4
                ),
            )
        return ()  # bookkeeping only: the next settlement withholds the reserve

    def postcondition(ctx: StepContext) -> PostconditionResult:
        _, effective = planned_target(
            st.kit_store(ctx.policy), policy, ctx.policy.tenant.tenant_id, ctx.intent["cycle_id"]
        )
        ok = effective >= ctx.decision.computed["target"]
        return PostconditionResult(ok, f"effective reserve target {effective}")

    return KitWorkflowSpec(
        approver=ApproverScope.KIT_OPERATOR,  # the kit 8 operator approves
        kit=KIT,
        action="recalculate_seller_reserve",
        validate=validate_recalculate,
        policy=policy_fn,
        steps=(StepSpec("record_override", build),),
        approval_ttl=APPROVAL_TTL,
        read_allowlist=TENANT_READS,
        postcondition=postcondition,
    )
