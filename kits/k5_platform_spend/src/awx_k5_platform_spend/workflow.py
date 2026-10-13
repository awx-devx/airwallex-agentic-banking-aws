"""Kit 5 workflow specs. The spine runs them: validate -> policy -> approval -> steps ->
postcondition, with on_abandon when no step ran (runner rules 0 to 11, kit rules M1 to M4 in
awx_common.workflow).

Actions: register_shortfall, bridge_shortfall, reconcile_bridge, collect_fee,
collect_repayment, issue_card, simulate_card_spend. The model supplies references (which
obligation, card, scenario, program) and words. Code computes every amount, picks the connected
account and derives every request_id.

Purity (rule 0): policies only read. The bridge queue is written by register_shortfall's step
and dropped by the bridge postcondition, reconcile_bridge and on_abandon. A hold is placed by
the bridge build and owned by that run's proposal_id (M4). It is released only by its owner's
postcondition when nothing was sent, or after a definitive outcome (M1): a non-retryable
rejection, a provider FAILED, or settlement. on_abandon never touches the ledger.

State: the platform book (capital ledger with the platform book id, bridge queue) is built
per call over `ctx.platform_store`; each tenant's registries and its tenant book id live in
`ctx.store`. Static platform configuration is the setup document an operator seeds into the
platform store (`seed_platform_setup`), or `configure_platform` in-process; tests call
`build_specs(setup_provider)`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from awx_common import Money, Operation, OperationResult, OperationStatus, ReadRequest, RiskTier
from awx_common.errors import AwxError, NotFound, StateConflict
from awx_common.ops import DEDUP_RESEND_MARGIN
from awx_common.store import KitStore, VersionConflict
from awx_common.workflow import (
    ApproverScope,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    ReconcileUndetermined,
    StepContext,
    StepSpec,
)

from .allocator import BridgeCandidate, allocate, eligibility
from .amounts import available_balance, provider_money
from .book import PlatformBook, PlatformSetup, load_platform_setup
from .config import KIT
from .finality import (
    BRIDGE_FINALITY,
    CARD_FINALITY,
    CARDHOLDER_FINALITY,
    CHARGE_FINALITY,
    CHARGES_PATH,
    MOVEMENT_FAILED,
    SETTLED,
    SUSPENDED,
    TRANSFERS_PATH,
    CardTxnOutcome,
    CreateOutcome,
    bridge_reconcile,
    card_reconcile,
    card_transaction_outcome,
    cardholder_reconcile,
    charge_reconcile,
    find_movement,
    interpret_create,
    simulation_reconcile,
)
from .ledger import EntryKind, Hold, LedgerError, StaleObservation, WalletObservation
from .operations import (
    attempt_step,
    bridge_operation,
    business_key,
    card_operation,
    card_simulation_operation,
    cardholder_operation,
    charge_operation,
    request_id_for,
)
from .tenants import customer_read
from .validation import (
    AMOUNT,
    CARD_ID,
    CURRENCY,
    MCC,
    PERIOD,
    RATIONALE_MAX,
    REF,
    InvalidIntent,
    amount_of,
    strict,
)

log = logging.getLogger(__name__)

SetupProvider = Callable[[PolicyContext], PlatformSetup]

_ID = r"[A-Za-z0-9_-]+"
BALANCES = "/api/v1/balances/current"
ALLOW_BALANCES = r"^/api/v1/balances/current$"
# Platform-scope reads (no x-on-behalf-of): platform objects and the platform wallet (rule 5).
PLATFORM_TRANSFERS = (
    r"^/api/v1/connected_account_transfers$",
    rf"^/api/v1/connected_account_transfers/{_ID}$",
)
PLATFORM_CHARGES = (r"^/api/v1/charges$", rf"^/api/v1/charges/{_ID}$")
ALLOW_CARDS = (
    r"^/api/v1/issuing/cardholders$",
    rf"^/api/v1/issuing/cardholders/{_ID}$",
    r"^/api/v1/issuing/cards$",
    rf"^/api/v1/issuing/cards/{_ID}$",
)
ALLOW_TXNS = (r"^/api/v1/issuing/transactions$",)
SIM_AMOUNT_MARGIN = "1.00"  # lets a test probe limit + 0.01 without opening the card further

# Who must approve what. Model-started proposals never run below SINGLE unless the spec sets
# agent_min_tier=AUTO, which register_shortfall and reconcile_bridge do (DESIGN.md explains why).
APPROVAL_MATRIX: Mapping[str, Mapping[str, str]] = {
    "register_shortfall": {
        "AUTO": "records a code-computed shortfall in today's bridge queue; moves no money",
        "BLOCKED": "no such obligation, no shortfall, ineligible, already bridged",
    },
    "issue_card": {
        "SINGLE_APPROVER": "always: a card is persistent spending authority",
        "BLOCKED": "tenant inactive, unknown card program, card already issued for this ref",
    },
    "simulate_card_spend": {
        "AUTO": "SCHEDULE origin only: sandbox simulation on a card this kit issued the tenant",
        "SINGLE_APPROVER": "model-started (agent floor): the authorisation reserves funds",
        "BLOCKED": "scenario already run, card not issued by this kit, amount over the cap",
    },
    "bridge_shortfall": {
        "SINGLE_APPROVER": "bridge at or below the high-risk threshold, clean repayment record",
        "DUAL_APPROVER": "bridge above the high-risk threshold, or any late repayment on record",
        "BLOCKED": "no shortfall, ineligible, already bridged or in flight, "
        "or the allocator denies it (reserve floor, outranked)",
    },
    "reconcile_bridge": {
        "AUTO": "reads a bridge in flight; records only a definitive outcome; moves no money",
        "BLOCKED": "nothing in flight, or not definitive: still pending, SUSPENDED, absent or "
        "mismatched under its request_id (a person decides)",
    },
    "collect_fee": {
        "SINGLE_APPROVER": "always: a charge pulls money from the customer wallet",
        "BLOCKED": "no fee schedule, future period, already collected, balance below the fee",
    },
    "collect_repayment": {
        "SINGLE_APPROVER": "always: a charge pulls money from the customer wallet",
        "BLOCKED": "nothing outstanding, deposit not landed (balance below the receivable)",
    },
}

_SETUP: PlatformSetup | None = None

NOT_CONFIGURED = "kit 5 is not configured yet"


class KitNotConfigured(RuntimeError):
    """No platform setup has been seeded: tools answer NOT_CONFIGURED."""


def kit_today(ctx: PolicyContext) -> date:
    """The date the kit treats as today: register_shortfall, bridge_shortfall and
    read_bridge_position all decide "due today" from it, so a read never disagrees with them."""
    return ctx.now.date()


def configure_platform(setup: PlatformSetup | None) -> None:
    """Load the static platform configuration that `SPECS` uses. Configuration only: every
    piece of state lives in the run's `platform_store` and `store`."""
    global _SETUP
    _SETUP = setup


