"""Kit 7 workflow specs: weekly collection, advance disbursement and portfolio reconciliation.

State lives in the kit's platform-wide KitStore (ctx.platform_store) as one versioned
LoanBook (book.py), shared by every k7 tenant; loan_for_tenant keeps each borrower to its own
loan. Policy only loads it; builds and postconditions write it with optimistic concurrency,
idempotently.

Ledger postings happen only in postconditions, and only from a terminal provider read: a
create response saying NEW never moves the subledger. An in-flight charge or tranche is
recorded in the book before it is sent; until it is terminal and posted, the loan's next
proposal re-sends the same operation (same request_id and body), so an unknown outcome is
resolved by Airwallex's duplicate check and the reconcile hook, never by a second movement.

Origins: weekly collections are a scheduled run (Origin.SCHEDULE) and keep the
policy tier, AUTO. A model-started collection gets the default floor, SINGLE. Disbursements
are SINGLE or DUAL from policy whatever the origin. reconcile_portfolio moves no money and sets
agent_min_tier=AUTO (see README.md, "Approval rules").
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from awx_common import ApprovalBindingError, Money, OperationStatus, RiskTier
from awx_common.errors import NotFound, TenantScopeViolation, ValidationFailed
from awx_common.workflow import (
    ApproverScope,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    StepContext,
    StepSpec,
)

from .book import (
    DEDUP_MARGIN,
    LoanBook,
    PendingCollection,
    PendingDisbursement,
    PortfolioNotOpen,
    WeekRecord,
    check_loan_id,
    load_book,
    update_book,
    week_window,
)
from .policy import TrancheOutcome, size_disbursement
from .provider import (
    BALANCES_CURRENT,
    BALANCES_HISTORY,
    CA_TRANSFERS,
    CHARGE_FINALITY,
    CHARGES,
    KIT,
    TRANSFER_FINALITY,
    available_balance,
    charge_operation,
    collection_request_id,
    disbursement_request_id,
    find_by_request_id,
    platform_reader,
    read_charge,
    read_transfer,
    reconcile_charge,
    reconcile_transfer,
    report_operation,
    to_money,
    transfer_operation,
    weekly_revenue,
)
from .reconcile import reconcile, reconcile_ledger
from .repayment import plan_collection


def _exact(path: str) -> str:
    return f"^{path}$"


def _by_id(path: str) -> str:
    return f"^{path}/[A-Za-z0-9_-]+$"


# Borrower-scoped GETs (x-on-behalf-of = the borrower): wallet and revenue only.
BORROWER_READS: tuple[str, ...] = (_exact(BALANCES_CURRENT), _exact(BALANCES_HISTORY))
# Headerless GETs on the platform account: its wallet, and our own movements by id or by
# request_id (finality and reconcile reads of body-bound movements, rule 5).
PLATFORM_READS: tuple[str, ...] = (
    _exact(BALANCES_CURRENT),
    _exact(CHARGES),
    _by_id(CHARGES),
    _exact(CA_TRANSFERS),
    _by_id(CA_TRANSFERS),
)


def _only(raw: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(raw) - allowed
    if extra:
        raise ValidationFailed(f"unexpected fields {sorted(extra)}")


def validate_collect(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    _only(raw, {"loan_id", "week"})
    week = raw.get("week")
    if not isinstance(week, str):
        raise ValidationFailed("week is required")
    week_window(week)
    return {"loan_id": check_loan_id(raw.get("loan_id")), "week": week}


def validate_disburse(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    _only(raw, {"loan_id"})
    return {"loan_id": check_loan_id(raw.get("loan_id"))}


def validate_reconcile(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    _only(raw, set())
    return {}


def _blocked(*reasons: str, computed: Mapping[str, Any] | None = None) -> PolicyDecision:
    return PolicyDecision(RiskTier.BLOCKED, tuple(reasons), dict(computed or {}))


@dataclass(frozen=True)
class Observed:
    """What Airwallex says about a step's movement.

    state: NONE (nothing was sent), FUNDS (refused for insufficient funds, nothing created),
    REFUSED (any other refusal, nothing created), UNKNOWN (not found, or not determinable),
    or the provider status of the object (NEW, PENDING, SETTLED, SUSPENDED, FAILED).
    """

    state: str
    provider_id: str | None = None
    amount: Money | None = None
    detail: str | None = None


def _observe(
    ctx: StepContext,
    step: str,
    kind: str,
    request_id: str,
    currency: str,
    *,
    amount: Money | None = None,
    account: str | None = None,
) -> Observed:
    results = ctx.prior.get(step, ())
    read = platform_reader(ctx.policy)
    if not results:
        return Observed("NONE")
    result = results[-1]
    if result.status is OperationStatus.OUTCOME_UNKNOWN and result.error_code in PERSON_CODES:
        # Codes a resend cannot change, including the shape where another object holds the
        # request_id (which may be another payment). Never adopt it: in
        # flight until a person decides.
        return Observed("UNKNOWN", detail=f"{result.error_code}: a person must decide")
    if result.status is OperationStatus.FAILED and result.error_type in RETRYABLE_ERRORS:
        # A retryable FAILED (429, 5xx before acceptance) proves nothing about whether an
        # earlier send of the same request_id landed: in flight, never a refusal (as kits 1
        # and 5 do).
        return Observed("UNKNOWN", detail=f"retryable {result.error_code or result.error_type}")
    if result.status is OperationStatus.FAILED and result.error_type != "DuplicateRequest":
        if result.error_type == "InsufficientFunds":
            return Observed("FUNDS", detail=result.error_code)
        return Observed("REFUSED", detail=result.error_code or result.error_type)
    if result.status is OperationStatus.SUCCEEDED and result.provider_id:
        reader = read_charge if kind == "charge" else read_transfer
        try:
            body = read(reader(result.provider_id))
        except NotFound:
            # A NotFound on a by-id read after a send means unknown, never absent.
            return Observed("UNKNOWN", result.provider_id, detail="not visible by id yet")
        status = str(body.get("status"))
        amount = to_money(body.get("amount"), currency) if status == "SETTLED" else None
        return Observed(status, result.provider_id, amount)
    # OUTCOME_UNKNOWN or a duplicate: find our object by request_id (FAKE-FIDELITY.md rows
    # 51-52).
    try:
        found = find_by_request_id(read, kind, request_id, currency)
    except Exception as exc:
        return Observed("UNKNOWN", detail=str(exc))
    if found is None:
        return Observed("UNKNOWN", detail="no object with this request_id yet")
    if _mismatch(found, amount, account):
        return Observed(
            "UNKNOWN",
            detail=f"{found.provider_id} under {request_id} does not match this movement; "
            "a person must look",
        )
    return Observed(found.status, found.provider_id, found.amount)


def _mismatch(found: Any, amount: Money | None, account: str | None) -> bool:
    """Another payment holds this request_id: never adopt it."""
    return (amount is not None and found.amount != amount) or (
        account is not None and found.counterparty != account
    )


RETRYABLE_ERRORS = frozenset({"RateLimited", "TransientServerError", "OutcomeUnknown"})
# OUTCOME_UNKNOWN codes rule 9 stops on at once: a resend cannot change them.
PERSON_CODES = frozenset(
    {
        "request_id_reused",
        "request_id_bound_to_another_body",
        "multiple_objects",
        "first_attempt_unknown",
        "past_dedupe_margin",
    }
)


def _confirm_refusal(
    ctx: StepContext,
    seen: Observed,
    kind: str,
    request_id: str,
    currency: str,
    *,
    amount: Money | None = None,
    account: str | None = None,
) -> Observed:
    """A refusal at create is definitive only once the request_id's read-back is empty too.

    If the read-back finds an object the refusal was not what it seemed, so its status
    decides; if the read-back itself is ambiguous, the money stays in flight (rule M1).
    """
    if seen.state not in ("FUNDS", "REFUSED"):
        return seen
    try:
        found = find_by_request_id(platform_reader(ctx.policy), kind, request_id, currency)
    except Exception as exc:
        return Observed("UNKNOWN", detail=f"refused, but read-back is ambiguous: {exc}")
    if found is None:
        return seen
    if _mismatch(found, amount, account):
        # Another payment holds this request_id: never adopt it.
        return Observed(
            "UNKNOWN",
            detail=f"{found.provider_id} under {request_id} does not "
            "match this movement; a person must look",
        )
    return Observed(found.status, found.provider_id, found.amount)


def _definitive(seen: Observed, resend: bool, owns_hold: bool = True) -> Observed:
    """Rule M1 for re-sends. A refusal at create, even with an empty read-back, is definitive
    only for a first send. When this run re-emitted a movement whose first send ended
    unknown, the refusal says nothing about the original, which may still land and be
    invisible for a while; so it stays in flight on the original request_id. Only a read of
    the original's own failure (FAILED) moves it."""
    if (resend or not owns_hold) and seen.state in ("FUNDS", "REFUSED"):
        return Observed("UNKNOWN", detail=f"re-send refused ({seen.detail}); original in flight")
    return seen


