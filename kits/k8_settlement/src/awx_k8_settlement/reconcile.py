"""reconcile_seller_money: read back every payout and recovery charge of this seller that is in
flight, and record any definitive outcome. It never sends an Airwallex write.

agent_min_tier is AUTO for this action only (justified in the README): the step emits no
Operation, the policy derives its whole computed list from the seller's own store, every outcome
it records comes from a provider read checked against the operation (amount, currency, party),
and under kit rule M1 the only outcomes it may record are definitive ones. An injected
instruction can at most make the agent record what Airwallex already shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common import RiskTier
from awx_common.errors import AwxError
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
    identity_problem,
    strict,
)
from .operations import (
    KIT,
    PLATFORM_READS,
    TENANT_READS,
    Outcome,
    build_payout_op,
    build_recovery_op,
    person_reason,
    resolve,
    within_dedup_window,
)
from .recover import apply_charge_outcome
from .settle import apply_payout_outcome

SETTLED_OR_FAILED = frozenset(
    {Outcome.SETTLED, Outcome.FAILED, Outcome.NOT_CREATED, Outcome.INSUFFICIENT_FUNDS}
)


def validate_reconcile(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, ())


def make_reconcile() -> KitWorkflowSpec:
    def policy_fn(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        if (bad := account_or_block(ctx)) is not None:
            return bad
        kstore, seller = ctx.store, ctx.tenant.tenant_id
        try:
            st.book_id(kstore)
            state = st.ledger_state(kstore, seller, st.profile(kstore).currency)
            payouts = list(st.unresolved_payouts(state))
            charges = [
                a.attempt for a in st.recovery_attempts(kstore) if a.outcome in st.PENDING_RECOVERY
            ]
            watches = [f"{w['kind']}/{w['ref']}" for w in wt.open_watches(kstore)]
        except (AwxError, ValueError) as exc:
            return blocked(f"cannot reconcile: {exc}")
        if not payouts and not charges and not watches:
            return blocked("nothing is in flight or watched for this seller")
        reason = (
            f"read back {len(payouts)} payout(s) and {len(charges)} recovery charge(s) "
            f"in flight, and {len(watches)} not_landed decision(s) on watch; sends nothing"
        )
        computed = {"payouts": payouts, "charges": charges, "watches": watches}
        return PolicyDecision(RiskTier.AUTO, (reason,), computed)

    def build(ctx: StepContext) -> tuple:
        return ()  # read-only: the postcondition does the reads

    def postcondition(ctx: StepContext) -> PostconditionResult:
        kstore = st.kit_store(ctx.policy)
        tenant = ctx.policy.tenant
        book = st.book_id(kstore)
        currency = st.profile(kstore).currency
        lines, still = [], 0
        for cycle_id in ctx.decision.computed["payouts"]:
            state = st.ledger_state(kstore, tenant.tenant_id, currency)
            settlement = st.settlement(state, cycle_id)
            if settlement is None or st.payout_outcome(state, cycle_id) is not None:
                continue
            op = build_payout_op(settlement, tenant, book_id=book, intent_hash=ctx.intent_hash)
            outcome, _ = resolve(op, None, ctx.policy.read_platform)
            person = (
                person_reason(op, None, ctx.policy.read_platform)
                if outcome is Outcome.UNKNOWN
                else None
            )
            lines.append(
                f"{cycle_id}: "
                + apply_payout_outcome(kstore, tenant, settlement, outcome, person=person)
            )
            still += _escalation(kstore, "payout", cycle_id, outcome, ctx, lines)
        attempts = {a.attempt: a for a in st.recovery_attempts(kstore)}
        for n in ctx.decision.computed["charges"]:
            a = attempts.get(n)
            if a is None or a.outcome not in st.PENDING_RECOVERY:
                continue
            op = build_recovery_op(n, a.amount, tenant, book_id=book, intent_hash=ctx.intent_hash)
            outcome, _ = resolve(op, None, ctx.policy.read_platform)
            person = (
                person_reason(op, None, ctx.policy.read_platform)
                if outcome is Outcome.UNKNOWN
                else None
            )
            lines.append(
                f"charge {n}: "
                + apply_charge_outcome(kstore, tenant, n, a.amount, outcome, person=person)[1]
            )
            still += _escalation(kstore, "charge", str(n), outcome, ctx, lines)
        still += _check_watches(kstore, ctx, lines)
        fresh = st.ledger_state(kstore, tenant.tenant_id, currency)
        if (problem := identity_problem(fresh, None)) is not None:
            return PostconditionResult(False, problem)
        if still:
            lines.append(f"{still} item{'s' if still > 1 else ''} still in flight")
        return PostconditionResult(still == 0, "; ".join(lines) or "nothing left in flight")

    return KitWorkflowSpec(
        approver=ApproverScope.OWN_TENANT,  # the seller's own approver
        kit=KIT,
        action="reconcile_seller_money",
        validate=validate_reconcile,
        policy=policy_fn,
        steps=(StepSpec("read_back", build),),
        approval_ttl=APPROVAL_TTL,
        read_allowlist=TENANT_READS,
        platform_read_allowlist=PLATFORM_READS,
        postcondition=postcondition,
        agent_min_tier=RiskTier.AUTO,  # sends nothing; see the module docstring and README
    )


def _check_watches(kstore, ctx: StepContext, lines: list[str]) -> int:
    """Read every watched not_landed decision back.
    Read-only; no window limit. Returns how many need a person."""
    tenant, problems, now = ctx.policy.tenant, 0, ctx.policy.now
    read = ctx.policy.read_platform
    currency = st.profile(kstore).currency
    for w in wt.open_watches(kstore):
        if f"{w['kind']}/{w['ref']}" not in ctx.decision.computed.get("watches", ()):
            continue
        tag = f"{w['kind']} {w['ref']}"
        who = f"ruled not_landed by proposal {w['owner_proposal_id']}"
        applied = wt.decision_applied(kstore, w)
        if applied == "missing":
            # The decision's run died before its ledger move: the money is still in flight
            # and the in-flight read-back above handles it. The watch stays open.
            lines.append(f"{tag}: decision recorded, ledger move pending; still in flight")
            problems += 1
            continue
        if applied == "superseded":
            wt.close(kstore, w, "superseded")
            continue
        seen, status = wt.look(kstore, tenant, read, w)
        if seen == "settled":
            if w["kind"] == "payout":
                state = st.ledger_state(kstore, tenant.tenant_id, currency)
                apply_payout_outcome(
                    kstore, tenant, st.settlement(state, w["ref"]), Outcome.SETTLED
                )
            else:
                apply_charge_outcome(kstore, tenant, int(w["ref"]), w["amount"], Outcome.SETTLED)
            wt.close(kstore, w, "found")
            reason = (
                f"LANDED after all ({w['request_id']}), {who}: posted as a late "
                "landing; check for a double payment or charge"
            )
            st.flag_attention(kstore, w["kind"], w["ref"], reason)
            lines.append(f"{tag}: NEEDS_PERSON, {reason}")
            problems += 1
        elif seen == "failed":
            wt.close(kstore, w, "confirmed_failed")
            st.clear_attention(kstore, w["kind"], w["ref"])
            lines.append(f"{tag}: Airwallex shows its own failure state ({status}); watch closed")
        elif seen in ("open", "undetermined"):
            reason = (
                f"shows at Airwallex as {status or seen} under {w['request_id']}, {who}; "
                "nothing posted, settle and recover held"
            )
            st.flag_attention(kstore, w["kind"], w["ref"], reason)
            lines.append(f"{tag}: NEEDS_PERSON, {reason}")
            problems += 1
        elif not w.get("confirmed_at") and now - w["decided_at"] >= wt.CONFIRM_AFTER:
            # A second clean read, a day on: empty by request_id AND by counterparty.
            if why := wt.counterparty_match(kstore, tenant, read, w, now):
                st.flag_attention(kstore, w["kind"], w["ref"], f"{why}, {who}")
                lines.append(f"{tag}: NEEDS_PERSON, {why}")
                problems += 1
            else:
                wt.update(kstore, w, confirmed_at=now)
                lines.append(f"{tag}: read empty a day after the decision; watch confirmed")
    return problems


def _escalation(kstore, kind: str, ref: str, outcome: Outcome, ctx: StepContext, lines) -> int:
    """1 for any item still in flight (whether or not a person is flagged), so each item is
    counted once."""
    if outcome in SETTLED_OR_FAILED:
        return 0
    sent = st.first_sent_at(kstore, kind, ref)
    if sent is not None and not within_dedup_window(sent, ctx.policy.now):
        lines.append(
            f"{kind} {ref}: NEEDS_PERSON, past the deduplication window; nothing sent. "
            "Use propose_resolve_in_flight_money with Airwallex's evidence"
        )
        st.flag_attention(kstore, kind, ref, "in flight past the deduplication window")
    return 1