def current_setup(ctx: PolicyContext | None = None) -> PlatformSetup:
    """The platform setup for this call. A process-level `configure_platform` wins (tests,
    operator tools); otherwise the deployed kit reads the setup document an operator seeded
    into the platform store (`seed_platform_setup`). Neither: fail closed."""
    if _SETUP is not None:
        return _SETUP
    if ctx is not None and ctx.platform_store is not None:
        setup = load_platform_setup(ctx.platform_store)
        if setup is not None:
            return setup
    raise KitNotConfigured(
        "no PlatformSetup: seed it into the platform store or call configure_platform()"
    )


def book_for(ctx: PolicyContext, setup: PlatformSetup) -> PlatformBook:
    """A PlatformBook over this run's platform store, built fresh for each call."""
    if ctx.platform_store is None:
        raise StateConflict("kit 5 needs ctx.platform_store for the shared capital pool")
    return PlatformBook(setup, ctx.platform_store)


def _blocked(*reasons: str) -> PolicyDecision:
    return PolicyDecision(RiskTier.BLOCKED, tuple(reasons))


def _balance(ctx: PolicyContext, currency: str) -> Money:
    req = customer_read(ReadRequest(path=BALANCES), ctx.tenant)
    return available_balance(ctx.read(req), currency)


def _tenant_store(ctx: PolicyContext) -> KitStore:
    if ctx.store is None:
        raise StateConflict("kit 5 needs the tenant's KitStore")
    return ctx.store


def _last(ctx: StepContext, step: str) -> OperationResult | None:
    results = ctx.prior.get(step) or ()
    return results[-1] if results else None


# ---- money in flight (M1) ---------------------------------------------------------------
#
# Every money-moving attempt (bridge transfer, fee or repayment charge) records `sent/<rid>`
# in the platform store before its first send: the first send time, and whether any send's
# outcome was left unknown. Two rules follow:
#   - Past SAFE_DEDUPE_MARGIN (6 days, inside Airwallex's ~7-day request_id dedupe window) an
#     attempt that has neither settled nor definitively failed is never re-emitted; it goes to
#     a person (NEEDS_PERSON) and nothing is sent.
#   - Once a send's outcome was unknown, a later create refusal is NOT definitive: only the first
#     send's refusal is. The original may still land, so the money stays in flight on the same
#     request_id until the provider object's own state is read.

SAFE_DEDUPE_MARGIN = DEDUP_RESEND_MARGIN  # the spine's margin, from its kit-facing home
SENT_PREFIX = "sent/"


def _sent(book: PlatformBook, rid: str) -> Mapping[str, Any] | None:
    item = book.store.get(SENT_PREFIX + rid)
    return None if item is None else item.value


def _mark_sent(
    book: PlatformBook, rid: str, kind: str, tenant_id: str, now: datetime, proposal_id: str
) -> None:
    """Idempotent; keeps the earliest send time and the proposal that first built the attempt.

    A record written by ANOTHER proposal means an earlier run built (and may have sent) this
    attempt, perhaps crashing before its postcondition, so this build is a re-send: mark it
    unknown (never cleared). The same proposal building again (rule 0: a build retried before
    its operation was dispatched) is not a re-send: the runner puts every dispatched operation
    in prior, and the executor's own resend refusal comes back OUTCOME_UNKNOWN. The same
    proposal also builds again when Step Functions REDRIVES a failed execution: the redriven
    run keeps its proposal_id, so it is treated as this run, and anything the first execution
    dispatched is in its prior as OUTCOME_UNKNOWN, never as a fresh first send."""
    for _ in range(8):
        item = book.store.get(SENT_PREFIX + rid)
        if item is not None:
            if item.value.get("proposal_id") != proposal_id:
                _mark_unknown(book, rid)
            return
        try:
            book.store.put(
                SENT_PREFIX + rid,
                {
                    "first_sent_at": now,
                    "kind": kind,
                    "tenant_id": tenant_id,
                    "unknown": False,
                    "proposal_id": proposal_id,
                },
                expected_version=None,
            )
            return
        except VersionConflict:
            continue
    raise StateConflict("in-flight record kept conflicting")


def _mark_unknown(book: PlatformBook, rid: str) -> None:
    for _ in range(8):
        item = book.store.get(SENT_PREFIX + rid)
        if item is None or item.value.get("unknown"):
            return
        try:
            book.store.put(
                SENT_PREFIX + rid, {**item.value, "unknown": True}, expected_version=item.version
            )
            return
        except VersionConflict:
            continue
    raise StateConflict("in-flight record kept conflicting")


def needs_person(book: PlatformBook, rid: str, now: datetime) -> str | None:
    """NEEDS_PERSON reason when this attempt is in flight past the safe dedupe margin."""
    sent = _sent(book, rid)
    if sent is None or book.ledger.entry(rid) is not None:
        return None
    if now - sent["first_sent_at"] <= SAFE_DEDUPE_MARGIN:
        return None
    return (
        f"NEEDS_PERSON: {sent['kind']} for tenant {sent['tenant_id']} in flight on request_id "
        f"{rid} since {sent['first_sent_at'].isoformat()}, past the dedupe margin; nothing sent"
    )


def _in_flight(book: PlatformBook, rid: str) -> bool:
    sent = _sent(book, rid)
    return bool(sent and sent.get("unknown")) and book.ledger.entry(rid) is None


def _movement_by_request_id(
    ctx: StepContext,
    book: PlatformBook,
    rid: str,
    list_path: str,
    *,
    amount: Money,
    account_field: str,
) -> Mapping[str, Any] | None:
    """The movement under rid, checked against the approved amount and account; None while it
    is not readable (list lag) or ambiguous. Never a GET by id, which 404s during the lag."""
    if ctx.policy.read_platform is None:
        raise StateConflict("platform reads are not available to this run")
    try:
        return find_movement(
            ctx.policy.read_platform,
            list_path,
            rid,
            amount=amount.amount,
            currency=amount.currency,
            account_field=account_field,
            account=book.tenants.record(ctx.policy.tenant).connected_account_id,
        )
    except (ReconcileUndetermined, AwxError):
        return None  # unreadable now: the money stays in flight, never released on a failed read


def _refusal_after_unknown(
    ctx: StepContext,
    book: PlatformBook,
    rid: str,
    *,
    list_path: str,
    amount: Money,
    account_field: str,
) -> Mapping[str, Any] | None:
    """A create refusal for an attempt whose earlier send was unknown: read the original by
    request_id. Returns it when found; None (still in flight) when absent or unreadable."""
    if ctx.policy.read_platform is None:
        return None
    try:
        return find_movement(
            ctx.policy.read_platform,
            list_path,
            rid,
            amount=amount.amount,
            currency=amount.currency,
            account_field=account_field,
            account=book.tenants.record(ctx.policy.tenant).connected_account_id,
        )
    except (ReconcileUndetermined, AwxError):
        return None  # unreadable now: the money stays in flight, never released on a failed read


# ---- shortfall and bridge ---------------------------------------------------------------


def _validate_obligation(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, {"obligation_ref": REF}, {"rationale": RATIONALE_MAX})


NO_BOOK = "the platform book is not initialised (fund the capital pool first)"