def _owns(hold: Any, proposal_id: str) -> bool:
    """Only the run that placed a hold made its first send (M4). A refusal seen by any other
    run is a refusal of a re-send, which says nothing about the original."""
    return hold is None or hold.owner == proposal_id


def _past_margin(placed_at: Any, now: Any) -> bool:
    return now - placed_at >= DEDUP_MARGIN


DISAGREE = "the tenants table and the seeded account disagree"
log = logging.getLogger(__name__)


def _tenants_row_disagrees(book: LoanBook, loan_id: str, ctx: PolicyContext) -> str | None:
    """Money goes where the tenants row points, so a money policy BLOCKs
    when the run tenant is this loan's borrower but its connected account is not the one the
    loan was seeded and checked with. Ids only; no account id reaches the borrower."""
    terms = book.loan(loan_id)
    if book.tenant_row_disagrees(ctx.tenant):
        # Both ids go to the operator log only; the tenant sees fixed text.
        log.warning(
            "k7 tenants row disagrees: tenant=%s loan=%s row_account=%s seeded_account=%s",
            ctx.tenant.tenant_id,
            loan_id,
            ctx.tenant.connected_account_id,
            terms.borrower_account,
        )
        return DISAGREE
    return None


def _load(ctx: PolicyContext) -> LoanBook | None:
    try:
        return load_book(ctx.platform_store)[0]
    except PortfolioNotOpen:
        return None


