"""Kit 2 workflow specs. The spine runs these: validate -> policy -> approval -> steps -> post.

Approval matrix (README.md and DESIGN.md give the reasoning):

| action                | tier                                                             |
|-----------------------|------------------------------------------------------------------|
| issue_card            | SINGLE; DUAL above the ALL_TIME threshold; BLOCKED over the cap  |
| update_card_controls  | AUTO for a pure reduction (also model-started), else SINGLE/DUAL |
| freeze_card           | AUTO, also when the model starts it (revokes authority)          |
| unfreeze_card         | SINGLE (restores persistent authority)                           |
| close_card            | SINGLE (irreversible)                                            |
| run_limit_test        | SINGLE (debits the wallet); BLOCKED unless a funded test card    |

Policy tiers are raised to SINGLE when the model starts a proposal (the agent approval floor,
agent_min_tier), except freeze_card and update_card_controls, which set agent_min_tier=AUTO
because they run at AUTO only when they reduce what the card can spend (DESIGN.md).

Every step builder re-derives what the policy decided and refuses to run if it no longer
matches, so a changed balance or card between approval and execution fails closed.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from awx_common import Money, OperationResult, OperationStatus, ReadRequest, RiskTier
from awx_common.ops import DEDUP_RESEND_MARGIN
from awx_common.store import VersionConflict
from awx_common.workflow import (
    FinalitySpec,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    StepContext,
    StepSpec,
)

from . import ops
from .controls import CardControls, Interval, SpendSoFar
from .harness import (
    HarnessNotPossible,
    Observation,
    Probe,
    ProbeKind,
    judge,
    plan_probes,
    required_funding,
)
from .policy_config import POLICY, PurchasePolicy
from .provider import (
    BALANCES,
    CARD_TERMINAL,
    CARDHOLDER_SUCCESS,
    CARDHOLDER_TERMINAL,
    available_balance,
    card_controls,
    decline_reason,
    items,
    limits_match,
    limits_spent,
    object_id,
    read_card,
    read_card_limits,
    read_card_transactions,
    rest_txn_state,
    row_amount,
    row_card_transaction_id,
    row_merchant,
    sim_reply_state,
)
from .reconcile import (
    reconcile_card,
    reconcile_card_update,
    reconcile_cardholder,
    reconcile_simulation,
)
from .terms import (
    choose_plan,
    controls_for,
    parse_flows,
    parse_terms,
    reconsideration,
    terms_from_intent,
    terms_to_intent,
)

KIT = "k2"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ID_RE = r"[A-Za-z0-9_-]+"

READ_ALLOWLIST: tuple[str, ...] = (
    r"^/api/v1/balances/current$",
    r"^/api/v1/issuing/cardholders$",
    r"^/api/v1/issuing/cards$",
    rf"^/api/v1/issuing/cardholders/{_ID_RE}$",
    rf"^/api/v1/issuing/cards/{_ID_RE}$",
    rf"^/api/v1/issuing/cards/{_ID_RE}/limits$",
    r"^/api/v1/issuing/transactions$",
)

PERSISTENT_AUTHORITY = "creates or raises persistent spending authority"


class StaleApproval(RuntimeError):
    """What the approver saw no longer matches the world; the run stops before sending."""


# ---- shared evaluation ----------------------------------------------------------------------


def _card_id(raw: Mapping[str, Any]) -> str:
    card_id = raw.get("card_id")
    if not isinstance(card_id, str) or not _ID.match(card_id):
        raise ValueError("card_id must be 1-64 characters of A-Z, a-z, 0-9, '_' and '-'")
    return card_id


def _reason_text(raw: Mapping[str, Any]) -> str:
    reason = raw.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 200:
        raise ValueError("reason must be a string of up to 200 characters")
    return reason.strip()


def validate_terms_input(raw: Mapping[str, Any]) -> dict[str, Any]:
    terms = parse_terms(raw)
    return terms_to_intent(terms, parse_flows(raw.get("forecast_flows"), terms.currency))


def evaluate_purchase(
    intent: Mapping[str, Any],
    ctx: PolicyContext,
    *,
    policy: PurchasePolicy = POLICY,
    already_spent_all_time: Money | None = None,
) -> dict[str, Any]:
    """Cash curve, plan, reconsideration and control set for one set of terms. Pure on reads."""
    terms, flows = terms_from_intent(intent)
    opening = available_balance(ctx.read(BALANCES), terms.currency)
    choice = choose_plan(terms, opening, flows, policy)
    out: dict[str, Any] = {
        "policy_version": policy.version,
        "opening_balance": opening.to_dict(),
        "reserve_floor": policy.floor(terms.currency).to_dict(),
        "annual_saving": choice.annual_saving.to_dict(),
        "plan": choice.chosen.value if choice.chosen else None,
        "plan_reason": choice.reason,
        "outcomes": {p.value: o.to_dict() for p, o in choice.outcomes.items()},
    }
    if choice.chosen is not None:
        review = reconsideration(terms, choice.chosen, policy)
        controls = controls_for(
            terms, choice.chosen, review, policy, already_spent_all_time=already_spent_all_time
        )
        out["reconsideration"] = review.to_dict()
        out["controls"] = controls.to_dict()
    return out


def _authority_tier(controls: CardControls, policy: PurchasePolicy) -> tuple[RiskTier, str]:
    total = controls.limit(Interval.ALL_TIME)
    assert total is not None  # controls_for always sets ALL_TIME
    if total > policy.max_all_time[controls.currency]:
        return RiskTier.BLOCKED, f"ALL_TIME {total} is over the policy cap"
    if total > policy.dual_approval_above[controls.currency]:
        return RiskTier.DUAL, f"ALL_TIME {total} is over the dual-approval threshold"
    return RiskTier.SINGLE, f"ALL_TIME {total} within single-approval threshold"


# ---- in-flight writes (kit rule M1) ------------------------------------------------------------
#
# A write whose outcome stayed unknown (OUTCOME_UNKNOWN, including the executor's refused-resend
# and multiple_objects shapes) is recorded in the kit store under its request_id, owned by the run's
# proposal_id (M4). It stays in flight until a later run reads a definitive outcome for that
# request_id. Card create deduplicates on request_id for about 7 days, so a re-proposal inside
# that window re-emits the same Operation (rule 9); past the margin a person decides. Card
# authorisation simulations ignore request_id, so an unknown probe always goes to a person.

INFLIGHT_PREFIX = "inflight:"
# The spine stops resending past this deduplication margin; kit 2 hands such money to a person.
DEDUPE_MARGIN = DEDUP_RESEND_MARGIN


SENT_AT_PREFIX = "sent-at:"


def mark_sending(ctx: StepContext, ops_out: list) -> list:
    """Record, in the build just before the send, when each operation is sent, so an in-flight
    record's `since` is the send time, not a later postcondition's.
    Idempotent: a redelivered build keeps the first time."""
    store = ctx.policy.store
    if store is not None:
        for op in ops_out:
            key = f"{SENT_AT_PREFIX}{op.request_id}"
            if store.get(key) is None:
                try:
                    store.put(key, {"at": ctx.policy.now}, expected_version=None)
                except VersionConflict:
                    pass
    return ops_out


def record_in_flight(
    ctx: StepContext, what: str, result: OperationResult, *, unconfirmed: bool = False
) -> None:
    """unconfirmed: the reply said it landed but its object was never listed, so the money
    stays in flight too (M1)."""
    store = ctx.policy.store
    if store is None:
        return
    if result.status is not OperationStatus.OUTCOME_UNKNOWN and not unconfirmed:
        return
    key = f"{INFLIGHT_PREFIX}{what}"
    if store.get(key) is not None:
        return  # idempotent; the first run that saw it unknown stays the owner
    sent = store.get(f"{SENT_AT_PREFIX}{result.request_id}")
    value = {
        "request_id": result.request_id,
        "since": sent.value["at"] if sent is not None else ctx.policy.now,
        "owner": ctx.proposal_id,
        "resolved": False,
        "error_code": result.error_code or "",
    }
    try:
        store.put(key, value, expected_version=None)
    except VersionConflict:
        pass


def resolve_in_flight(ctx: StepContext, what: str, result: OperationResult) -> None:
    """A later run read a definitive outcome (landed) for the same request_id."""
    store = ctx.policy.store
    if store is None:
        return
    found = store.get(f"{INFLIGHT_PREFIX}{what}")
    if found is None or found.value.get("resolved"):
        return
    if found.value["request_id"] != result.request_id:
        return
    if result.status is not OperationStatus.SUCCEEDED:
        return
    try:
        store.put(
            found.key,
            {**found.value, "resolved": True, "resolved_by": ctx.proposal_id},
            expected_version=found.version,
        )
    except VersionConflict:
        pass


def in_flight(ctx: PolicyContext, what: str) -> Mapping[str, Any] | None:
    if ctx.store is None:
        return None
    found = ctx.store.get(f"{INFLIGHT_PREFIX}{what}")
    if found is None or found.value.get("resolved"):
        return None
    return found.value


# ---- issue_card -----------------------------------------------------------------------------


def _existing_card_for_purchase(
    ctx: PolicyContext, purchase_ref: str, book: str | None
) -> tuple[str, Any] | None:
    """The card this book already issued for the purchase, with its controls (canonical dict),
    or None. Read-only (rule 0). Unreadable controls count as different terms."""
    if book is None:
        return None
    email = ops.cardholder_email(book, purchase_ref)
    holders = items(ctx.read(ReadRequest("/api/v1/issuing/cardholders", {"email": email})))
    for holder in holders:
        if holder.get("email") != email:
            continue
        cards = items(
            ctx.read(
                ReadRequest("/api/v1/issuing/cards", {"cardholder_id": holder["cardholder_id"]})
            )
        )
        for card in cards:
            try:
                controls: Any = card_controls(card).to_dict()
            except (KeyError, TypeError, ValueError):
                controls = None
            return str(card.get("card_id")), controls
    return None


def issue_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    ev = {**evaluate_purchase(intent, ctx), "book_id": current_book(ctx)}
    reasons = [ev["plan_reason"], f"policy {ev['policy_version']}"]
    if ev["plan"] is None:
        return PolicyDecision(RiskTier.BLOCKED, ("no plan keeps the reserve floor", *reasons), ev)
    pending = in_flight(ctx, f"card:{intent['purchase_ref']}")
    if pending is not None and ctx.now - pending["since"] > DEDUPE_MARGIN:
        return PolicyDecision(
            RiskTier.BLOCKED,
            (
                f"the card create for this purchase has been in flight on request_id "
                f"{pending['request_id']} since {pending['since'].isoformat()}, past the "
                "deduplication margin; a person decides (M1)",
            ),
            ev,
        )
    existing = _existing_card_for_purchase(ctx, intent["purchase_ref"], ev["book_id"])
    if existing is not None:
        card_id, controls = existing
        if controls != ev["controls"]:
            return PolicyDecision(
                RiskTier.BLOCKED,
                (
                    f"purchase {intent['purchase_ref']} already has card {card_id}; change it "
                    "with update_card_controls",
                ),
                ev,
            )
    tier, why = _authority_tier(CardControls.from_dict(ev["controls"]), POLICY)
    return PolicyDecision(
        tier,
        (f"{PERSISTENT_AUTHORITY}: a cardholder and a virtual card", why, *reasons),
        ev,
    )


def _recheck_issue(ctx: StepContext) -> CardControls:
    """Re-run the evaluation; the plan and control set must be exactly what was approved."""
    fresh = {**evaluate_purchase(ctx.intent, ctx.policy), "book_id": current_book(ctx.policy)}
    approved = ctx.decision.computed
    if fresh.get("plan") != approved.get("plan") or fresh.get("controls") != approved.get(
        "controls"
    ):
        raise StaleApproval(
            "the cash position changed since approval and the plan or controls differ; "
            "the purchase was not issued"
        )
    return CardControls.from_dict(approved["controls"])


def _existing_cardholder(ctx: StepContext) -> str | None:
    """The purchase's cardholder if an earlier run already made it. Cardholder create ignores
    request_id and refuses a reused email with 400 (FAKE-FIDELITY.md, request_id idempotency),
    so a re-run must find it rather than send the create again."""
    email = ops.cardholder_email(ensure_book(ctx), ctx.intent["purchase_ref"])
    rows = items(ctx.policy.read(ReadRequest("/api/v1/issuing/cardholders", {"email": email})))
    ids = [r.get("cardholder_id") for r in rows if r.get("email") == email]
    if len(ids) > 1:
        raise StaleApproval(f"{len(ids)} cardholders share {email}; a human decides")
    return ids[0] if ids and isinstance(ids[0], str) else None


def build_cardholder(ctx: StepContext) -> list:
    _recheck_issue(ctx)
    if _existing_cardholder(ctx) is not None:
        return []
    return [ops.cardholder_create(ensure_book(ctx), ctx.intent["purchase_ref"], ctx.intent_hash)]


def build_card(ctx: StepContext) -> list:
    controls = _recheck_issue(ctx)
    ref = ctx.intent["purchase_ref"]
    made = ctx.prior.get("cardholder") or ()
    holder_id = object_id(made[0], "cardholder_id") if made else _existing_cardholder(ctx)
    if holder_id is None:
        raise StaleApproval("the purchase's cardholder does not exist")
    return mark_sending(
        ctx, [ops.card_create(ensure_book(ctx), ref, holder_id, controls, ctx.intent_hash)]
    )


def _verify_card(ctx: StepContext, step: str, expected: CardControls) -> PostconditionResult:
    results = ctx.prior.get(step) or ()
    if not results:
        return PostconditionResult(False, f"step {step} did not complete; no card to verify")
    if results[0].status is not OperationStatus.SUCCEEDED or not (
        results[0].provider_id or (results[0].body or {}).get("card_id")
    ):
        return PostconditionResult(
            False,
            f"step {step} ended {results[0].status.value}; its write stays in flight on "
            f"request_id {results[0].request_id} (M1)",
        )
    card_id = object_id(results[0], "card_id")
    card = ctx.policy.read(read_card(card_id))
    if not ops.is_ours(card):
        return PostconditionResult(False, f"card {card_id} does not carry the poc-k2- label")
    try:
        got = card_controls(card)
    except (ValueError, KeyError) as exc:
        return PostconditionResult(False, f"card {card_id} controls unreadable: {exc}")
    if got != expected:
        return PostconditionResult(
            False, f"card {card_id} controls {got.to_dict()} != approved {expected.to_dict()}"
        )
    problems = limits_match(ctx.policy.read(read_card_limits(card_id)), expected)
    if problems:
        return PostconditionResult(False, f"card {card_id} limits: " + "; ".join(problems))
    return PostconditionResult(True, f"card {card_id} carries exactly the approved control set")


def _one_card_per_purchase(ctx: StepContext, card_id: str) -> str | None:
    """The cardholder is book-scoped, so it holds exactly one card.
    Any other card under it is duplicate authority; name it."""
    card = ctx.policy.read(read_card(card_id))
    holder = card.get("cardholder_id")
    if not isinstance(holder, str):
        return f"card {card_id} has no cardholder_id to check"
    rows = items(ctx.policy.read(ReadRequest("/api/v1/issuing/cards", {"cardholder_id": holder})))
    extra = sorted(str(r.get("card_id")) for r in rows if r.get("card_id") != card_id)
    if extra:
        return f"purchase cardholder {holder} also holds card(s) {', '.join(extra)}"
    return None


def issue_postcondition(ctx: StepContext) -> PostconditionResult:
    what = f"card:{ctx.intent['purchase_ref']}"
    for result in ctx.prior.get("card") or ():
        record_in_flight(ctx, what, result)
        resolve_in_flight(ctx, what, result)
    verified = _verify_card(ctx, "card", CardControls.from_dict(ctx.decision.computed["controls"]))
    if not verified.ok:
        return verified
    extra = _one_card_per_purchase(ctx, object_id((ctx.prior.get("card") or ())[0], "card_id"))
    if extra:
        return PostconditionResult(False, f"{verified.detail}; but {extra}")
    return verified


def _row_status(key: str) -> Any:
    """Status of the row `FinalitySpec.select` picked. A list read never 404s during lag, where
    GET by id does (FAKE-FIDELITY, Read-back). Without a select (no provider id to pick by) a
    list of several rows is AMBIGUOUS, so the poll stops rather than guess."""

    def status_of(body: Mapping[str, Any]) -> str:
        if isinstance(body, Mapping) and "items" not in body:  # select picked one row
            return str(body.get(key))
        rows = list(items(body))
        if len(rows) > 1:
            return "AMBIGUOUS"
        return str(rows[0].get(key)) if rows else "LISTING"

    return status_of


def _select_by(id_key: str) -> Any:
    """FinalitySpec.select: this operation's own row, by the create reply's id. None while
    it is not listed yet (a non-terminal poll)."""

    def select(body: Mapping[str, Any], result: OperationResult) -> Mapping[str, Any] | None:
        want = result.provider_id or (result.body or {}).get(id_key)
        if not want:
            return body  # nothing to pick by: status_of sees the list (AMBIGUOUS if several)
        for row in items(body):
            if row.get(id_key) == want:
                return row
        return None

    return select


CARDHOLDER_FINALITY = FinalitySpec(
    read_request=lambda r: ReadRequest(
        "/api/v1/issuing/cardholders", {"email": str((r.body or {}).get("email"))}
    ),
    status_of=_row_status("status"),
    terminal=CARDHOLDER_TERMINAL | {"AMBIGUOUS"},
    success=CARDHOLDER_SUCCESS,
    interval_s=2,
    timeout_s=60,
    select=_select_by("cardholder_id"),
)

# Card create: the cardholder's card list, this card's row picked by the reply's card_id.
CARD_CREATE_FINALITY = FinalitySpec(
    read_request=lambda r: ReadRequest(
        "/api/v1/issuing/cards", {"cardholder_id": str((r.body or {}).get("cardholder_id"))}
    ),
    status_of=_row_status("card_status"),
    terminal=CARD_TERMINAL | {"AMBIGUOUS"},
    success=frozenset({"ACTIVE"}),
    interval_s=2,
    timeout_s=60,
    select=_select_by("card_id"),
)


def _card_finality(success: frozenset[str]) -> FinalitySpec:
    return FinalitySpec(
        read_request=lambda r: read_card(object_id(r, "card_id")),
        status_of=lambda b: str(b.get("card_status")),
        terminal=CARD_TERMINAL,
        success=success,
        interval_s=2,
        timeout_s=60,
    )


# ---- card reads shared by the card actions ---------------------------------------------------


def _our_card(ctx: PolicyContext, card_id: str) -> tuple[Mapping[str, Any] | None, str | None]:
    card = ctx.read(read_card(card_id))
    if not ops.is_ours(card):
        return None, f"card {card_id} was not created by kit 2 (no poc-k2- label)"
    return card, None


# ---- book id (rule M2) ------------------------------------------------------------------------
#
# Every request_id kit 2 builds starts with the book id: a random id minted once, when kit 2's
# state for this tenant is first created, and stored with that state. Policy only reads it
# (rule 0); the first build mints it with a conditional create, so concurrent first builds agree
# on one book. A fresh store seeded like an old one gets a new book, so its request_ids are
# disjoint from every earlier run on the shared sandbox (~7-day dedup window).

BOOK_KEY = "book"


def current_book(ctx: PolicyContext) -> str | None:
    """The book id if kit 2's state already exists; policy puts it in computed (rule 0)."""
    if ctx.store is None:
        return None
    found = ctx.store.get(BOOK_KEY)
    return found.value["book_id"] if found is not None else None