def _book_id(book: PlatformBook) -> str:
    book_id = book.book_id
    if book_id is None:
        raise StateConflict(NO_BOOK)
    return book_id


def bridge_request_id(book: PlatformBook, tenant, obligation_id: str) -> str:
    """The current attempt's request_id. It moves on only after a definitive failure (M1)."""
    book_id = _book_id(book)
    bk = business_key(tenant, book_id, "bridge", obligation_id)
    step = attempt_step(book.ledger.failures(bk))
    return request_id_for(tenant, book_id, "bridge", obligation_id, step)


DISAGREE = "the tenants table and the seeded account disagree"


def _account_disagrees(ctx: PolicyContext, book: PlatformBook) -> PolicyDecision | None:
    """Money goes where the run tenant's connected_account_id (the tenants table)
    points, so every money policy BLOCKs when it differs from the account the setup seeded and
    checked for that tenant. The tenant caller sees only its own tenant id; both account ids go
    to the operator log. An unknown tenant id still raises TenantScopeViolation."""
    seeded = book.tenants.resolve(ctx.tenant.tenant_id)
    if seeded.connected_account_id == ctx.tenant.connected_account_id:
        return None
    log.warning(
        "%s for %s: tenants row %s, seeded %s",
        DISAGREE,
        ctx.tenant.tenant_id,
        ctx.tenant.connected_account_id,
        seeded.connected_account_id,
    )
    return _blocked(f"{DISAGREE} for tenant {ctx.tenant.tenant_id}; an operator must reconcile")


def _shortfall(intent, ctx: PolicyContext, book: PlatformBook):
    """(candidate, rid, available, None) or (None, None, None, blocked decision). Pure."""
    disagree = _account_disagrees(ctx, book)
    if disagree is not None:
        return None, None, None, disagree
    record = book.tenants.record(ctx.tenant)  # raises TenantScopeViolation on a forged context
    try:
        ob = book.obligation(ctx.tenant, intent["obligation_ref"])
    except NotFound:
        return None, None, None, _blocked("no such obligation for this tenant")
    if book.book_id is None:
        return None, None, None, _blocked(NO_BOOK)
    rid = bridge_request_id(book, ctx.tenant, ob.obligation_id)
    if book.ledger.entry(rid) is not None:
        return None, None, None, _blocked("this obligation has already been bridged")
    if book.ledger.hold(rid) is not None:
        return None, None, None, _blocked("a bridge for this obligation is already in flight")
    available = _balance(ctx, book.config.currency)
    gap = ob.amount_due - available
    if gap.amount <= 0:
        return None, None, None, _blocked("no shortfall: the customer wallet covers the payment")
    candidate = BridgeCandidate(
        tenant_id=record.tenant_id,
        obligation_id=ob.obligation_id,
        shortfall=gap,
        due_on=ob.due_on,
        expected_deposit_on=ob.expected_deposit_on,
        late_repayments=book.late_repayments.get(record.tenant_id, 0),
        has_open_advance=book.ledger.has_open_advance(record.tenant_id),
        tenant_active=record.active,
    )
    return candidate, rid, available, None


def assess_register(intent, ctx: PolicyContext, book: PlatformBook) -> PolicyDecision:
    candidate, _rid, available, blocked = _shortfall(intent, ctx, book)
    if blocked is not None:
        return blocked
    reason = eligibility(candidate, book.config, kit_today(ctx))
    if reason is not None:
        return _blocked(f"not eligible for a bridge: {reason}")
    return PolicyDecision(
        RiskTier.AUTO,
        (f"queue a {candidate.shortfall} shortfall for today's bridge ranking",),
        {"shortfall": candidate.shortfall, "observed_available": available},
    )


def _build_register(ctx: StepContext, book: PlatformBook) -> Sequence[Operation]:
    candidate, _rid, _available, blocked = _shortfall(ctx.intent, ctx.policy, book)
    if blocked is not None or candidate.shortfall != ctx.decision.computed["shortfall"]:
        raise StateConflict("approval invalidated: the shortfall changed")
    book.register_candidate(candidate, observed_at=ctx.policy.now)
    return ()


def _register_postcondition(ctx: StepContext, book: PlatformBook) -> PostconditionResult:
    key = (ctx.policy.tenant.tenant_id, ctx.intent["obligation_ref"])
    if book.queue_entry(key) is None:
        return PostconditionResult(False, "shortfall was not queued")
    return PostconditionResult(True, f"queued {ctx.decision.computed['shortfall']}")


def _wallet_room(ctx: PolicyContext, book: PlatformBook) -> Money | None:
    """What the platform wallet really holds above the floor, net of committed holds.

    observed - held - floor is conservative: a hold whose create already landed has moved money
    out of the wallet and is subtracted twice. That errs towards denying, never towards a breach.
    None when the run has no platform read."""
    if ctx.read_platform is None:
        return None
    observed = available_balance(
        ctx.read_platform(ReadRequest(path=BALANCES)), book.config.currency
    )
    return observed - book.ledger.held - book.config.reserve_floor


WALLET_OBSERVATION_ATTEMPTS = 5


def _place_bridge_hold(ctx: StepContext, book: PlatformBook, rid: str, approved: Money) -> Hold:
    """Place this run's hold with the wallet cap checked inside the ledger's own write.

    The wallet is read between two ledger reads at the same version; if the ledger moves before
    the hold lands, the wallet is read again. A run whose build cannot read the platform wallet
    places no hold (fail closed)."""
    if ctx.policy.read_platform is None:
        raise StateConflict("bridges need the platform wallet read; none is wired")
    for _ in range(WALLET_OBSERVATION_ATTEMPTS):
        version = book.ledger.version
        observed = available_balance(
            ctx.policy.read_platform(ReadRequest(path=BALANCES)), book.config.currency
        )
        try:
            hold, _created = book.ledger.place_hold(
                rid,
                ctx.policy.tenant.tenant_id,
                approved,
                owner=ctx.proposal_id,  # M4: owned by this run, never by the shared intent hash
                wallet=WalletObservation(version, observed),
            )
            return hold
        except StaleObservation:
            continue
        except LedgerError as exc:
            raise StateConflict(f"approval invalidated: {exc}") from exc
    raise StateConflict("the capital ledger kept moving; propose the bridge again")


def _platform_headroom(ctx: PolicyContext, book: PlatformBook) -> Money:
    """Ledger headroom, capped by the platform wallet's observed balance."""
    ledger_room = book.ledger.headroom()
    wallet_room = _wallet_room(ctx, book)
    return ledger_room if wallet_room is None else min(ledger_room, wallet_room)


