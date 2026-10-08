"""settle_seller_cycle: pay one seller for one cycle and resolve the payout to an outcome.

The build posts the cycle (payout_pending), records when the money first left, and returns the
payout. The postcondition always runs (rule 2) and reads the payout back. Kit rule M1: only a
definitive outcome moves the money out of payout_pending. SETTLED is posted paid. A FAILED
create, or the provider's own FAILED state, is posted payable (re-sent next cycle under that
cycle's request_id). Anything else - an empty read-back, OUTCOME_UNKNOWN, NEW, PENDING,
SUSPENDED - leaves it in flight on its ORIGINAL request_id, and later cycles never include it.

Proposing the same cycle again while its payout is in flight is a resend: the build reads back
by request_id first and re-emits the identical Operation only if nothing is found, inside the
provider's deduplication window. Past the window a person decides and nothing is sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common import RiskTier, TenantContext, canonical_json
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
from . import watch as wt
from .common import (
    APPROVAL_TTL,
    account_or_block,
    blocked,
    compute_settlement,
    identity_problem,
    strict,
    tier,
)
from .ledger import CycleSettlement
from .operations import (
    DEFINITIVE_FAILURES,
    KIT,
    PAYOUT_FINALITY,
    PLATFORM_READS,
    TENANT_READS,
    Outcome,
    build_payout_op,
    find_by_request_id,
    payout_request_id,
    person_reason,
    resend_outcome,
    resolve,
    within_dedup_window,
)
from .reserve import ReservePolicy, is_material_increase, needs_dual, reserve_increase

DEFINITIVE = frozenset(
    {Outcome.SETTLED, Outcome.FAILED, Outcome.NOT_CREATED, Outcome.INSUFFICIENT_FUNDS}
)


def validate_settle(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, ("cycle_id",))


def apply_payout_outcome(
    kstore: KitStore,
    tenant: TenantContext,
    settlement: CycleSettlement,
    outcome: Outcome,
    *,
    person: str | None = None,
) -> str:
    """Post a payout's outcome once. Only a definitive outcome moves money (M1)."""
    seller, cycle_id, amount = tenant.tenant_id, settlement.cycle_id, settlement.payout
    state = st.ledger_state(kstore, seller, settlement.currency)
    prior = st.payout_outcome(state, cycle_id)
    led = state.ledger
    if outcome in DEFINITIVE:
        st.clear_attention(kstore, "payout", cycle_id)
    if outcome is Outcome.SETTLED:
        if prior is None:
            st.post(
                kstore,
                state,
                f"payout-outcome:{cycle_id}",
                led.payout_settled(amount),
                {"outcome": "SETTLED"},
            )
        elif prior in DEFINITIVE_FAILURES:
            # A payout posted failed WITHOUT Airwallex's own failure state: a person's
            # not_landed decision (resolve_in_flight_money). It turned up after all, found by
            # reconcile's watch. The payable is cleared, and any amount already re-sent becomes
            # a receivable; reconcile flags it for a person. No automatic path posts a failure
            # without a definitive one.
            st.post(
                kstore,
                state,
                f"payout-late:{cycle_id}",
                led.payout_found_after_failed(amount),
                {"outcome": "SETTLED"},
            )
        return "payout SETTLED"
    if outcome in DEFINITIVE:
        if prior is None:
            st.post(
                kstore,
                state,
                f"payout-outcome:{cycle_id}",
                led.payout_failed(amount),
                {"outcome": outcome.value},
            )
        return f"payout {outcome.value}; {amount} is payable and re-sent next cycle"
    rid = payout_request_id(st.book_id(kstore), seller, cycle_id)
    if outcome is Outcome.NEEDS_PERSON:
        st.flag_attention(kstore, "payout", cycle_id, f"SUSPENDED at Airwallex ({rid})")
        return (
            f"payout SUSPENDED by Airwallex: operator action required; {amount} stays in "
            f"flight on request_id {rid}"
        )
    if person:
        st.flag_attention(kstore, "payout", cycle_id, person)
        return (
            f"payout {outcome.value}; {amount} stays in flight on request_id {rid}; needs "
            f"a person: {person}. Nothing will be re-sent"
        )
    return (
        f"payout {outcome.value}; {amount} stays in flight on request_id {rid}. "
        "reconcile_seller_money reads it back; re-proposing the same cycle re-emits the "
        "identical request_id only if Airwallex has no record of it"
    )