def ensure_book(ctx: StepContext) -> str:
    """The approved book, minting it on first use. computed["book_id"] binds the approval and
    the proposal ID to the book, so a fresh book never reaches an old book's execution."""
    store = ctx.policy.store
    if store is None:
        raise StaleApproval("no kit store; kit 2 cannot mint its book id")
    found = store.get(BOOK_KEY)
    if found is None:
        try:
            found = store.put(
                BOOK_KEY, {"book_id": f"bk-{uuid.uuid4().hex}"}, expected_version=None
            )
        except VersionConflict:
            found = store.get(BOOK_KEY)
    if found is None:
        raise StaleApproval("the book id could not be read back after a conflicting create")
    book = found.value["book_id"]
    approved = ctx.decision.computed.get("book_id")
    if approved is not None and approved != book:
        raise StaleApproval("kit 2's book changed since approval; nothing was sent")
    return book


# ---- card change numbers ----------------------------------------------------------------------
#
# A status or control change is keyed on the card plus its change number n, the business object
# "the n-th change kit 2 makes to this card". Policy (pure, rule 0) reads the claims in the kit
# store and puts the next number in `computed`, so the approval binds it and a concurrent change
# moves the hash (rule 1: STALE). The build claims n once, recording the run's proposal_id; a retry
# of the same build finds its own claim and reuses n, so the request_id and the operation-store
# record are reused and nothing is sent twice. A claim held by another proposal fails closed.

