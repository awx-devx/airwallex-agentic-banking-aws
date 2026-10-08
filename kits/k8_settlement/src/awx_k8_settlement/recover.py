"""recover_shortfall: collect a seller's receivable, but only when their wallet covers it.

Every new charge needs a human approval. The wallet is read before (policy) and again just
before the charge (build); if it is short, no charge is made and the shortfall stays a
receivable. A charge rejected for funds is recorded and never retried; another attempt is a new
proposal with a new attempt number (its own request_id), and only after a definitive
outcome.

Kit rule M1: a charge with no definitive outcome (OUTCOME_UNKNOWN, an empty read-back, NEW,
PENDING, SUSPENDED) stays PENDING on its ORIGINAL request_id, the receivable is not cleared and
cannot be netted off a payout. Proposing recover again then resends: it reads back by
request_id first and re-emits the identical charge only if nothing is found, inside the
provider's deduplication window. Past the window a person decides and nothing is sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common import Money, RiskTier, TenantContext
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
    identity_problem,
    strict,
)
from .operations import (
    CHARGE_FINALITY,
    KIT,
    PLATFORM_READS,
    TENANT_READS,
    Outcome,
    available_balance,
    build_recovery_op,
    find_by_request_id,
    person_reason,
    resend_outcome,
    resolve,
    seller_balance_request,
    within_dedup_window,
)
from .reserve import ReservePolicy, needs_dual


def validate_recover(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, ())


def apply_charge_outcome(
    kstore: KitStore,
    tenant: TenantContext,
    attempt: int,
    amount: Money,
    outcome: Outcome,
    *,
    person: str | None = None,
) -> tuple[bool, str]:
    """Record a charge's outcome once. Only a definitive outcome changes the receivable (M1)."""
    rid = build_recovery_op(
        attempt, amount, tenant, book_id=st.book_id(kstore), intent_hash="0" * 64
    ).request_id
    if outcome in (
        Outcome.SETTLED,
        Outcome.FAILED,
        Outcome.NOT_CREATED,
        Outcome.INSUFFICIENT_FUNDS,
    ):
        st.clear_attention(kstore, "charge", str(attempt))
    if outcome is Outcome.SETTLED:
        settled = st.RecoveryAttempt(attempt, amount, rid, "SETTLED")
        for _ in range(5):  # the receivable and the claim's outcome move in ONE write
            state = st.ledger_state(kstore, tenant.tenant_id, amount.currency)
            try:
                # Equals recovery_settled when the receivable covers the charge; any
                # over-collection becomes payable instead of raising (defence in depth only:
                # the claim and settlement netting are serialised by the ledger version).
                posted = st.post(
                    kstore,
                    state,
                    f"recovered:{attempt}",
                    state.ledger.recovery_found_after_failed(amount),
                    {"outcome": "SETTLED"},
                    recovery=settled,
                )
            except StateConflict:
                continue
            if not posted:
                st.put_recovery_attempt(kstore, settled)
            return True, f"recovered {amount}"
        raise StateConflict("ledger kept changing while posting a recovery")
    if outcome in (Outcome.FAILED, Outcome.NOT_CREATED, Outcome.INSUFFICIENT_FUNDS):
        final = "INSUFFICIENT_FUNDS" if outcome is Outcome.INSUFFICIENT_FUNDS else "FAILED"
        st.put_recovery_attempt(kstore, st.RecoveryAttempt(attempt, amount, rid, final))
        return False, f"recovery {final}; the shortfall stays a receivable, no retry"
    if outcome is Outcome.NEEDS_PERSON:
        st.flag_attention(kstore, "charge", str(attempt), f"SUSPENDED at Airwallex ({rid})")
        st.put_recovery_attempt(kstore, st.RecoveryAttempt(attempt, amount, rid, "SUSPENDED"))
        return False, (
            "recovery charge SUSPENDED by Airwallex: operator action required; "
            f"{amount} stays in flight on request_id {rid}"
        )
    if person:
        st.flag_attention(kstore, "charge", str(attempt), person)
        return False, (
            f"recovery {outcome.value}; {amount} stays in flight on request_id "
            f"{rid}; needs a person: {person}. Nothing will be re-sent"
        )
    return False, (
        f"recovery {outcome.value}; {amount} stays in flight on request_id {rid}. "
        "reconcile_seller_money reads it back; recover_shortfall re-emits the identical "
        "request_id only if Airwallex has no record of it"
    )