def build_specs() -> dict[str, KitWorkflowSpec]:
    # Weekly collection

    def collect_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        book = _load(ctx)
        if book is None:
            return _blocked("no loan portfolio is open")
        disagree = _tenants_row_disagrees(book, intent["loan_id"], ctx)
        if disagree:
            return _blocked(disagree)
        terms = book.loan_for_tenant(intent["loan_id"], ctx.tenant)
        lid, week = terms.loan_id, intent["week"]
        expected = book.next_collectible_week(lid)
        if expected is None:
            return _blocked("the loan has no settled tranche yet; nothing to collect")
        if week != expected:
            return _blocked(f"the next week to collect for {lid} is {expected}, not {week}")
        _, end = week_window(week)
        if end > ctx.now:
            return _blocked(f"week {week} has not ended")
        pending = book.pending_collection(lid)
        if pending is not None and _past_margin(pending.placed_at, ctx.now):
            return _blocked(
                f"the {week} charge on {pending.request_id} is unresolved after "
                f"{DEDUP_MARGIN.days} days; a person must resolve it, nothing is sent"
            )
        if pending is not None:
            return PolicyDecision(
                RiskTier.AUTO,
                (f"re-send the in-flight {week} charge of {pending.amount} unchanged",),
                {
                    "due": pending.due,
                    "collect": pending.amount,
                    "shortfall": pending.due - pending.amount,
                    "request_id": pending.request_id,
                    "attempt": book.attempt("collect", lid, week),
                    "resend": True,
                },
            )
        outstanding = book.ledger.outstanding(lid)
        if outstanding.is_zero():
            return _blocked("loan is fully repaid")
        start, end = week_window(week)
        own = frozenset(book.issued_request_ids()) | frozenset(
            e.provider_id for e in book.ledger.entries if e.provider_id
        )
        revenue = weekly_revenue(
            ctx, terms.currency, start, end, own, book.tranche_credits(lid, start, end)
        )
        attempt = book.attempt("collect", lid, week)
        available = available_balance(ctx.read, terms.currency)
        plan = plan_collection(revenue, outstanding, available, terms.repayment)
        reasons = [
            f"revenue-share collection for {week}: {terms.repayment.share_rate} of {revenue}, "
            f"floor {terms.repayment.weekly_floor}, cap {terms.repayment.weekly_cap}, "
            f"outstanding {outstanding}, wallet {available}"
        ]
        if plan.shortfall.amount > 0:
            reasons.append(f"wallet short by {plan.shortfall}; the shortfall becomes arrears")
        return PolicyDecision(
            RiskTier.AUTO,
            tuple(reasons),
            computed={
                "revenue": plan.revenue,
                "share": plan.share,
                "due": plan.due,
                "collect": plan.collect,
                "shortfall": plan.shortfall,
                "request_id": collection_request_id(book.book_id, lid, week, attempt),
                "attempt": attempt,
                "resend": False,
            },
        )

    def collect_build(ctx: StepContext) -> tuple:
        lid, week = ctx.intent["loan_id"], ctx.intent["week"]
        computed = ctx.decision.computed
        amount: Money = computed["collect"]
        if amount.is_zero():
            return ()  # empty wallet: the week closes as arrears, nothing is sent

        def reserve(book: LoanBook) -> str:
            book.loan_for_tenant(lid, ctx.policy.tenant)
            book.reserve_collection(
                PendingCollection(
                    lid,
                    week,
                    computed["due"],
                    amount,
                    computed["request_id"],
                    ctx.proposal_id,
                    ctx.policy.now,
                ),
                resend=bool(computed["resend"]),
            )
            return book.book_id

        book_id = update_book(ctx.policy.platform_store, reserve)
        op = charge_operation(
            book_id=book_id,
            loan_id=lid,
            week=week,
            attempt=computed["attempt"],
            amount=amount,
            intent_hash=ctx.intent_hash,
            tenant=ctx.policy.tenant,
        )
        if op.request_id != computed["request_id"]:
            raise ApprovalBindingError("charge request_id differs from the approved one")
        return (op,)

    def collect_post(ctx: StepContext) -> PostconditionResult:
        lid, week = ctx.intent["loan_id"], ctx.intent["week"]
        computed = ctx.decision.computed
        rid = computed["request_id"]
        due, amount = computed["due"], computed["collect"]
        book, _ = load_book(ctx.policy.platform_store)
        seen = _observe(
            ctx,
            "charge",
            "charge",
            rid,
            book.config.currency,
            amount=amount,
            account=book.loan(lid).borrower_account,
        )
        seen = _confirm_refusal(
            ctx,
            seen,
            "charge",
            rid,
            book.config.currency,
            amount=amount,
            account=book.loan(lid).borrower_account,
        )
        seen = _definitive(
            seen,
            bool(computed["resend"]),
            _owns(book.pending_collection(lid), ctx.proposal_id),
        )
        zero = Money.zero(book.config.currency)

        def close(collected: Money, charge_id: str | None, status: str | None, sent: bool):
            record = WeekRecord(lid, week, due, collected, rid if sent else None, charge_id, status)

            def apply(b: LoanBook) -> None:
                b.close_week(record)
                breaks = reconcile_ledger(b)
                if breaks:
                    raise AssertionError("; ".join(breaks))

            update_book(ctx.policy.platform_store, apply)
            return PostconditionResult(
                True, f"{week}: due {due}, collected {collected}, arrears +{due - collected}"
            )

        if seen.state == "NONE":
            if amount.is_zero():
                return close(zero, None, None, sent=False)
            # This run built the charge (so it may have been sent) but holds no result for
            # it, for example after a finality read error: the hold stays (rule M1).
            return PostconditionResult(
                False, "no result for the charge; it stays in flight and the week stays open"
            )
        if seen.state == "FUNDS":
            # Refused synchronously, nothing created (row 109): the due becomes arrears.
            return close(zero, None, seen.detail, sent=True)
        if seen.state == "REFUSED":

            def release(b: LoanBook) -> None:
                b.release_collection(lid, rid)

            update_book(ctx.policy.platform_store, release)
            return PostconditionResult(
                False,
                f"charge refused ({seen.detail}), nothing created; not arrears. The week stays "
                "open on its next attempt, so a fresh approved proposal sends a new request_id",
            )
        if seen.state == "SETTLED":
            assert seen.amount is not None
            if seen.amount != amount:
                return PostconditionResult(
                    False, f"settled {seen.amount} differs from the charge sent ({amount})"
                )
            return close(seen.amount, seen.provider_id, seen.state, sent=True)
        if seen.state == "FAILED":
            return close(zero, seen.provider_id, seen.state, sent=True)
        if seen.state == "SUSPENDED":
            return PostconditionResult(
                False,
                f"charge {seen.provider_id} SUSPENDED: needs an operator; the week stays open",
            )
        return PostconditionResult(
            False, f"charge not final ({seen.state} {seen.detail or ''}); week stays open"
        )

    collect = KitWorkflowSpec(
        approver=ApproverScope.OWN_TENANT,  # the requester's own approver
        kit=KIT,
        action="collect_weekly_repayment",
        validate=validate_collect,
        policy=collect_policy,
        steps=(StepSpec("charge", collect_build, CHARGE_FINALITY, reconcile=reconcile_charge),),
        approval_ttl=timedelta(hours=24),
        read_allowlist=BORROWER_READS,
        platform_read_allowlist=PLATFORM_READS,
        postcondition=collect_post,
    )

    # Disbursement

    def _plan(book: LoanBook, loan_id: str, ctx: PolicyContext):
        provider = available_balance(platform_reader(ctx), book.config.currency)
        usable = book.usable_cash(provider)
        return size_disbursement(book.commitment_remaining(loan_id), usable, book.config), provider

    def disburse_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        book = _load(ctx)
        if book is None:
            return _blocked("no loan portfolio is open")
        cfg = book.config
        disagree = _tenants_row_disagrees(book, intent["loan_id"], ctx)
        if disagree:
            return _blocked(disagree)
        terms = book.loan_for_tenant(intent["loan_id"], ctx.tenant)
        lid = terms.loan_id
        pending = book.pending_for(lid)
        if pending is not None and _past_margin(pending.placed_at, ctx.now):
            return _blocked(
                f"tranche {pending.tranche} on {pending.request_id} is unresolved after "
                f"{DEDUP_MARGIN.days} days; a person must resolve it, nothing is sent"
            )
        if pending is not None:
            tier = RiskTier.DUAL if pending.amount > cfg.dual_approval_above else RiskTier.SINGLE
            return PolicyDecision(
                tier,
                (f"re-send the in-flight tranche {pending.tranche} of {pending.amount} unchanged",),
                {
                    "amount": pending.amount,
                    "fee": terms.fee_for(pending.amount),
                    "tranche": pending.tranche,
                    "request_id": pending.request_id,
                    "attempt": book.attempt("disburse", lid, str(pending.tranche)),
                    "resend": True,
                },
            )
        if book.commitment_remaining(lid).is_zero():
            return _blocked("commitment fully disbursed")
        plan, _provider = _plan(book, lid, ctx)
        tranche = book.next_tranche(lid)
        attempt = book.attempt("disburse", lid, str(tranche))
        computed = {
            "amount": plan.amount,
            "fee": terms.fee_for(plan.amount),
            "tranche": tranche,
            "outcome": plan.outcome,
            "commitment_remaining": plan.commitment_remaining,
            "request_id": disbursement_request_id(book.book_id, lid, tranche, attempt),
            "attempt": attempt,
            "resend": False,
        }
        # Borrower-facing text describes only this borrower's own terms: the platform's cash,
        # wallet, in-flight and usable totals and the portfolio reserve floor never reach a
        # borrower. Operators see the sizing in the book (recorded at build) and in
        # read_portfolio_position.
        if plan.outcome is TrancheOutcome.DELAY:
            return _blocked(
                "delayed: not enough headroom above the reserve floor for the minimum tranche",
                computed=computed,
            )
        why = {
            TrancheOutcome.FULL: "the full remaining commitment fits",
            TrancheOutcome.PARTIAL: "the reserve floor binds",
        }[plan.outcome]
        reasons = [
            f"{plan.outcome.value.lower()} tranche {plan.amount} of the remaining commitment "
            f"{plan.commitment_remaining}: {why}",
            "every disbursement needs a human approver",
        ]
        tier = RiskTier.SINGLE
        if plan.amount > cfg.dual_approval_above:
            tier = RiskTier.DUAL
            reasons.append("a tranche of this size needs two approvers")
        return PolicyDecision(tier, tuple(reasons), computed)

    def disburse_build(ctx: StepContext) -> tuple:
        lid = ctx.intent["loan_id"]
        computed = ctx.decision.computed

        def reserve(book: LoanBook) -> str:
            book.loan_for_tenant(lid, ctx.policy.tenant)
            own = book._pending.get(computed["request_id"])
            retried = own is not None and own.owner == ctx.proposal_id
            if not computed["resend"] and not retried:
                # A retried build of this proposal skips the re-plan: its own hold is already
                # netted out of usable cash, so the plan would mismatch (rule 0).
                plan, provider = _plan(book, lid, ctx.policy)
                # Operator-only record of what sized this tranche (never shown to borrowers).
                book.record_sizing(
                    computed["request_id"],
                    {
                        "at": ctx.policy.now,
                        "loan_id": lid,
                        "amount": plan.amount,
                        "outcome": plan.outcome.value,
                        "ledger_cash": book.ledger.platform_cash(),
                        "provider_available": provider,
                        "in_flight": book.pending_total(),
                        "usable": plan.usable_cash,
                        "reserve_floor": plan.reserve_floor,
                    },
                )
                # Fail closed if anything the approver saw has moved (rule 1 re-runs policy
                # too; this guards the window between that re-run and this write).
                if (
                    plan.amount != computed["amount"]
                    or book.next_tranche(lid) != computed["tranche"]
                ):
                    raise ApprovalBindingError("portfolio cash or loan state changed")
            book.reserve_disbursement(
                PendingDisbursement(
                    lid,
                    computed["tranche"],
                    computed["amount"],
                    computed["request_id"],
                    ctx.proposal_id,
                    ctx.policy.now,
                ),
                resend=bool(computed["resend"]),
            )
            return book.book_id

        book_id = update_book(ctx.policy.platform_store, reserve)
        op = transfer_operation(
            book_id=book_id,
            loan_id=lid,
            tranche=computed["tranche"],
            attempt=computed["attempt"],
            amount=computed["amount"],
            intent_hash=ctx.intent_hash,
            tenant=ctx.policy.tenant,
        )
        if op.request_id != computed["request_id"]:
            raise ApprovalBindingError("transfer request_id differs from the approved one")
        return (op,)

    def disburse_post(ctx: StepContext) -> PostconditionResult:
        computed = ctx.decision.computed
        rid = computed["request_id"]
        book, _ = load_book(ctx.policy.platform_store)
        seen = _observe(
            ctx,
            "transfer",
            "transfer",
            rid,
            book.config.currency,
            amount=computed["amount"],
            account=book.loan(ctx.intent["loan_id"]).borrower_account,
        )
        seen = _confirm_refusal(
            ctx,
            seen,
            "transfer",
            rid,
            book.config.currency,
            amount=computed["amount"],
            account=book.loan(ctx.intent["loan_id"]).borrower_account,
        )
        seen = _definitive(
            seen,
            bool(computed["resend"]),
            _owns(book.pending_for(ctx.intent["loan_id"]), ctx.proposal_id),
        )
        if seen.state in ("FUNDS", "REFUSED", "FAILED"):
            # Definitive (rule M1): released, and the tranche moves to its next attempt.
            update_book(ctx.policy.platform_store, lambda b: b.release_disbursement(rid))
            return PostconditionResult(
                False,
                f"transfer failed ({seen.state} {seen.detail or ''}); headroom released, the "
                "next approved proposal sends the tranche on a new request_id",
            )
        if seen.state != "SETTLED":
            return PostconditionResult(
                False, f"transfer not final ({seen.state} {seen.detail or ''}); held"
            )
        assert seen.provider_id is not None
        assert seen.amount is not None
        now = ctx.policy.now

        def settle(b: LoanBook) -> list[str]:
            b.settle_disbursement(rid, seen.provider_id, seen.amount, settled_at=now)
            breaks = reconcile_ledger(b)
            if b.ledger.platform_cash() < b.config.reserve_floor:
                breaks.append("ledger platform cash fell below the reserve floor")
            return breaks

        breaks = update_book(ctx.policy.platform_store, settle)
        if breaks:
            return PostconditionResult(False, "; ".join(breaks))
        return PostconditionResult(True, f"tranche {computed['tranche']} {seen.amount} settled")

    def disburse_abandon(ctx: StepContext, reason: str) -> None:
        # Rule M4: an abandoned run never built, so it took no hold. A re-send proposal names
        # a tranche held by an earlier run, whose outcome may still be unknown: it stays held.
        return None

    disburse = KitWorkflowSpec(
        approver=ApproverScope.KIT_OPERATOR,  # the kit's operator approves every advance
        kit=KIT,
        action="disburse_advance",
        validate=validate_disburse,
        policy=disburse_policy,
        steps=(
            StepSpec("transfer", disburse_build, TRANSFER_FINALITY, reconcile=reconcile_transfer),
        ),
        approval_ttl=timedelta(hours=4),
        read_allowlist=BORROWER_READS,
        platform_read_allowlist=PLATFORM_READS,
        postcondition=disburse_post,
        on_abandon=disburse_abandon,
    )

    # Reconciliation

    def reconcile_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        if ctx.tenant.connected_account_id is not None:
            raise TenantScopeViolation("portfolio reconciliation runs as the platform only")
        try:
            _, version = load_book(ctx.platform_store)
        except PortfolioNotOpen:
            return _blocked("no loan portfolio is open")
        # Only the ledger version is hashed: the live reads happen in the postcondition, so a
        # re-run moves the hash only if the book itself moved (the proposal hash covers computed).
        return PolicyDecision(
            RiskTier.AUTO,
            ("read-only reconciliation of the loan ledger plus a balance report request",),
            computed={"ledger_version": version},
        )

    def reconcile_build(ctx: StepContext) -> tuple:
        book, _ = load_book(ctx.policy.platform_store)
        return (
            report_operation(
                book_id=book.book_id,
                ledger_version=ctx.decision.computed["ledger_version"],
                intent_hash=ctx.intent_hash,
                tenant=ctx.policy.tenant,
            ),
        )

    def reconcile_post(ctx: StepContext) -> PostconditionResult:
        from .book import ReconcileRecord

        book, _ = load_book(ctx.policy.platform_store)
        available = available_balance(ctx.policy.read, book.config.currency)
        result = reconcile(book, ctx.policy.read, available)
        now = ctx.policy.now

        def record(b: LoanBook) -> None:
            b.last_reconcile = ReconcileRecord(now, result.ok, result.breaks)

        update_book(ctx.policy.platform_store, record)
        if not result.ok:
            return PostconditionResult(False, "; ".join(result.breaks))
        return PostconditionResult(True, "portfolio reconciles to settled Airwallex movements")

    recon = KitWorkflowSpec(
        approver=ApproverScope.OWN_TENANT,  # the requester's own approver
        kit=KIT,
        action="reconcile_portfolio",
        validate=validate_reconcile,
        policy=reconcile_policy,
        steps=(StepSpec("report", reconcile_build),),
        approval_ttl=timedelta(hours=1),
        read_allowlist=PLATFORM_READS,
        postcondition=reconcile_post,
        agent_min_tier=RiskTier.AUTO,
    )

    return {s.action: s for s in (collect, disburse, recon)}


SPECS: dict[str, KitWorkflowSpec] = build_specs()