CHANGE_PREFIX = "card-change:"


def _claims_prefix(card_id: str) -> str:
    return f"{CHANGE_PREFIX}{card_id}:"


def next_change_seq(ctx: PolicyContext, card_id: str) -> int | None:
    """The next change number for a card; None when there is no kit store."""
    if ctx.store is None:
        return None
    taken = [int(item.key.rsplit(":", 1)[1]) for item in ctx.store.query(_claims_prefix(card_id))]
    return max(taken, default=0) + 1


def claim_change(ctx: StepContext, card_id: str) -> int:
    """Claim the approved change number for this run, idempotently (rule 0, M4).

    The claim's owner is ctx.proposal_id. A redelivered build of the same execution finds its
    own claim and reuses n; another execution, even with an identical intent and computed,
    finds the claim owned by someone else and fails closed. Claims are never released:
    on_abandon only writes a notice and touches no claim."""
    store = ctx.policy.store
    n = ctx.decision.computed.get("change_seq")
    if store is None or not isinstance(n, int):
        raise StaleApproval("no kit store or change number; card changes cannot be keyed")
    key = f"{_claims_prefix(card_id)}{n:06d}"
    found = store.get(key)
    if found is None:
        try:
            store.put(
                key,
                {
                    "owner": ctx.proposal_id,
                    "intent_hash": ctx.intent_hash,
                    "target": ctx.decision.computed.get("target_status") or "CONTROLS",
                },
                expected_version=None,
            )
        except VersionConflict:
            found = store.get(key)
            if found is None:  # never fall through without a claim
                raise StaleApproval(
                    _not_applied(ctx, card_id, f"change {n} could not be read back")
                ) from None
    # M4: the owner is this run's proposal_id; an identical proposal in another window shares
    # intent_hash but is a different run and must never share n.
    if found is not None and found.value.get("owner") != ctx.proposal_id:
        why = f"change {n} was taken by another change to the card"
        if ctx.decision.computed.get("target_status") == "INACTIVE":
            write_unapplied_freeze(ctx, "STALE", why)
        raise StaleApproval(_not_applied(ctx, card_id, why))
    return n