def make_recover(policy: ReservePolicy) -> KitWorkflowSpec:
    def policy_fn(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        if (bad := account_or_block(ctx)) is not None:
            return bad
        kstore, seller = ctx.store, ctx.tenant.tenant_id
        try:
            st.book_id(kstore)  # rule M2
            currency = st.profile(kstore).currency
            ledger = st.ledger_state(kstore, seller, currency).ledger
            attempts = st.recovery_attempts(kstore)
            pending = [a for a in attempts if a.outcome in st.PENDING_RECOVERY]
            if pending:
                a = pending[0]
                sent = st.first_sent_at(kstore, "charge", str(a.attempt))
                if sent is not None and not within_dedup_window(sent, ctx.now):
                    return blocked(
                        f"needs a person: recovery attempt {a.attempt} ({a.amount}) has been "
                        "in flight past the request_id deduplication window; nothing will be "
                        "sent. Use propose_resolve_in_flight_money with Airwallex's evidence"
                    )
                return PolicyDecision(
                    RiskTier.AUTO,
                    (
                        f"read back recovery attempt {a.attempt} ({a.amount}); re-send the "
                        "identical charge (same request_id) only if Airwallex has no record of it",
                    ),
                    {"mode": "resend", "attempt": a.attempt, "charge": a.amount},
                )
            if ledger.receivable.is_zero():
                return blocked("the seller owes nothing")
            hold = wt.gate(kstore, ctx.tenant, ctx.read_platform, "charge", ctx.now)
            if hold:  # never start a new attempt while a ruled-not_landed charge is in doubt
                return blocked(f"hold: {hold}")
            watched = wt.open_watches(kstore)
            body = ctx.read(seller_balance_request(ctx.tenant))
            available = available_balance(body, currency)
        except (AwxError, ValueError, TypeError) as exc:
            return blocked(f"cannot assess recovery: {exc}")
        # The live balance is NOT in computed: computed is in the proposal hash, so any
        # wallet movement would make the approval stale. The build re-reads it and skips
        # the charge if it fell short.
        computed: dict[str, Any] = {
            "mode": "recover",
            "receivable": ledger.receivable,
            "attempt": len(attempts) + 1,
            "ledger_version": ledger.version,
            "charge": None,
        }
        if available < ledger.receivable:
            reason = (
                "seller wallet is short of the receivable: no charge attempted; the shortfall "
                f"of {ledger.receivable} stays a receivable"
            )
            return PolicyDecision(RiskTier.AUTO, (reason,), computed)
        computed["charge"] = ledger.receivable
        tier = RiskTier.DUAL if needs_dual(policy, ledger.receivable) else RiskTier.SINGLE
        reasons = [f"recovery charge of {ledger.receivable} from the seller wallet"]
        if watched:
            reasons.append(
                f"watching {len(watched)} not_landed decision(s): " + wt.describe(watched)
            )
        return PolicyDecision(tier, tuple(reasons), computed)

    def build(ctx: StepContext) -> tuple:
        computed = ctx.decision.computed
        if computed["charge"] is None:
            return ()
        kstore = st.kit_store(ctx.policy)
        tenant = ctx.policy.tenant
        attempt, amount = computed["attempt"], computed["charge"]
        # The provider sees the identical body and request_id; the op carries THIS run's
        # proposal hash, which the engine binds every operation to (rules 4 and 9).
        original = ctx.intent_hash
        op = build_recovery_op(
            attempt, amount, tenant, book_id=st.book_id(kstore), intent_hash=original
        )
        recorded = [a for a in st.recovery_attempts(kstore) if a.attempt == attempt]
        if computed["mode"] == "resend" or recorded:
            if not recorded or recorded[0].outcome not in st.PENDING_RECOVERY:
                return ()
            sent = st.first_sent_at(kstore, "charge", str(attempt))
            if sent is not None and not within_dedup_window(sent, ctx.policy.now):
                raise StateConflict("past the deduplication window; a person must decide")
            # Reconcile first (M1). ReconcileUndetermined propagates: fail closed, send nothing.
            if find_by_request_id(op, ctx.policy.read_platform) is not None:
                return ()  # it landed; the postcondition reads and records it
            st.mark_sent(
                kstore,
                "charge",
                str(attempt),
                op.request_id,
                ctx.policy.now,
                op.intent_hash,
                ctx.proposal_id,
            )
            return (op,)  # the identical Operation: same request_id, same body
        state = st.ledger_state(kstore, tenant.tenant_id, amount.currency)
        ledger = state.ledger
        if ledger.version != computed["ledger_version"] or ledger.receivable != amount:
            raise StateConflict("receivable changed since approval; propose again")
        fresh = available_balance(ctx.policy.read(seller_balance_request(tenant)), amount.currency)
        if fresh < amount:  # the approval-time read was stale
            st.claim_recovery(
                kstore,
                state,
                st.RecoveryAttempt(
                    attempt, amount, op.request_id, "SKIPPED_STALE_BALANCE", ctx.proposal_id
                ),
            )
            return ()
        # The claim is written in the ledger item, conditional on the version read above: a
        # settlement that netted this receivable in the meantime makes this raise, and nothing
        # is sent. From here on, settlements exclude it from netting.
        st.claim_recovery(
            kstore,
            state,
            st.RecoveryAttempt(attempt, amount, op.request_id, "PENDING", ctx.proposal_id),
        )
        st.mark_sent(
            kstore,
            "charge",
            str(attempt),
            op.request_id,
            ctx.policy.now,
            op.intent_hash,
            ctx.proposal_id,
        )
        return (op,)

    def postcondition(ctx: StepContext) -> PostconditionResult:
        computed = ctx.decision.computed
        if computed["charge"] is None:
            return PostconditionResult(
                True, "no charge attempted; the shortfall stays a receivable"
            )
        kstore = st.kit_store(ctx.policy)
        tenant = ctx.policy.tenant
        attempt, amount = computed["attempt"], computed["charge"]
        recorded = [a for a in st.recovery_attempts(kstore) if a.attempt == attempt]
        if not recorded:
            return PostconditionResult(False, "no charge was attempted (the build did not run)")
        rec = recorded[0]
        if rec.outcome == "SKIPPED_STALE_BALANCE":
            return PostconditionResult(
                True, "wallet fell short before execution: no charge; the shortfall stays"
            )
        if rec.outcome not in st.PENDING_RECOVERY:
            return PostconditionResult(rec.outcome == "SETTLED", f"recovery {rec.outcome}")
        op = build_recovery_op(
            attempt, amount, tenant, book_id=st.book_id(kstore), intent_hash=ctx.intent_hash
        )
        results = ctx.prior.get("recover", ())
        outcome, _ = resolve(op, results[0] if results else None, ctx.policy.read_platform)
        if computed["mode"] == "resend":
            outcome = resend_outcome(outcome)
        person = None
        if outcome is Outcome.UNKNOWN:
            person = person_reason(op, results[0] if results else None, ctx.policy.read_platform)
        ok, detail = apply_charge_outcome(kstore, tenant, attempt, amount, outcome, person=person)
        fresh = st.ledger_state(kstore, tenant.tenant_id, amount.currency)
        if (problem := identity_problem(fresh, None)) is not None:
            return PostconditionResult(False, problem)
        return PostconditionResult(ok, detail)

    return KitWorkflowSpec(
        approver=ApproverScope.OWN_TENANT,  # the seller's own approver
        kit=KIT,
        action="recover_shortfall",
        validate=validate_recover,
        policy=policy_fn,
        steps=(StepSpec("recover", build, CHARGE_FINALITY, find_by_request_id),),
        approval_ttl=APPROVAL_TTL,
        read_allowlist=TENANT_READS,
        platform_read_allowlist=PLATFORM_READS,
        postcondition=postcondition,
    )