def assess_bridge(
    intent, ctx: PolicyContext, book: PlatformBook, spec_ttl: timedelta
) -> PolicyDecision:
    if ctx.read_platform is None:
        # Without the platform wallet read the floor would bind on the books alone: fail
        # closed.
        return _blocked("bridges need the platform wallet read; none is wired")
    candidate, rid, available, blocked = _shortfall(intent, ctx, book)
    if blocked is not None:
        return blocked
    today = kit_today(ctx)
    pool = book.ranking_pool(today=today, now=ctx.now, max_age=spec_ttl, exclude={candidate.key})
    pool.append(candidate)
    headroom = _platform_headroom(ctx, book)
    funded, reason, rank = allocate(pool, headroom, book.config, today).decision_for(candidate.key)
    if not funded:
        return _blocked(f"bridge denied: {reason}")
    cfg = book.config
    gap = candidate.shortfall
    reasons = [
        f"bridge {gap} so the {intent['obligation_ref']} payment due today is covered; "
        f"wallet holds {available}",
        "platform capital is available for it within the reserve floor",
    ]
    # Platform-scope figures (capital headroom, rank among other tenants' requests, policy
    # thresholds) never go into reasons: the dispatcher returns reasons to the tenant caller.
    # Operators get them in the Lambda log and in the ledger itself.
    log.info(
        "bridge %s for %s: rank %s of %s, headroom %s",
        rid,
        candidate.tenant_id,
        rank,
        len(pool),
        headroom,
    )
    tier = RiskTier.SINGLE
    if gap > cfg.high_risk_bridge:
        tier = RiskTier.DUAL
        reasons.append("large bridge: needs two approvers")
    if candidate.late_repayments:
        tier = RiskTier.DUAL
        reasons.append(f"{candidate.late_repayments} late repayment(s) on record")
    # Hash-stable values only: the approval binds to these; headroom and rank are reasons.
    return PolicyDecision(
        tier,
        tuple(reasons),
        {"amount": gap, "observed_available": available, "request_id": rid},
    )


def _build_bridge(ctx: StepContext, book: PlatformBook, ttl: timedelta) -> Sequence[Operation]:
    tenant = ctx.policy.tenant
    obligation_id = ctx.intent["obligation_ref"]
    rid = ctx.decision.computed["request_id"]
    approved: Money = ctx.decision.computed["amount"]
    if book.ledger.entry(rid) is not None:
        return ()  # already settled: nothing to send
    if book.ledger.hold(rid) is None:
        # Defence in depth on top of rule 1: the approval bound to this amount and balance.
        fresh = assess_bridge(ctx.intent, ctx.policy, book, ttl)
        if fresh.risk_tier is RiskTier.BLOCKED:
            raise StateConflict("approval invalidated: " + "; ".join(fresh.reasons))
        if fresh.computed != ctx.decision.computed or fresh.risk_tier is not ctx.decision.risk_tier:
            raise StateConflict("approval invalidated: amount, balance or tier changed")
    # Build the operation first, so nothing after the hold can raise.
    op = bridge_operation(
        tenant,
        book.tenants,
        book_id=_book_id(book),
        obligation_id=obligation_id,
        amount=approved,
        step=rid_step(book, tenant, "bridge", obligation_id, rid),
        intent_hash=ctx.intent_hash,
    )
    hold = _place_bridge_hold(ctx, book, rid, approved)
    if hold.owner != ctx.proposal_id:
        # Placed by another run (an identical proposal in another window): its money may be in
        # flight, so this run sends nothing and releases nothing.
        raise StateConflict("another run's bridge for this obligation is in flight")
    person = needs_person(book, rid, ctx.policy.now)
    if person is not None:
        raise StateConflict(person)
    _mark_sent(book, rid, "bridge", tenant.tenant_id, ctx.policy.now, ctx.proposal_id)
    # With this run's hold already in place (a re-run of the same execution) this resends the
    # same request_id and body; the operation store and Airwallex both refuse a second transfer.
    return (op,)


def rid_step(book: PlatformBook, tenant, kind: str, ref: str, rid: str) -> str:
    book_id = _book_id(book)
    step = attempt_step(book.ledger.failures(business_key(tenant, book_id, kind, ref)))
    if request_id_for(tenant, book_id, kind, ref, step) != rid:
        raise StateConflict("the approved attempt is no longer current")
    return step


def _bridge_postcondition(ctx: StepContext, book: PlatformBook) -> PostconditionResult:
    """Runs whenever the steps stop (rule 2), success or not."""
    tenant = ctx.policy.tenant
    key = (tenant.tenant_id, ctx.intent["obligation_ref"])
    rid = ctx.decision.computed["request_id"]
    bk = business_key(tenant, _book_id(book), "bridge", ctx.intent["obligation_ref"])
    approved: Money = ctx.decision.computed["amount"]
    last = _last(ctx, "bridge")
    if last is None:
        if book.ledger.entry(rid) is not None:
            book.drop_candidate(key)
            return PostconditionResult(True, "already settled")
        if _sent(book, rid) is not None:
            # The build got as far as handing the operation over, so it may have left even if
            # the runner kept no result for it: in flight, never released (M1).
            _mark_unknown(book, rid)
            return PostconditionResult(False, "the bridge may have been sent; hold kept")
        # Nothing was handed over in this run. Release only a hold this run placed (M4); another
        # run's hold covers money that may be in flight and stays until a definitive read.
        released = book.ledger.release_hold(rid, owner=ctx.proposal_id)
        detail = "nothing was sent; this run's hold released" if released else "nothing was sent"
        return PostconditionResult(False, detail)
    outcome = interpret_create(last)
    body: Mapping[str, Any] | None = None
    if outcome is CreateOutcome.REJECTED and _in_flight(book, rid):
        # A refused resend is not definitive: the earlier unknown send may still land (M1).
        body = _refusal_after_unknown(
            ctx, book, rid, list_path=TRANSFERS_PATH, amount=approved, account_field="destination"
        )
        if body is None:
            return PostconditionResult(
                False, "resend refused after an unknown send; hold kept, still in flight"
            )
    elif outcome is CreateOutcome.REJECTED:
        book.ledger.record_failure(bk, rid)  # releases the hold; next proposal is a new attempt
        book.drop_candidate(key)
        return PostconditionResult(False, f"bridge rejected ({last.error_code}); hold released")
    elif outcome is not CreateOutcome.ACCEPTED:
        _mark_unknown(book, rid)
        return PostconditionResult(False, "bridge outcome unknown; hold kept until reconciled")
    if body is None:
        body = _movement_by_request_id(
            ctx, book, rid, TRANSFERS_PATH, amount=approved, account_field="destination"
        )
        if body is None:
            _mark_unknown(book, rid)
            return PostconditionResult(False, "bridge not readable by request_id yet; hold kept")
    status = body.get("status")
    if status == MOVEMENT_FAILED:
        book.ledger.record_failure(bk, rid)
        book.drop_candidate(key)
        return PostconditionResult(False, "bridge FAILED at the provider; hold released")
    if status == SUSPENDED:
        _mark_unknown(book, rid)
        return PostconditionResult(False, "bridge SUSPENDED: needs a person; hold kept")
    if status != SETTLED:
        _mark_unknown(book, rid)
        return PostconditionResult(False, f"bridge is {status}, not SETTLED; hold kept")
    account = book.tenants.record(tenant).connected_account_id
    if body.get("destination") != account or body.get("request_id") != rid:
        return PostconditionResult(False, "settled bridge does not match the operation")
    if provider_money(body.get("amount"), str(body.get("currency"))) != approved:
        return PostconditionResult(False, "settled bridge amount differs from the approved amount")
    book.ledger.post_advance(
        rid, tenant.tenant_id, approved, provider_id=str(body["id"]), at=ctx.policy.now
    )
    book.drop_candidate(key)
    return PostconditionResult(
        True, f"bridge {approved} SETTLED; receivable {book.ledger.receivable(tenant.tenant_id)}"
    )