def _not_applied(ctx: StepContext, card_id: str, why: str) -> str:
    """Facts only (no instruction to the model): which change did not happen and why."""
    what = "freeze" if ctx.decision.computed.get("target_status") == "INACTIVE" else "change"
    return f"the {what} of card {card_id} in proposal {ctx.proposal_id} was not applied: {why}"


# ---- update_card_controls ---------------------------------------------------------------------


def validate_update(raw: Mapping[str, Any]) -> dict[str, Any]:
    card_id = _card_id(raw)
    terms_raw = {k: v for k, v in raw.items() if k != "card_id"}
    return {"card_id": card_id, **validate_terms_input(terms_raw)}


def _update_eval(intent: Mapping[str, Any], ctx: PolicyContext) -> tuple[RiskTier, list[str], dict]:
    card_id = intent["card_id"]
    card, why = _our_card(ctx, card_id)
    if card is None:
        return RiskTier.BLOCKED, [why or "not ours"], {}
    if card.get("card_status") not in ("ACTIVE", "INACTIVE"):
        return RiskTier.BLOCKED, [f"card is {card.get('card_status')}"], {}
    try:
        current = card_controls(card)
    except (ValueError, KeyError) as exc:
        return RiskTier.BLOCKED, [f"current controls unreadable: {exc}"], {}
    if current.currency != intent["currency"]:
        return RiskTier.BLOCKED, ["a card's currency cannot change; issue a new card"], {}
    spent = limits_spent(ctx.read(read_card_limits(card_id)), current.currency)
    if spent is None or current.limit(Interval.ALL_TIME) is None:
        return RiskTier.BLOCKED, ["lifetime spend on the card is unknown"], {}
    terms_intent = {k: v for k, v in intent.items() if k != "card_id"}
    ev = evaluate_purchase(
        terms_intent, ctx, already_spent_all_time=spent.of(Interval.ALL_TIME, current.currency)
    )
    ev["current_controls"] = current.to_dict()
    seq = next_change_seq(ctx, card_id)
    if seq is None:
        return RiskTier.BLOCKED, ["no kit store to number the card change"], ev
    ev["change_seq"] = seq
    ev["book_id"] = current_book(ctx)
    reasons = [ev["plan_reason"], f"policy {ev['policy_version']}"]
    if ev["plan"] is None:
        return RiskTier.BLOCKED, ["no plan keeps the reserve floor", *reasons], ev
    new = CardControls.from_dict(ev["controls"])
    if new.reduces_or_equals(current):
        return RiskTier.AUTO, ["only reduces spending authority", *reasons], ev
    tier, why = _authority_tier(new, POLICY)
    return tier, [f"{PERSISTENT_AUTHORITY}: raises card limits", why, *reasons], ev