def _resend_decision(kstore: KitStore, state: st.LedgerState, cycle_id: str, now) -> PolicyDecision:
    existing = st.settlement(state, cycle_id)
    assert existing is not None
    if existing.payout.is_zero() or st.payout_outcome(state, cycle_id) is not None:
        return blocked("this cycle is already settled for this seller")
    sent = st.first_sent_at(kstore, "payout", cycle_id)
    if sent is not None and not within_dedup_window(sent, now):
        return blocked(
            f"needs a person: the {existing.payout} payout for {cycle_id} has been in flight "
            "past the request_id deduplication window; nothing will be sent. Use "
            "propose_resolve_in_flight_money with Airwallex's evidence"
        )
    return PolicyDecision(
        RiskTier.AUTO,
        (
            f"read back the {existing.payout} payout for {cycle_id}; re-send the identical "
            "operation (same request_id) only if Airwallex has no record of it",
        ),
        {
            "mode": "resend",
            "payout": existing.payout,
            "request_id": payout_request_id(st.book_id(kstore), state.ledger.seller, cycle_id),
        },
    )


def make_settle(policy: ReservePolicy) -> KitWorkflowSpec:
    def policy_fn(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        if (bad := account_or_block(ctx)) is not None:
            return bad
        kstore, seller, cycle_id = ctx.store, ctx.tenant.tenant_id, intent["cycle_id"]
        try:
            st.book_id(kstore)  # rule M2: no book id, no money movement
            state = st.ledger_state(kstore, seller, st.profile(kstore).currency)
            if (problem := identity_problem(state, None)) is not None:
                return blocked(f"seller ledger does not balance ({problem})")
            if st.settlement(state, cycle_id) is not None:
                return _resend_decision(kstore, state, cycle_id, ctx.now)
            settlement, _ = compute_settlement(kstore, state, policy, seller, cycle_id)
            hold = wt.gate(kstore, ctx.tenant, ctx.read_platform, "payout", ctx.now)
            # Netting would take a receivable a watched charge may
            # already have collected, so a charge watch that shows also holds settlement.
            hold = hold or wt.shows_hold(kstore, ctx.tenant, ctx.read_platform, "charge")
            watched = wt.open_watches(kstore)
        except (AwxError, ValueError) as exc:
            return blocked(f"cannot settle: {exc}")
        if hold:  # never re-pay on a doubt
            return blocked(f"hold: {hold}")
        increase = reserve_increase(settlement.reserve_before, settlement.reserve_target)
        material = is_material_increase(
            policy, settlement.reserve_before, settlement.reserve_target
        )
        reasons = [
            f"payout {settlement.payout}; reserve {settlement.reserve_before} -> "
            f"{settlement.reserve_after}"
        ]
        if watched:
            reasons.append(
                f"watching {len(watched)} not_landed decision(s): " + wt.describe(watched)
            )
        if held := st.needs_person(kstore):
            reasons.append(
                f"{len(held)} item(s) need an operator: "
                + "; ".join(f"{h['kind']} {h['ref']}: {h['reason']}" for h in held)
            )
        if not state.ledger.payout_pending.is_zero():
            reasons.append(
                f"excludes {state.ledger.payout_pending} still in flight on earlier "
                "payouts' own request_ids"
            )
        if material:
            reasons.append(f"material reserve increase of {increase} withheld from the seller")
        if not settlement.receivable_offset.is_zero():
            material = True
            reasons.append(f"nets {settlement.receivable_offset} owed by the seller off the payout")
        if not settlement.shortfall.is_zero():
            material = True
            reasons.append(f"refunds exceed cash and reserve: {settlement.shortfall} receivable")
        if not settlement.unmet_reserve.is_zero():
            reasons.append(f"reserve target short by {settlement.unmet_reserve} this cycle")
        dual = needs_dual(policy, settlement.payout, increase, settlement.receivable_offset)
        return PolicyDecision(
            tier(material, dual),
            tuple(reasons),
            {"mode": "settle", "settlement": settlement.to_record()},
        )

    def build(ctx: StepContext) -> tuple:
        kstore = st.kit_store(ctx.policy)
        tenant = ctx.policy.tenant
        seller, cycle_id = tenant.tenant_id, ctx.intent["cycle_id"]
        state = st.ledger_state(kstore, seller, st.profile(kstore).currency)
        settlement = st.settlement(state, cycle_id)
        if ctx.decision.computed["mode"] == "resend":
            if settlement is None or st.payout_outcome(state, cycle_id) is not None:
                return ()
            # Identical body and request_id; the op carries THIS run's proposal hash, which the
            # engine binds every operation to (rule 9).
            original = ctx.intent_hash
            op = build_payout_op(
                settlement, tenant, book_id=st.book_id(kstore), intent_hash=original
            )
            sent = st.first_sent_at(kstore, "payout", cycle_id)
            if sent is not None and not within_dedup_window(sent, ctx.policy.now):
                raise StateConflict("past the deduplication window; a person must decide")
            # Reconcile first (M1). ReconcileUndetermined propagates: fail closed, send nothing.
            if find_by_request_id(op, ctx.policy.read_platform) is not None:
                return ()  # it landed; the postcondition reads and posts it
            st.mark_sent(
                kstore,
                "payout",
                cycle_id,
                op.request_id,
                ctx.policy.now,
                op.intent_hash,
                ctx.proposal_id,
            )
            return (op,)  # the identical Operation: same request_id, same body
        approved = canonical_json(ctx.decision.computed["settlement"])
        if settlement is None:
            settlement, new = compute_settlement(kstore, state, policy, seller, cycle_id)
            if canonical_json(settlement.to_record()) != approved:
                raise StateConflict("ledger or cycle changed since approval; propose again")
            st.post(kstore, state, f"settle:{cycle_id}", new, settlement.to_record())
        elif canonical_json(settlement.to_record()) != approved:
            raise StateConflict("cycle was settled by a different proposal")
        if settlement.payout.is_zero():
            return ()
        op = build_payout_op(
            settlement, tenant, book_id=st.book_id(kstore), intent_hash=ctx.intent_hash
        )
        st.mark_sent(
            kstore,
            "payout",
            cycle_id,
            op.request_id,
            ctx.policy.now,
            op.intent_hash,
            ctx.proposal_id,
        )
        return (op,)

    def postcondition(ctx: StepContext) -> PostconditionResult:
        kstore = st.kit_store(ctx.policy)
        tenant = ctx.policy.tenant
        seller, cycle_id = tenant.tenant_id, ctx.intent["cycle_id"]
        currency = st.profile(kstore).currency
        state = st.ledger_state(kstore, seller, currency)
        settlement = st.settlement(state, cycle_id)
        if settlement is None:
            return PostconditionResult(False, "cycle not posted; nothing was sent")
        if settlement.payout.is_zero():
            problem = identity_problem(state, settlement)
            return PostconditionResult(problem is None, problem or "no payout due; balanced")
        op = build_payout_op(
            settlement, tenant, book_id=st.book_id(kstore), intent_hash=ctx.intent_hash
        )
        results = ctx.prior.get("payout", ())
        outcome, _ = resolve(op, results[0] if results else None, ctx.policy.read_platform)
        if ctx.decision.computed["mode"] == "resend":
            outcome = resend_outcome(outcome)
        person = None
        if outcome is Outcome.UNKNOWN:
            person = person_reason(op, results[0] if results else None, ctx.policy.read_platform)
        detail = apply_payout_outcome(kstore, tenant, settlement, outcome, person=person)
        fresh = st.ledger_state(kstore, seller, currency)
        if (problem := identity_problem(fresh, settlement)) is not None:
            return PostconditionResult(False, problem)
        return PostconditionResult(
            outcome is Outcome.SETTLED, f"{detail}; cycle identity balances to zero"
        )

    return KitWorkflowSpec(
        approver=ApproverScope.KIT_OPERATOR,  # the kit 8 operator approves
        kit=KIT,
        action="settle_seller_cycle",
        validate=validate_settle,
        policy=policy_fn,
        steps=(StepSpec("payout", build, PAYOUT_FINALITY, find_by_request_id),),
        approval_ttl=APPROVAL_TTL,
        read_allowlist=TENANT_READS,
        platform_read_allowlist=PLATFORM_READS,
        postcondition=postcondition,
    )


def in_flight_payouts(state: st.LedgerState) -> tuple[CycleSettlement, ...]:
    return tuple(st.settlement(state, cid) for cid in st.unresolved_payouts(state))