def _bridge_abandon(ctx: StepContext, reason: str, book: PlatformBook) -> None:
    """REJECTED, EXPIRED, BLOCKED or STALE. No step ran (rule 8), so this run placed no hold:
    give up the queue place only and never touch the ledger (M4)."""
    book.drop_candidate((ctx.policy.tenant.tenant_id, ctx.intent["obligation_ref"]))


# ---- reconcile a bridge in flight (M1) --------------------------------------------------


def assess_reconcile(intent, ctx: PolicyContext, book: PlatformBook) -> PolicyDecision:
    """Pure: read the bridge in flight under its request_id and decide whether its outcome is
    definitive. Anything not definitive keeps the hold and goes to a person."""
    disagree = _account_disagrees(ctx, book)
    if disagree is not None:
        return disagree
    record = book.tenants.record(ctx.tenant)
    try:
        ob = book.obligation(ctx.tenant, intent["obligation_ref"])
    except NotFound:
        return _blocked("no such obligation for this tenant")
    if book.book_id is None:
        return _blocked(NO_BOOK)
    rid = bridge_request_id(book, ctx.tenant, ob.obligation_id)
    if book.ledger.entry(rid) is not None:
        return _blocked("this bridge has already settled")
    hold = book.ledger.hold(rid)
    if hold is None:
        return _blocked("no bridge for this obligation is in flight")
    if ctx.read_platform is None:
        raise StateConflict("reconcile_bridge needs platform reads")
    try:
        found = find_movement(
            ctx.read_platform,
            TRANSFERS_PATH,
            rid,
            amount=hold.amount.amount,
            currency=hold.amount.currency,
            account_field="destination",
            account=record.connected_account_id,
        )
    except ReconcileUndetermined as exc:
        return _blocked(f"needs a person: {exc}")
    if found is None:
        return _blocked(
            "needs a person: no transfer under this request_id; an empty read-back is not a "
            "failure, so the hold stays (M1)"
        )
    status = found.get("status")
    if status == SUSPENDED:
        return _blocked("needs a person: the bridge is SUSPENDED at the provider")
    if status not in (SETTLED, MOVEMENT_FAILED):
        return _blocked(f"the bridge is still {status}; reconcile again later")
    return PolicyDecision(
        RiskTier.AUTO,
        (f"the bridge {rid} is {status} at the provider; record it",),
        {
            "outcome": status,
            "request_id": rid,
            "provider_id": str(found.get("id")),
            "amount": hold.amount,
        },
    )


def _build_reconcile(ctx: StepContext, book: PlatformBook) -> Sequence[Operation]:
    """Records the definitive outcome the approval bound to. Sends nothing."""
    computed = ctx.decision.computed
    rid = computed["request_id"]
    tenant = ctx.policy.tenant
    if book.ledger.entry(rid) is not None:
        return ()  # a re-run after settlement was recorded
    fresh = assess_reconcile(ctx.intent, ctx.policy, book)
    if fresh.computed != computed:
        raise StateConflict("the bridge's provider state changed since this run was proposed")
    key = (tenant.tenant_id, ctx.intent["obligation_ref"])
    if computed["outcome"] == SETTLED:
        book.ledger.post_advance(
            rid,
            tenant.tenant_id,
            computed["amount"],
            provider_id=computed["provider_id"],
            at=ctx.policy.now,
        )
    else:
        bk = business_key(tenant, _book_id(book), "bridge", ctx.intent["obligation_ref"])
        book.ledger.record_failure(bk, rid)
    book.drop_candidate(key)
    return ()


def _reconcile_postcondition(ctx: StepContext, book: PlatformBook) -> PostconditionResult:
    rid = ctx.decision.computed["request_id"]
    if ctx.decision.computed["outcome"] == SETTLED:
        ok = book.ledger.entry(rid) is not None
        return PostconditionResult(ok, "advance posted" if ok else "advance not posted")
    ok = book.ledger.hold(rid) is None
    return PostconditionResult(ok, "failure recorded; hold released" if ok else "hold remains")


# ---- charges (fee, repayment) -----------------------------------------------------------


def _validate_fee(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, {"period": PERIOD}, {"rationale": RATIONALE_MAX})


def _validate_none(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, {}, {"rationale": RATIONALE_MAX})


def charge_request_id(book: PlatformBook, tenant, kind: str, ref: str) -> str:
    """The current attempt; it moves on only after a definitive failure (M1)."""
    book_id = _book_id(book)
    bk = business_key(tenant, book_id, kind, ref)
    return request_id_for(tenant, book_id, kind, ref, attempt_step(book.ledger.failures(bk)))


def assess_fee(intent, ctx: PolicyContext, book: PlatformBook) -> PolicyDecision:
    disagree = _account_disagrees(ctx, book)
    if disagree is not None:
        return disagree
    record = book.tenants.record(ctx.tenant)
    fee = book.monthly_fees.get(record.tenant_id)
    if fee is None:
        return _blocked("no fee schedule for this tenant")
    period = intent["period"]
    if period > ctx.now.strftime("%Y-%m"):
        return _blocked("cannot collect a fee for a future period")
    if book.book_id is None:
        return _blocked(NO_BOOK)
    if _fee_posted(book, ctx.tenant, period):
        return _blocked(f"fee for {period} already collected")
    rid = charge_request_id(book, ctx.tenant, "fee", period)
    person = needs_person(book, rid, ctx.now)
    if person is not None:
        return _blocked(person)
    if _in_flight(book, rid):
        # The same charge is in flight: re-emit it on the same request_id so the runner
        # reconciles first. The wallet may already be debited, so skip the balance check.
        reason = f"re-emit the {period} fee {fee} in flight on {rid} so it is reconciled"
    else:
        available = _balance(ctx, fee.currency)
        if available < fee:
            return _blocked(f"wallet holds {available}, below the {fee} fee")
        reason = f"charge the {period} platform fee {fee}"
    return PolicyDecision(
        RiskTier.SINGLE, (reason,), {"amount": fee, "ref": period, "request_id": rid}
    )


def _fee_posted(book: PlatformBook, tenant, period: str) -> bool:
    book_id = _book_id(book)
    bk = business_key(tenant, book_id, "fee", period)
    n = book.ledger.failures(bk)
    return any(
        book.ledger.entry(request_id_for(tenant, book_id, "fee", period, attempt_step(i)))
        is not None
        for i in range(n + 1)
    )


def _open_advance_key(book: PlatformBook, tenant_id: str) -> str | None:
    keys = [
        e.key
        for e in book.ledger.entries()
        if e.kind is EntryKind.ADVANCE and e.tenant_id == tenant_id
    ]
    return keys[-1] if keys else None