def update_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    tier, reasons, ev = _update_eval(intent, ctx)
    return PolicyDecision(tier, tuple(reasons), ev)


def build_update(ctx: StepContext) -> list:
    _, _, ev = _update_eval(ctx.intent, ctx.policy)
    approved = ctx.decision.computed
    if ev.get("controls") != approved.get("controls"):
        raise StaleApproval("the card or cash position changed since approval; nothing was sent")
    new = CardControls.from_dict(approved["controls"])
    n = claim_change(ctx, ctx.intent["card_id"])
    book = ensure_book(ctx)
    return [ops.card_update_controls(book, ctx.intent["card_id"], new, n, ctx.intent_hash)]


def update_postcondition(ctx: StepContext) -> PostconditionResult:
    return _verify_card(ctx, "controls", CardControls.from_dict(ctx.decision.computed["controls"]))


# ---- freeze / unfreeze / close --------------------------------------------------------------


def validate_card_action(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"card_id": _card_id(raw), "reason": _reason_text(raw)}


def _status_policy(*, from_status: frozenset[str], tier: RiskTier, why: str, target: str) -> Any:
    def policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        card, bad = _our_card(ctx, intent["card_id"])
        if card is None:
            return PolicyDecision(RiskTier.BLOCKED, (bad or "not ours",))
        status = card.get("card_status")
        if status not in from_status:
            return PolicyDecision(
                RiskTier.BLOCKED, (f"card is {status}, needs {sorted(from_status)}",)
            )
        if tier is not RiskTier.AUTO:
            try:
                card_controls(card)
            except (ValueError, KeyError) as exc:
                return PolicyDecision(RiskTier.BLOCKED, (f"controls unsafe to restore: {exc}",))
        seq = next_change_seq(ctx, intent["card_id"])
        if seq is None:
            return PolicyDecision(RiskTier.BLOCKED, ("no kit store to number the card change",))
        return PolicyDecision(
            tier,
            (why,),
            {
                "from_status": status,
                "target_status": target,
                "change_seq": seq,
                "book_id": current_book(ctx),
            },
        )

    return policy


