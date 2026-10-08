"""Kit 3 policy: whether a replacement may be proposed, and every amount it would move.

Pure (rule 0): reads Airwallex and the kit store, never writes. The checks, re-run by the
runner's rule 1 and again in the build step:

1. Finality: the original must be CANCELLED with an allowlisted failure code (finality.assess).
2. Duplicate lock: no transfer may already sit under any of the original's replacement
   request_ids, and the kit-store lock (ledger.py) decides the attempt:
   - no lock: attempt 1;
   - a live lock (SENDING or IN_FLIGHT, M1): the money is already in flight on that
     request_id, so the plan RESUMES it with the identical Operation (rule 9 reconciles before
     any resend), never a new request_id; past the ~6-day margin of the ~7-day deduplication
     window a person decides and nothing is sent;
   - RELEASED (the attempt was definitively refused at its first send, or never sent): the
     next attempt, up to three;
   - LANDED or REPLACEMENT_FAILED: blocked.
3. Copyable: the original names a beneficiary_id and pays out in its own wallet currency.
4. Funding (fresh attempts only): the wallet holds the amount plus the SWIFT fee when the
   method is SWIFT. The original's fee is not assumed refunded, so a SWIFT incident budgets
   for both fees. A resume is not funding-checked: if the in-flight send landed unseen it has
   already been debited, and a resend under the same request_id cannot debit twice.

`computed` holds every amount the approver sees and the proposal hash covers it (M3),
but not the wallet balance: a deposit between proposal and run would turn the approval STALE
for no reason. The build step re-checks the balance instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from awx_common import Money, ReadRequest, RiskTier
from awx_common.store import KitStore
from awx_common.workflow import PolicyContext, PolicyDecision

from . import airwallex as awx
from . import ledger
from .finality import Assessment, IncidentDecision, assess
from .intent import TRANSFER_METHODS


@dataclass(frozen=True)
class ReplacementPlan:
    original_transfer_id: str
    attempt: int
    request_id: str
    beneficiary_id: str
    amount: Money
    transfer_method: str
    fee: Money
    required: Money
    reference: str
    resume: bool  # the attempt is already in flight; the build re-emits it, takes nothing

    def computed(self) -> dict[str, Any]:
        return {
            "original_transfer_id": self.original_transfer_id,
            "attempt": self.attempt,
            "replacement_request_id": self.request_id,
            "beneficiary_id": self.beneficiary_id,
            "amount": self.amount,
            "transfer_method": self.transfer_method,
            "fee": self.fee,
            "required_funds": self.required,
            "reference": self.reference,
        }


@dataclass(frozen=True)
class IncidentEvaluation:
    assessment: Assessment
    lock_held: bool
    existing_replacements: tuple[str, ...]
    plan: ReplacementPlan | None
    reasons: tuple[str, ...]
    lock: ledger.Lock | None = None

    @property
    def decision(self) -> IncidentDecision:
        """REPLACE only when every check passed; a held lock or a failed check escalates."""
        if self.assessment.decision is not IncidentDecision.REPLACE:
            return self.assessment.decision
        return IncidentDecision.REPLACE if self.plan is not None else IncidentDecision.ESCALATE


def swift_fee(original: Mapping[str, Any], amount: Money) -> Money | None:
    """The fee one more SWIFT payout costs, or None if code cannot price it."""
    fee_amount, fee_currency = original.get("fee_amount"), original.get("fee_currency")
    if fee_amount is not None and fee_currency == amount.currency:
        try:
            fee = awx.amount_of(fee_amount, fee_currency)
        except awx.UnreadableAmount:
            return None
        if fee.amount > 0:
            return fee
    return awx.SWIFT_FEE_FALLBACK.get(amount.currency)


def replacement_reference(original_transfer_id: str) -> str:
    """Distinct from the original's reference so a person can tell them apart in the web app,
    and never carrying the no-settle marker, so the replacement can settle."""
    return f"{awx.POC_LABEL}r-{original_transfer_id}"[:64]


def _attempt(lock: ledger.Lock | None, now: datetime) -> tuple[int | None, bool, tuple[str, ...]]:
    """(attempt, resume, reasons); attempt None means no replacement may be proposed."""
    if lock is None:
        return 1, False, ()
    if lock.state in ledger.LIVE_STATES:
        if lock.past_margin(now):
            return (
                None,
                False,
                (
                    f"replacement attempt {lock.attempt} has been in flight since "
                    f"{lock.taken_at.isoformat()}, past ~6 days of the ~7-day deduplication "
                    "window: a person decides; nothing is sent",
                ),
            )
        return (
            lock.attempt,
            True,
            (
                f"replacement attempt {lock.attempt} is in flight ({lock.state}); resuming it on "
                "its own request_id",
            ),
        )
    if lock.state == ledger.RELEASED:
        nxt = lock.attempt + 1
        if nxt > ledger.MAX_REPLACEMENT_ATTEMPTS:
            return None, False, ("every replacement attempt was refused: a person decides",)
        return nxt, False, (f"attempt {lock.attempt} was definitively refused; attempt {nxt}",)
    return None, False, (f"duplicate lock held: replacement {lock.state}",)


RESUMABLE = frozenset({"SCHEDULED", "PROCESSING", "SENT"})


def _created_at(transfer: Mapping[str, Any]) -> datetime | None:
    value = transfer.get("created_at")
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 5 and value[-5] in "+-" and value[-4:].isdigit():  # +0000 -> +00:00
        value = value[:-2] + ":" + value[-2:]
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def listed_past_margin(transfer: Mapping[str, Any], now: datetime) -> bool:
    """A listed replacement is resumable only inside the ~6-day margin of the ~7-day
    deduplication window, measured from its own created_at (with both stores lost there is no lock
    to measure from). An unreadable created_at, or no current time, fails closed."""
    if now is None:
        return True
    created = _created_at(transfer)
    return created is None or now - created >= ledger.DEDUPE_MARGIN


def _listed_attempt(listed: list[Mapping[str, Any]], original_transfer_id: str) -> int | None:
    if len(listed) != 1:
        return None
    rids = awx.all_replacement_request_ids(original_transfer_id)
    rid = listed[0].get("request_id")
    return rids.index(rid) + 1 if rid in rids else None


def evaluate_incident(
    read: awx.Reader,
    original_transfer_id: str,
    store: KitStore | None = None,
    now: datetime | None = None,
) -> IncidentEvaluation:
    original = awx.get_transfer(read, original_transfer_id)
    if original.get("id") not in (None, original_transfer_id):
        a = assess({"status": ""})
        return IncidentEvaluation(a, False, (), None, ("read returned a different transfer",))
    a = assess({**original, "id": original_transfer_id})
    listed = awx.find_replacements(read, original_transfer_id)
    existing = tuple(str(t.get("id") or "?") for t in listed)
    lock = ledger.get_lock(store, original_transfer_id)
    reasons = list(a.reasons)
    if a.decision is not IncidentDecision.REPLACE:
        return IncidentEvaluation(
            a, bool(existing) or lock is not None, existing, None, tuple(reasons), lock
        )
    if lock is not None and now is None:
        raise ValueError("evaluating a locked incident needs the current time")
    if existing:
        # The one replacement exists. If it is still in flight, the only thing left to do is
        # drive it to its own end: resume the identical Operation (it settles as a duplicate)
        # so the finality poll and the sandbox driver run. Never a new attempt.
        reasons.append(f"duplicate lock held: replacement {', '.join(existing)} already exists")
        attempt = _listed_attempt(listed, original_transfer_id)
        status = listed[0].get("status") if len(listed) == 1 else None
        if (
            attempt is None
            or status not in RESUMABLE
            or (lock is not None and now is not None and lock.past_margin(now))
            or listed_past_margin(listed[0], now)
        ):
            return IncidentEvaluation(a, True, existing, None, tuple(reasons), lock)
        resume, why = True, (f"replacement attempt {attempt} is {status}; resuming it",)
    else:
        attempt, resume, why = _attempt(lock, now) if lock is not None else (1, False, ())
    reasons += why
    held = lock is not None or bool(existing)
    if attempt is None:
        return IncidentEvaluation(a, True, existing, None, tuple(reasons), lock)

    problems: list[str] = []
    beneficiary_id = original.get("beneficiary_id")
    if not isinstance(beneficiary_id, str) or not beneficiary_id:
        problems.append("original has no beneficiary_id; inline beneficiaries are not copied")
    method = original.get("transfer_method")
    if method not in TRANSFER_METHODS:
        problems.append(f"original transfer_method {method!r} is not LOCAL or SWIFT")
    currency = original.get("transfer_currency")
    source_currency = original.get("source_currency") or currency
    if source_currency != currency:
        problems.append("original converted currency; cross-currency replacement not supported")
    amount: Money | None = None
    try:
        amount = awx.amount_of(original.get("transfer_amount"), currency)
    except awx.UnreadableAmount as exc:
        problems.append(f"original amount unreadable: {exc}")
    if amount is not None and amount.amount <= 0:
        problems.append("original amount is not positive")
    if problems or amount is None:
        return IncidentEvaluation(a, held, existing, None, (*reasons, *problems), lock)

    fee = Money.zero(amount.currency)
    if method == "SWIFT":
        priced = swift_fee(original, amount)
        if priced is None:
            return IncidentEvaluation(
                a,
                held,
                existing,
                None,
                (*reasons, f"cannot price a SWIFT fee in {amount.currency}"),
                lock,
            )
        fee = priced
    required = amount + fee
    if not resume:
        shortfall = funding_problem(read, required)
        if shortfall is not None:
            return IncidentEvaluation(a, held, existing, None, (*reasons, shortfall), lock)
        reasons.append(f"funded: wallet covers {required}")
    plan = ReplacementPlan(
        original_transfer_id=original_transfer_id,
        attempt=attempt,
        request_id=awx.replacement_request_id(original_transfer_id, attempt),
        beneficiary_id=beneficiary_id,
        amount=amount,
        transfer_method=method,
        fee=fee,
        required=required,
        reference=replacement_reference(original_transfer_id),
        resume=resume,
    )
    return IncidentEvaluation(a, held, existing, plan, tuple(reasons), lock)


def funding_problem(read: awx.Reader, required: Money) -> str | None:
    """Why the wallet cannot fund `required`, or None when it can."""
    try:
        available = awx.available_balance(read, required.currency)
    except awx.UnreadableAmount as exc:
        return f"balance unreadable: {exc}"
    if available < required:
        return f"insufficient funds: need {required} (fees included), have {available}"
    return None


def replace_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    ev = evaluate_incident(ctx.read, intent["original_transfer_id"], ctx.store, ctx.now)
    if ev.decision is not IncidentDecision.REPLACE or ev.plan is None:
        return PolicyDecision(RiskTier.BLOCKED, (f"decision {ev.decision}", *ev.reasons))
    return PolicyDecision(
        RiskTier.SINGLE,
        ("replacement is a new payment; one approver other than the proposer", *ev.reasons),
        ev.plan.computed(),
    )


# ---- send_supplier_transfer ---------------------------------------------------------------


def original_reference(payment_key: str, stage_for_incident: bool) -> str:
    """A staged original carries the no-settle marker so the sandbox driver holds it at SENT."""
    suffix = awx.NOSETTLE_MARKER if stage_for_incident else ""
    return f"{awx.POC_LABEL}{payment_key}"[: 64 - len(suffix)] + suffix


def _staging_beneficiary(ctx: PolicyContext, beneficiary_id: str) -> dict[str, str] | str:
    """The payee shown to the approver, or why staging refuses it: the beneficiary must exist
    and its nickname must start with kit 3's label."""
    from awx_common.errors import AwxError

    try:
        ben = ctx.read(ReadRequest(f"/api/v1/beneficiaries/{beneficiary_id}"))
    except AwxError as exc:
        return f"beneficiary {beneficiary_id} cannot be read ({type(exc).__name__}): blocked"
    nickname = str(ben.get("nickname") or "")
    if not nickname.startswith(awx.STAGING_BENEFICIARY_PREFIX):
        return f"beneficiary {beneficiary_id} is not a kit 3 demo beneficiary: blocked"
    inner = ben.get("beneficiary") or {}
    bank = inner.get("bank_details") or {}
    account = str(bank.get("account_number") or "")
    return {
        "nickname": nickname,
        "name": str(bank.get("account_name") or inner.get("company_name") or ""),
        "bank": str(bank.get("bank_name") or ""),
        "account_last4": account[-4:],
    }