def assess_repayment(intent, ctx: PolicyContext, book: PlatformBook) -> PolicyDecision:
    disagree = _account_disagrees(ctx, book)
    if disagree is not None:
        return disagree
    record = book.tenants.record(ctx.tenant)
    outstanding = book.ledger.receivable(record.tenant_id)
    advance = _open_advance_key(book, record.tenant_id)
    if outstanding.amount <= 0 or advance is None:
        return _blocked("nothing outstanding")
    rid = charge_request_id(book, ctx.tenant, "repayment", advance)
    person = needs_person(book, rid, ctx.now)
    if person is not None:
        return _blocked(person)
    if _in_flight(book, rid):
        # A repayment that may have landed already lowered the wallet: re-emit the same
        # request_id for reconciliation instead of reporting "deposit not landed".
        reason = f"re-emit the {outstanding} repayment in flight on {rid} so it is reconciled"
    else:
        available = _balance(ctx, outstanding.currency)
        if available < outstanding:
            return _blocked(
                f"deposit has not landed: wallet {available} below receivable {outstanding}"
            )
        reason = f"collect the {outstanding} bridge advance back now the deposit has landed"
    return PolicyDecision(
        RiskTier.SINGLE, (reason,), {"amount": outstanding, "ref": advance, "request_id": rid}
    )


def _build_charge(kind: str, assess: Callable[..., PolicyDecision]):
    def build(ctx: StepContext, book: PlatformBook) -> Sequence[Operation]:
        rid = ctx.decision.computed["request_id"]
        if book.ledger.entry(rid) is not None:
            return ()
        fresh = assess(ctx.intent, ctx.policy, book)
        if fresh.risk_tier is RiskTier.BLOCKED or fresh.computed != ctx.decision.computed:
            raise StateConflict("approval invalidated: " + "; ".join(fresh.reasons))
        person = needs_person(book, rid, ctx.policy.now)
        if person is not None:
            raise StateConflict(person)
        _mark_sent(book, rid, kind, ctx.policy.tenant.tenant_id, ctx.policy.now, ctx.proposal_id)
        ref = ctx.decision.computed["ref"]
        return (
            charge_operation(
                ctx.policy.tenant,
                book.tenants,
                book_id=_book_id(book),
                kind=kind,
                ref=ref,
                amount=ctx.decision.computed["amount"],
                step=rid_step(book, ctx.policy.tenant, kind, ref, rid),
                intent_hash=ctx.intent_hash,
            ),
        )

    return build


def _charge_postcondition(kind: str):
    def post(ctx: StepContext, book: PlatformBook) -> PostconditionResult:
        tenant = ctx.policy.tenant
        rid = ctx.decision.computed["request_id"]
        bk = business_key(tenant, _book_id(book), kind, ctx.decision.computed["ref"])
        amount: Money = ctx.decision.computed["amount"]
        last = _last(ctx, "charge")
        if last is None:
            if book.ledger.entry(rid) is not None:
                return PostconditionResult(True, "already collected")
            if _sent(book, rid) is not None:
                _mark_unknown(book, rid)  # it may have left; in flight (M1)
                return PostconditionResult(False, f"{kind} may have been sent; in flight on {rid}")
            return PostconditionResult(False, "nothing was sent")
        outcome = interpret_create(last)
        body: Mapping[str, Any] | None = None
        if outcome is CreateOutcome.REJECTED and _in_flight(book, rid):
            # A refused resend is not definitive: the earlier unknown send may still land (M1).
            body = _refusal_after_unknown(
                ctx, book, rid, list_path=CHARGES_PATH, amount=amount, account_field="source"
            )
            if body is None:
                return PostconditionResult(
                    False, f"{kind} resend refused after an unknown send; still in flight on {rid}"
                )
        elif outcome is CreateOutcome.REJECTED:
            book.ledger.record_failure(bk, rid)  # a later proposal uses the next attempt
            return PostconditionResult(False, f"{kind} rejected ({last.error_code})")
        elif outcome is not CreateOutcome.ACCEPTED:
            # M1: in flight on this request_id. A later proposal derives the same request_id
            # and body, so the runner reconciles it before anything is resent.
            _mark_unknown(book, rid)
            return PostconditionResult(False, f"{kind} outcome unknown; in flight on {rid}")
        if body is None:
            body = _movement_by_request_id(
                ctx, book, rid, CHARGES_PATH, amount=amount, account_field="source"
            )
            if body is None:
                _mark_unknown(book, rid)
                return PostconditionResult(False, f"{kind} not readable yet; in flight on {rid}")
        status = body.get("status")
        if status == MOVEMENT_FAILED:
            book.ledger.record_failure(bk, rid)
            return PostconditionResult(False, f"{kind} FAILED at the provider")
        if status != SETTLED:
            _mark_unknown(book, rid)
            return PostconditionResult(False, f"{kind} is {status}, not SETTLED")
        if body.get("source") != book.tenants.record(tenant).connected_account_id:
            return PostconditionResult(False, "settled charge came from a different account")
        if provider_money(body.get("amount"), str(body.get("currency"))) != amount:
            return PostconditionResult(
                False, "settled charge amount differs from the approved amount"
            )
        poster = book.ledger.post_fee if kind == "fee" else book.ledger.post_repayment
        poster(rid, tenant.tenant_id, amount, provider_id=str(body["id"]), at=ctx.policy.now)
        return PostconditionResult(True, f"{kind} {amount} SETTLED")

    return post


# ---- cards -----------------------------------------------------------------------------


def _card_key(card_id: str) -> str:
    return f"card/{card_id}"


def _card_ref_key(ref: str) -> str:
    return f"card-ref/{ref}"


def _sim_key(scenario_ref: str) -> str:
    return f"sim/{scenario_ref}"


TENANT_BOOK_KEY = "book"


def tenant_book_id(store: KitStore, *, create: bool) -> str | None:
    """The tenant's book id (M2), minted once with the tenant's kit state. Only builds create."""
    item = store.get(TENANT_BOOK_KEY)
    if item is not None:
        return str(item.value["book_id"])
    if not create:
        return None
    try:
        store.put(TENANT_BOOK_KEY, {"book_id": uuid.uuid4().hex}, expected_version=None)
    except VersionConflict:
        pass  # another writer minted it first; use theirs
    return str(store.get(TENANT_BOOK_KEY).value["book_id"])