def _status_steps(target: str) -> tuple[StepSpec, ...]:
    return (
        StepSpec(
            "status",
            lambda ctx: [
                ops.card_update_status(
                    ensure_book(ctx),
                    ctx.intent["card_id"],
                    target,
                    claim_change(ctx, ctx.intent["card_id"]),
                    ctx.intent_hash,
                )
            ],
            _card_finality(frozenset({target})),
            reconcile_card_update,
        ),
    )


def _status_post(target: str) -> Any:
    def post(ctx: StepContext) -> PostconditionResult:
        card = ctx.policy.read(read_card(ctx.intent["card_id"]))
        status = card.get("card_status")
        return PostconditionResult(status == target, f"card_status is {status}, want {target}")

    return post


UNAPPLIED_FREEZE_PREFIX = "unapplied-freeze:"


def write_unapplied_freeze(ctx: StepContext, reason: str, detail: str) -> None:
    """Record, as facts only, that a freeze was not applied. The notice is tied to the
    freeze's own approved change number (or, with none, the number it would have taken); any
    change number at or above it claimed by another proposal is a later decision on the card
    and hides the notice (live_unapplied_freezes). Never touches claims (M4)."""
    store = ctx.policy.store
    if store is None:
        return
    card_id = ctx.intent["card_id"]
    key = f"{UNAPPLIED_FREEZE_PREFIX}{card_id}:{ctx.proposal_id}"
    if store.get(key) is not None:
        return  # idempotent: on_abandon may run again after a crash
    seq = ctx.decision.computed.get("change_seq")
    if not isinstance(seq, int):
        claims = store.query(_claims_prefix(card_id))
        seq = max((int(c.key.rsplit(":", 1)[1]) for c in claims), default=0) + 1
    when = ctx.policy.now.isoformat()
    # System facts only. The model-supplied reason is stored for the audit but never put in
    # the message, because the model reads the message back.
    message = f"a freeze recorded at {when} (change {seq}) was not applied: {reason}, {detail}"
    value = {
        "reason": reason,
        "message": message,
        "as_of_change": seq,
        "owner": ctx.proposal_id,
        "requested_reason": ctx.intent.get("reason") or "",
    }
    try:
        store.put(key, value, expected_version=None)
    except VersionConflict:
        pass


def freeze_abandoned(ctx: StepContext, reason: str) -> None:
    """on_abandon for freeze_card (rule 8): STALE, BLOCKED, REJECTED or EXPIRED."""
    detail = "; ".join(ctx.decision.reasons) or "no step ran"
    write_unapplied_freeze(ctx, reason, detail)


def live_unapplied_freezes(store: Any, card_id: str, card_status: Any) -> list[dict[str, Any]]:
    """Notices with no later decision on the card: a notice is hidden as soon as another
    proposal has claimed a change number at or above the freeze's own, whether or not that
    run's postcondition ran (an unfreeze in flight counts). Facts only, with the card status."""
    if store is None:
        return []
    claims = store.query(_claims_prefix(card_id))
    out = []
    for n in store.query(f"{UNAPPLIED_FREEZE_PREFIX}{card_id}:"):
        as_of = int(n.value["as_of_change"])
        later = [
            c
            for c in claims
            if int(c.key.rsplit(":", 1)[1]) >= as_of and c.value.get("owner") != n.value["owner"]
        ]
        if not later:
            out.append({"notice": n.value["message"], "card_status_now": card_status})
    return out


# ---- run_limit_test --------------------------------------------------------------------------


def validate_limit_test(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"card_id": _card_id(raw)}