def send_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    """Operator staging only (workflow.STAGING_SPECS; no agent tool): at most
        STAGING_MAX_AMOUNT USD (USD only) to a beneficiary whose nickname starts with POC_LABEL.

    The request_id is NOT in computed: it carries the book id, which the first build mints
        (rule 0 forbids the policy writing it), and computed must not change between proposal and
        execution. payment_key, in the intent, names it; the build derives it."""
    amount: Money = intent["amount"]
    key = intent["payment_key"]
    book = ledger.book_id(ctx.store)
    if book is not None:
        rid = awx.original_request_id(book, key)
        existing = awx.find_transfers_by_request_id(ctx.read, rid)
        if existing:
            return PolicyDecision(
                RiskTier.BLOCKED, (f"payment_key already paid by transfer {existing[0].get('id')}",)
            )
        sent = ledger.sent_record(ctx.store, key)
        if sent is not None and ctx.now - sent["sent_at"] >= ledger.DEDUPE_MARGIN:
            return PolicyDecision(
                RiskTier.BLOCKED,
                ("payment in flight past ~6 days of the deduplication window: a person decides",),
            )
    if amount.currency != awx.STAGING_CURRENCY:
        return PolicyDecision(
            RiskTier.BLOCKED,
            (f"staging pays {awx.STAGING_CURRENCY} only; asked {amount.currency}",),
        )
    if amount.amount > awx.STAGING_MAX_AMOUNT:
        return PolicyDecision(
            RiskTier.BLOCKED,
            (f"staging pays at most {awx.STAGING_MAX_AMOUNT} {amount.currency}; asked {amount}",),
        )
    payee = _staging_beneficiary(ctx, intent["beneficiary_id"])
    if isinstance(payee, str):
        return PolicyDecision(RiskTier.BLOCKED, (payee,))
    fee = Money.zero(amount.currency)
    if intent["transfer_method"] == "SWIFT":
        priced = awx.SWIFT_FEE_FALLBACK.get(amount.currency)
        if priced is None:
            return PolicyDecision(
                RiskTier.BLOCKED, (f"cannot price a SWIFT fee in {amount.currency}",)
            )
        fee = priced
    required = amount + fee
    shortfall = funding_problem(ctx.read, required)
    if shortfall is not None:
        return PolicyDecision(RiskTier.BLOCKED, (shortfall,))
    return PolicyDecision(
        RiskTier.SINGLE,
        ("supplier payment; one approver other than the proposer",),
        {
            "payment_key": key,
            "beneficiary_id": intent["beneficiary_id"],
            "beneficiary": payee,  # what the approver sees; bound in the hash
            "amount": amount,
            "transfer_method": intent["transfer_method"],
            "fee": fee,
            "required_funds": required,
            "reference": original_reference(key, intent["stage_for_incident"]),
        },
    )


def simulate_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    """AUTO at the policy level (a sandbox object kit 3 made), but a model-started proposal
    still gets one approval: the spec keeps the default agent_min_tier (SINGLE)."""
    transfer = awx.get_transfer(ctx.read, intent["transfer_id"])
    if not awx.is_ours(transfer):
        return PolicyDecision(
            RiskTier.BLOCKED, (f"transfer was not created by kit 3 (no {awx.POC_LABEL})",)
        )
    status = transfer.get("status")
    if status not in (*awx.SIM_SENT_FROM, "SENT"):
        return PolicyDecision(
            RiskTier.BLOCKED,
            (f"can only fail a SCHEDULED, PROCESSING or SENT transfer, not {status}",),
        )
    return PolicyDecision(
        RiskTier.AUTO,
        ("sandbox simulation of an object kit 3 created",),
        {"transfer_id": intent["transfer_id"], "failure_type": intent["failure_type"]},
    )