def _validate_card(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return strict(raw, {"cardholder_ref": REF, "program": REF}, {"rationale": RATIONALE_MAX})


def assess_card(intent, ctx: PolicyContext, book: PlatformBook) -> PolicyDecision:
    disagree = _account_disagrees(ctx, book)
    if disagree is not None:
        return disagree
    record = book.tenants.record(ctx.tenant)
    if not record.active:
        return _blocked("tenant is not active")
    program = book.config.card_programs.get(intent["program"])
    if program is None:
        return _blocked("unknown card program")
    if _tenant_store(ctx).get(_card_ref_key(intent["cardholder_ref"])) is not None:
        return _blocked("a card has already been issued for this cardholder")
    return PolicyDecision(
        RiskTier.SINGLE,
        (
            f"issue a virtual card under {program.name}: {program.per_transaction_limit} per "
            f"transaction, {program.monthly_limit} a month",
        ),
        {
            "per_transaction_limit": program.per_transaction_limit,
            "monthly_limit": program.monthly_limit,
            "allowed_currencies": list(program.allowed_currencies),
            "allowed_merchant_categories": list(program.allowed_merchant_categories),
        },
    )


def _build_cardholder(ctx: StepContext, book: PlatformBook) -> Sequence[Operation]:
    return (
        cardholder_operation(
            ctx.policy.tenant,
            book.tenants,
            book_id=tenant_book_id(_tenant_store(ctx.policy), create=True),
            cardholder_ref=ctx.intent["cardholder_ref"],
            intent_hash=ctx.intent_hash,
        ),
    )


CARD_PENDING_PREFIX = "card-pending/"


def card_pending_key(tenant_id: str, cardholder_ref: str) -> str:
    return f"{CARD_PENDING_PREFIX}{tenant_id}/{cardholder_ref}"


def _build_card(ctx: StepContext, book: PlatformBook) -> Sequence[Operation]:
    holder = _last(ctx, "create_cardholder")
    if holder is None or holder.status is not OperationStatus.SUCCEEDED or not holder.provider_id:
        raise StateConflict("no cardholder id from the previous step")
    # Before the first card create: the program's terms are on this request_id's body, so an
    # operator replace must not change them until the card is recorded.
    key = card_pending_key(ctx.policy.tenant.tenant_id, ctx.intent["cardholder_ref"])
    if book.store.get(key) is None:
        try:
            book.store.put(
                key,
                {"tenant_id": ctx.policy.tenant.tenant_id, "program": ctx.intent["program"]},
                expected_version=None,
            )
        except VersionConflict:
            pass  # a concurrent run of the same ref wrote it first
    return (
        card_operation(
            ctx.policy.tenant,
            book.tenants,
            book_id=_require_tenant_book(ctx.policy),
            cardholder_ref=ctx.intent["cardholder_ref"],
            cardholder_id=holder.provider_id,
            program=book.config.card_programs[ctx.intent["program"]],
            intent_hash=ctx.intent_hash,
        ),
    )


def _require_tenant_book(ctx: PolicyContext) -> str:
    book_id = tenant_book_id(_tenant_store(ctx), create=False)
    if book_id is None:
        raise StateConflict("the tenant's kit state has no book id")
    return book_id


def _put_once(store: KitStore, key: str, value: Mapping[str, Any]) -> None:
    item = store.get(key)
    if item is not None:
        if dict(item.value) != dict(value):
            raise StateConflict(f"{key} already recorded with different content")
        return
    try:
        store.put(key, value, expected_version=None)
    except VersionConflict:
        _put_once(store, key, value)


def _mark_card_pending(book: PlatformBook, key: str, *, unknown: bool) -> None:
    """Update the card-pending record. An unknown send sets `unknown` for
    good and keeps the pin. A definitive first-send refusal, with no unknown send before it,
    marks it resolved "failed", which unpins the program's terms."""
    while True:
        item = book.store.get(key)
        if item is None:
            return  # no create was built: nothing pins the program
        value = dict(item.value)
        if unknown:
            if value.get("unknown") and "resolved" not in value:
                return
            value["unknown"] = True
            value.pop("resolved", None)
        else:
            if value.get("unknown") or value.get("resolved") == "failed":
                return  # after an unknown send a refusal is not definitive (M1)
            value["resolved"] = "failed"
        try:
            book.store.put(key, value, expected_version=item.version)
            return
        except VersionConflict:
            continue


def _card_postcondition(ctx: StepContext, book: PlatformBook) -> PostconditionResult:
    card = _last(ctx, "create_card")
    pending = card_pending_key(ctx.policy.tenant.tenant_id, ctx.intent["cardholder_ref"])
    if card is None:
        # No create result this run: the record (if any) keeps whatever it already says, and
        # an unresolved record pins its program by default.
        return PostconditionResult(False, "no card was created")
    outcome = interpret_create(card)
    if outcome is CreateOutcome.REJECTED:
        _mark_card_pending(book, pending, unknown=False)
        return PostconditionResult(False, f"card create refused ({card.error_code})")
    if outcome is not CreateOutcome.ACCEPTED or not card.provider_id:
        _mark_card_pending(book, pending, unknown=True)
        return PostconditionResult(False, "card create outcome unknown; its program stays pinned")
    req = customer_read(
        ReadRequest(path=f"/api/v1/issuing/cards/{card.provider_id}"), ctx.policy.tenant
    )
    body = ctx.policy.read(req)
    store = _tenant_store(ctx.policy)
    ref = ctx.intent["cardholder_ref"]
    # The registry records the card even if it is not ACTIVE yet: it exists and is ours.
    _put_once(
        store,
        _card_key(card.provider_id),
        {"cardholder_ref": ref, "program": ctx.intent["program"]},
    )
    _put_once(store, _card_ref_key(ref), {"card_id": card.provider_id})
    status = body.get("card_status")
    if status != "ACTIVE":
        return PostconditionResult(False, f"card is {status}, not ACTIVE")
    return PostconditionResult(True, "card ACTIVE on the tenant's connected account")


def _validate_sim(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    out = strict(
        raw,
        {
            "scenario_ref": REF,
            "card_id": CARD_ID,
            "amount": AMOUNT,
            "currency": CURRENCY,
            "merchant_category_code": MCC,
        },
        {},
    )
    amount_of(out["amount"], out["currency"])  # scale check
    return out


def assess_sim(intent, ctx: PolicyContext, book: PlatformBook) -> PolicyDecision:
    disagree = _account_disagrees(ctx, book)
    if disagree is not None:
        return disagree
    book.tenants.record(ctx.tenant)
    store = _tenant_store(ctx)
    if store.get(_sim_key(intent["scenario_ref"])) is not None:
        return _blocked("scenario already simulated; a replay would create a second authorisation")
    card = store.get(_card_key(intent["card_id"]))
    if card is None:
        return _blocked("card was not issued by this kit for this tenant")
    program = book.config.card_programs[card.value["program"]]
    amount = amount_of(intent["amount"], intent["currency"])
    cap = (
        program.per_transaction_limit.amount + Money.of(SIM_AMOUNT_MARGIN, program.currency).amount
    )
    if amount.amount > cap:
        return _blocked(f"simulated amount above the {cap} cap for this card")
    return PolicyDecision(
        RiskTier.AUTO,
        (f"sandbox spend simulation of {amount} at MCC {intent['merchant_category_code']}",),
        {"amount": amount},
    )


def _build_sim(ctx: StepContext, book: PlatformBook) -> Sequence[Operation]:
    store = _tenant_store(ctx.policy)
    key = _sim_key(ctx.intent["scenario_ref"])

    def op_at(not_before: datetime) -> Operation:
        return card_simulation_operation(
            ctx.policy.tenant,
            book.tenants,
            book_id=_require_tenant_book(ctx.policy),
            scenario_ref=ctx.intent["scenario_ref"],
            card_id=ctx.intent["card_id"],
            amount=ctx.decision.computed["amount"],
            merchant_category_code=ctx.intent["merchant_category_code"],
            not_before=not_before,
            intent_hash=ctx.intent_hash,
        )

    existing = store.get(key)
    if existing is not None:
        # A re-run of this execution: reuse the first send time so the operation is identical.
        op = op_at(existing.value["not_before"])
        if existing.value["request_id"] != op.request_id:
            raise StateConflict("scenario already used by another simulation")
        return (op,)
    op = op_at(ctx.policy.now)
    # Recorded BEFORE the call: if the call errors after creating the authorisation, the
    # scenario is already spent and a re-proposal is refused, never re-sent.
    _put_once(store, key, {"request_id": op.request_id, "not_before": ctx.policy.now})
    return (op,)


def _sim_postcondition(ctx: StepContext, book: PlatformBook) -> PostconditionResult:
    last = _last(ctx, "simulate")
    if last is None:
        return PostconditionResult(False, "nothing was sent")
    if last.status is OperationStatus.OUTCOME_UNKNOWN:
        return PostconditionResult(False, "simulation outcome unknown; not resent")
    if last.status is not OperationStatus.SUCCEEDED or last.body is None:
        return PostconditionResult(False, f"simulation failed ({last.error_code})")
    outcome, reason = card_transaction_outcome(last.body)
    if outcome is CardTxnOutcome.PENDING:
        return PostconditionResult(False, "card transaction still pending")
    return PostconditionResult(True, f"{outcome}" + (f" ({reason})" if reason else ""))


# ---- specs -----------------------------------------------------------------------------

BRIDGE_TTL = timedelta(hours=4)


def build_specs(setup: SetupProvider) -> dict[str, KitWorkflowSpec]:
    """Each call builds its PlatformBook over the context's platform_store."""

    def pol(fn):
        def policy(intent, ctx):
            try:
                current = setup(ctx)
            except KitNotConfigured:
                return _blocked(NOT_CONFIGURED)  # a stated answer, never INTERNAL
            return fn(intent, ctx, book_for(ctx, current))

        return policy

    def b(fn):  # build or postcondition: (StepContext)
        return lambda ctx: fn(ctx, book_for(ctx.policy, setup(ctx.policy)))

    def ab(fn):  # on_abandon: (StepContext, reason)
        return lambda ctx, reason: fn(ctx, reason, book_for(ctx.policy, setup(ctx.policy)))

    specs = [
        KitWorkflowSpec(
            approver=ApproverScope.OWN_TENANT,  # the requester's own tenant approves
            kit=KIT,
            action="register_shortfall",
            validate=_validate_obligation,
            policy=pol(assess_register),
            steps=(StepSpec("register", b(_build_register)),),
            approval_ttl=timedelta(hours=1),
            read_allowlist=(ALLOW_BALANCES,),
            postcondition=b(_register_postcondition),
            agent_min_tier=RiskTier.AUTO,
        ),
        KitWorkflowSpec(
            approver=ApproverScope.KIT_OPERATOR,  # the kit 5 operator approves
            kit=KIT,
            action="bridge_shortfall",
            validate=_validate_obligation,
            policy=pol(lambda i, c, bk: assess_bridge(i, c, bk, BRIDGE_TTL)),
            steps=(
                StepSpec(
                    "bridge",
                    b(lambda c, bk: _build_bridge(c, bk, BRIDGE_TTL)),
                    BRIDGE_FINALITY,
                    bridge_reconcile,
                ),
            ),
            approval_ttl=BRIDGE_TTL,
            read_allowlist=(ALLOW_BALANCES,),
            platform_read_allowlist=(ALLOW_BALANCES, *PLATFORM_TRANSFERS),
            postcondition=b(_bridge_postcondition),
            on_abandon=ab(_bridge_abandon),
        ),
        KitWorkflowSpec(
            approver=ApproverScope.OWN_TENANT,  # the requester's own tenant approves
            kit=KIT,
            action="reconcile_bridge",
            validate=_validate_obligation,
            policy=pol(assess_reconcile),
            steps=(StepSpec("apply", b(_build_reconcile)),),
            approval_ttl=timedelta(hours=1),
            read_allowlist=(ALLOW_BALANCES,),
            platform_read_allowlist=PLATFORM_TRANSFERS,
            postcondition=b(_reconcile_postcondition),
            agent_min_tier=RiskTier.AUTO,
        ),
        KitWorkflowSpec(
            approver=ApproverScope.OWN_TENANT,  # the requester's own tenant approves
            kit=KIT,
            action="collect_fee",
            validate=_validate_fee,
            policy=pol(assess_fee),
            steps=(
                StepSpec(
                    "charge", b(_build_charge("fee", assess_fee)), CHARGE_FINALITY, charge_reconcile
                ),
            ),
            approval_ttl=timedelta(hours=24),
            read_allowlist=(ALLOW_BALANCES,),
            platform_read_allowlist=PLATFORM_CHARGES,
            postcondition=b(_charge_postcondition("fee")),
        ),
        KitWorkflowSpec(
            approver=ApproverScope.OWN_TENANT,  # the requester's own tenant approves
            kit=KIT,
            action="collect_repayment",
            validate=_validate_none,
            policy=pol(assess_repayment),
            steps=(
                StepSpec(
                    "charge",
                    b(_build_charge("repayment", assess_repayment)),
                    CHARGE_FINALITY,
                    charge_reconcile,
                ),
            ),
            approval_ttl=timedelta(hours=24),
            read_allowlist=(ALLOW_BALANCES,),
            platform_read_allowlist=PLATFORM_CHARGES,
            postcondition=b(_charge_postcondition("repayment")),
        ),
        KitWorkflowSpec(
            approver=ApproverScope.OWN_TENANT,  # the requester's own tenant approves
            kit=KIT,
            action="issue_card",
            validate=_validate_card,
            policy=pol(assess_card),
            steps=(
                StepSpec(
                    "create_cardholder",
                    b(_build_cardholder),
                    CARDHOLDER_FINALITY,
                    cardholder_reconcile,
                ),
                StepSpec("create_card", b(_build_card), CARD_FINALITY, card_reconcile),
            ),
            approval_ttl=timedelta(hours=24),
            read_allowlist=ALLOW_CARDS,
            postcondition=b(_card_postcondition),
        ),
        KitWorkflowSpec(
            approver=ApproverScope.OWN_TENANT,  # the requester's own tenant approves
            kit=KIT,
            action="simulate_card_spend",
            validate=_validate_sim,
            policy=pol(assess_sim),
            steps=(StepSpec("simulate", b(_build_sim), None, simulation_reconcile),),
            approval_ttl=timedelta(hours=1),
            read_allowlist=(*ALLOW_CARDS, *ALLOW_TXNS),
            postcondition=b(_sim_postcondition),
        ),
    ]
    return {s.action: s for s in specs}


SPECS: dict[str, KitWorkflowSpec] = build_specs(current_setup)

__all__ = [
    "APPROVAL_MATRIX",
    "SPECS",
    "InvalidIntent",
    "book_for",
    "build_specs",
    "configure_platform",
    "current_setup",
]