def limit_test_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    """SINGLE: the at-limit probe debits the wallet and uses up the card's authority, and a
    reversal needs an admin key (FAKE-FIDELITY.md, Issuing). Runs only on a dedicated limit-test
    card (purchase_ref `limit-test-...`, issued and approved through issue_card), never on a
    card that pays a real purchase."""
    card_id = intent["card_id"]
    card, bad = _our_card(ctx, card_id)
    if card is None:
        return PolicyDecision(RiskTier.BLOCKED, (bad or "not ours",))
    if not ops.is_limit_test_card(card):
        return PolicyDecision(
            RiskTier.BLOCKED,
            (
                "the limit test runs only on a dedicated card: issue one with a purchase_ref "
                f"starting {ops.LIMIT_TEST_REF_PREFIX!r}, never on a purchase card",
            ),
        )
    if card.get("card_status") != "ACTIVE":
        return PolicyDecision(RiskTier.BLOCKED, (f"card is {card.get('card_status')}",))
    try:
        controls = card_controls(card)
    except (ValueError, KeyError) as exc:
        return PolicyDecision(RiskTier.BLOCKED, (f"controls unreadable: {exc}",))
    spent = limits_spent(ctx.read(read_card_limits(card_id)), controls.currency)
    if spent is None:
        if items(ctx.read(read_card_transactions(card_id))):
            return PolicyDecision(
                RiskTier.BLOCKED, ("card has transactions and its remaining limits are unknown",)
            )
        spent = SpendSoFar({})
    for kind in ProbeKind:
        pending = in_flight(ctx, f"probe:{card_id}:{kind.value}")
        if pending is not None:
            return PolicyDecision(
                RiskTier.BLOCKED,
                (
                    f"probe {kind.value} on this card has been in flight on request_id "
                    f"{pending['request_id']} since {pending['since'].isoformat()}; "
                    "simulations ignore request_id, so a person decides (M1)",
                ),
            )
    if any(not s.is_zero() for s in spent.by_interval.values()):
        return PolicyDecision(RiskTier.BLOCKED, ("the test needs a fresh card with no spend",))
    try:
        probes = plan_probes(controls, spent)
    except HarnessNotPossible as exc:
        return PolicyDecision(RiskTier.BLOCKED, (f"limit test not isolatable: {exc}",))
    debit = required_funding(probes, controls.currency)
    have = available_balance(ctx.read(BALANCES), controls.currency)
    computed = {
        "controls": controls.to_dict(),
        "probes": [p.to_dict() for p in probes],
        "debit": debit.to_dict(),
        "available": have.to_dict(),
        "book_id": current_book(ctx),
    }
    if have < debit:
        return PolicyDecision(
            RiskTier.BLOCKED,
            (
                f"pre-fund the {controls.currency} wallet to at least {debit} (has {have}): "
                "an INSUFFICIENT_FUNDS decline would prove nothing about the controls",
            ),
            computed,
        )
    return PolicyDecision(
        RiskTier.SINGLE,
        (
            f"debits {debit} from the {controls.currency} wallet (one at-limit charge, not "
            "reversible with this key) and uses up the test card's authority",
            "four probes designed to decline, one to clear",
        ),
        computed,
    )


def _probe(ctx: StepContext, kind: ProbeKind) -> Probe:
    for d in ctx.decision.computed["probes"]:
        if d["kind"] == kind.value:
            return Probe.from_dict(d)
    raise KeyError(kind.value)


def _probe_listed(kind: ProbeKind) -> FinalitySpec:
    """The probe is decided by its reply, which is final on 201 (FAKE-FIDELITY.md, Issuing). This
    poll only waits until the authorisation shows in GET /issuing/transactions?card_id= (list
    lag, 0.65 to 1.9 s observed), so the postcondition can confirm every reply against its row.

    select: with a provider_id (the simulation's card_transaction_id) only the row
    joined on card_transaction_data.card_transaction_id, on the operation's own card, counts.
    Without one, the operation's card's row carrying this probe's merchant name."""

    def select(body: Mapping[str, Any], result: OperationResult) -> Mapping[str, Any] | None:
        card_id = (result.body or {}).get("card_id")
        rows = [r for r in items(body) if card_id is None or r.get("card_id") == card_id]
        if result.provider_id:
            mine = [r for r in rows if row_card_transaction_id(r) == result.provider_id]
            return mine[0] if len(mine) == 1 else None
        if not isinstance(card_id, str):
            return None
        name = ops.probe_merchant(card_id, kind.value)
        mine = [r for r in rows if row_merchant(r)[1] == name]
        return mine[0] if len(mine) == 1 else None

    return FinalitySpec(
        read_request=lambda r: read_card_transactions(str((r.body or {}).get("card_id"))),
        status_of=lambda row: "LISTED",
        terminal=frozenset({"LISTED"}),
        success=frozenset({"LISTED"}),
        interval_s=1,
        timeout_s=30,
        select=select,
    )


def _probe_step(kind: ProbeKind) -> StepSpec:
    def build(ctx: StepContext) -> list:
        p = _probe(ctx, kind)
        return mark_sending(
            ctx,
            [
                ops.simulate_authorization(
                    ensure_book(ctx),
                    ctx.intent["card_id"],
                    p.amount,
                    p.merchant_category,
                    kind.value,
                    ctx.intent_hash,
                )
            ],
        )

    return StepSpec(kind.value, build, _probe_listed(kind), reconcile_simulation)


def _reply(result: OperationResult) -> Mapping[str, Any] | None:
    body = result.body
    if not isinstance(body, Mapping) or "process_result" not in body:
        return None  # rejected before a transaction existed, or outcome unknown
    return body


def _observe(
    ctx: StepContext, probe: Probe, rows: list[Mapping[str, Any]]
) -> tuple[Observation, str | None]:
    """Decide a probe from its reply; confirm it against the joined transaction list row."""
    results = ctx.prior.get(probe.kind.value) or ()
    reply = _reply(results[0]) if results else None
    if reply is None:
        return Observation(probe.kind, None, None), None
    state = sim_reply_state(reply)
    reason = decline_reason(reply)
    obs = Observation(probe.kind, state, str(reason) if reason else None)
    named = [
        r
        for r in rows
        if row_merchant(r)[1] == ops.probe_merchant(ctx.intent["card_id"], probe.kind.value)
    ]
    if len(named) > 1:  # a duplicate delivery made a second authorisation for this probe
        return obs, f"{probe.kind.value}: {len(named)} authorisations for one probe"
    txn_id = reply.get("card_transaction_id")
    joined = [r for r in rows if row_card_transaction_id(r) == txn_id]
    if len(joined) != 1:
        return obs, f"{probe.kind.value}: not confirmed, {len(joined)} list rows for {txn_id}"
    row = joined[0]
    if rest_txn_state(row) is not state or row_amount(row) != probe.amount:
        return obs, (
            f"{probe.kind.value}: list row says {rest_txn_state(row).value} "
            f"{row_amount(row)}, reply says {state.value} {probe.amount}"
        )
    return obs, None


def limit_test_postcondition(ctx: StepContext) -> PostconditionResult:
    probes = [Probe.from_dict(d) for d in ctx.decision.computed["probes"]]
    unknown = []
    for p in probes:
        for result in ctx.prior.get(p.kind.value) or ():
            if result.status is OperationStatus.OUTCOME_UNKNOWN:
                record_in_flight(ctx, f"probe:{ctx.intent['card_id']}:{p.kind.value}", result)
                unknown.append(f"{p.kind.value} in flight on request_id {result.request_id}")
    rows = list(items(ctx.policy.read(read_card_transactions(ctx.intent["card_id"]))))
    listed = {row_card_transaction_id(r) for r in rows}
    for p in probes:
        for result in ctx.prior.get(p.kind.value) or ():
            reply = _reply(result)
            if reply is not None and reply.get("card_transaction_id") not in listed:
                record_in_flight(
                    ctx, f"probe:{ctx.intent['card_id']}:{p.kind.value}", result, unconfirmed=True
                )
                unknown.append(f"{p.kind.value} not listed, in flight on {result.request_id}")
    obs: dict[ProbeKind, Observation] = {}
    problems: list[str] = []
    for p in probes:
        obs[p.kind], problem = _observe(ctx, p, rows)
        if problem:
            problems.append(problem)
    verdict = judge(probes, obs)
    if unknown:
        return PostconditionResult(False, "; ".join(unknown) + "; " + verdict.detail)
    if verdict.ok and problems:
        return PostconditionResult(False, verdict.detail + "; " + "; ".join(problems))
    return verdict


# ---- specs ----------------------------------------------------------------------------------


def _spec(action: str, **kw: Any) -> KitWorkflowSpec:
    return KitWorkflowSpec(kit=KIT, action=action, read_allowlist=READ_ALLOWLIST, **kw)


SPECS: dict[str, KitWorkflowSpec] = {
    "issue_card": _spec(
        "issue_card",
        validate=validate_terms_input,
        policy=issue_policy,
        steps=(
            StepSpec("cardholder", build_cardholder, CARDHOLDER_FINALITY, reconcile_cardholder),
            StepSpec("card", build_card, CARD_CREATE_FINALITY, reconcile_card),
        ),
        approval_ttl=timedelta(hours=4),
        postcondition=issue_postcondition,
    ),
    "update_card_controls": _spec(
        "update_card_controls",
        validate=validate_update,
        policy=update_policy,
        steps=(
            StepSpec(
                "controls",
                build_update,
                _card_finality(frozenset({"ACTIVE", "INACTIVE"})),
                reconcile_card_update,
            ),
        ),
        approval_ttl=timedelta(hours=4),
        postcondition=update_postcondition,
        # The policy is AUTO only for a pure reduction; any raise is SINGLE or DUAL.
        agent_min_tier=RiskTier.AUTO,
    ),
    "freeze_card": _spec(
        "freeze_card",
        validate=validate_card_action,
        policy=_status_policy(
            from_status=frozenset({"ACTIVE"}),
            tier=RiskTier.AUTO,
            target="INACTIVE",
            why="freezing only revokes authority",
        ),
        steps=_status_steps("INACTIVE"),
        approval_ttl=timedelta(minutes=30),
        postcondition=_status_post("INACTIVE"),
        on_abandon=freeze_abandoned,
        agent_min_tier=RiskTier.AUTO,  # DESIGN.md: why freezes and reductions run at AUTO
    ),
    "unfreeze_card": _spec(
        "unfreeze_card",
        validate=validate_card_action,
        policy=_status_policy(
            from_status=frozenset({"INACTIVE"}),
            tier=RiskTier.SINGLE,
            target="ACTIVE",
            why=f"{PERSISTENT_AUTHORITY}: restores a frozen card",
        ),
        steps=_status_steps("ACTIVE"),
        approval_ttl=timedelta(hours=4),
        postcondition=_status_post("ACTIVE"),
    ),
    "close_card": _spec(
        "close_card",
        validate=validate_card_action,
        policy=_status_policy(
            from_status=frozenset({"ACTIVE", "INACTIVE"}),
            tier=RiskTier.SINGLE,
            target="CLOSED",
            why="closing is irreversible; freeze is the autonomous revocation",
        ),
        steps=_status_steps("CLOSED"),
        approval_ttl=timedelta(hours=4),
        postcondition=_status_post("CLOSED"),
    ),
    "run_limit_test": _spec(
        "run_limit_test",
        validate=validate_limit_test,
        policy=limit_test_policy,
        steps=tuple(
            _probe_step(k)
            for k in (
                ProbeKind.CURRENCY,
                ProbeKind.MERCHANT_CATEGORY,
                ProbeKind.OVER_LIMIT,
                ProbeKind.AT_LIMIT,
                ProbeKind.CUMULATIVE,
            )
        ),
        approval_ttl=timedelta(hours=1),
        postcondition=limit_test_postcondition,
    ),
}

APPROVAL_MATRIX: dict[str, str] = {
    "issue_card": "SINGLE; DUAL above the ALL_TIME threshold; BLOCKED over the cap",
    "update_card_controls": "AUTO for a pure reduction, also when the model starts it; any "
    "raise is SINGLE or DUAL",
    "freeze_card": "AUTO, also when the model starts it",
    "unfreeze_card": "SINGLE",
    "close_card": "SINGLE",
    "run_limit_test": "SINGLE with the debit shown; BLOCKED unless a funded limit-test card",
}
