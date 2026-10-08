"""Kit 1 workflows: propose_lock_rate, propose_execute_plan, propose_release_obligation.

lock_rate (ratelock.py, AUTO): plan the requested obligations, create one Airwallex FX quote
          per bought currency and store it under ratelock/<CUR>. Moves no money.
execute_plan (this module):
policy    load the book from ctx.store; BLOCK a book with no id, unknown IDs, and any
          obligation whose lock is LOCKED, SUBMITTED, PAID or NEEDS_PERSON (with its age); plan
          against the STORED rate locks, so computed carries each conversion's exact buy, its
          exact sell from the locked quote, and the quote id (M3). No usable lock (missing,
          another amount, expired or already used) is BLOCKED: re-lock. Live rates are read only
          to plan currencies that have no lock, and such a plan is BLOCKED, so a rate moving
          between approval and execution cannot move the hash (rule 1).
convert   take the obligation locks (owner: proposal_id, M4); reconcile each conversion by
          request_id; for any not landed, check the locked quote still has its margin,
          re-read balances, re-check the WHOLE plan at the locked amounts with fees, then burn
          the quote, record the body and book it
transfer  reconcile each transfer; for any not landed, every conversion must have succeeded
          and the final reserve check must pass before it is sent
postcond  move each lock to PAID, SUBMITTED, NEEDS_PERSON or RELEASED (M1), and check every
          touched wallet is still at or above its floor
release_obligation (release.py): a person-approved move of a stuck lock, from evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from awx_common import Money, Operation, OperationResult, OperationStatus, RiskTier
from awx_common.workflow import (
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    ReconcileUndetermined,
    SandboxAdvance,
    StepContext,
    StepSpec,
)

from .api import (
    CONVERSION_FINALITY,
    READ_ALLOWLIST,
    TRANSFERS_CREATE,
    find_conversion,
    find_transfer,
    read_balances,
    read_rates,
    transfer_finality,
)
from .book import NO_BOOK, TransferMethod, TreasuryBook, load_book, load_book_id
from .intake import InvalidInput, parse_contradictions, parse_obligation_ids, require_keys
from .keys import (
    KIT,
    book_id_of,
    conversion_op,
    conversion_request_id,
    landed_or_unknown,
    obligation_key,
    op,
    transfer_body,
    transfer_request_id,
)
from .locks import (
    BLOCKING,
    NOTE_REFUSED,
    Acquired,
    LockState,
    ObligationLocked,
    acquire,
    burn_quote,
    claim_for_emission,
    get_submission,
    index_emission,
    need_store,
    quote_owner,
    read_lock,
    read_ratelock,
    record_run_attempts,
    record_submission,
    reenter,
    run_attempts,
    set_state,
    undo_acquire,
)
from .money_rules import (
    BadProviderData,
    ConversionLeg,
    IndicativeRate,
    K1Error,
    PayoutLeg,
    ReserveBreach,
    project,
    to_decimal,
    whole_plan_breaches,
)
from .plan import Plan, build_plan
from .policy import PolicyConfig, approval_tier, confidence_band

ACTION = "execute_plan"
_FIELDS = {"obligation_ids", "contradictions"}


class BookChanged(K1Error):
    pass


class BalanceChanged(K1Error):
    """A wallet the approver saw is now lower. Re-propose against the new balance."""


class QuoteExpired(K1Error):
    pass


class PriorStepFailed(K1Error):
    pass


class MissingRecord(K1Error):
    """The provider has an object under our request_id but we hold no record of its body."""


# --- computed helpers -----------------------------------------------------------------------


def payouts_of(computed: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(p) for p in computed.get("payouts", ())]


def conversions_of(computed: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(c) for c in computed.get("conversions", ())]


def payout_legs(computed: Mapping[str, Any]) -> list[PayoutLeg]:
    return [
        PayoutLeg(
            p["obligation_id"],
            p["beneficiary_id"],
            p["amount"],
            p["fee"],
            TransferMethod(p["method"]),
        )
        for p in payouts_of(computed)
    ]


def transfer_op(ctx: StepContext, p: Mapping[str, Any], attempt: int) -> Operation:
    book_id = book_id_of(ctx.decision.computed)
    rid = transfer_request_id(book_id, p["obligation_id"], attempt)
    return op(
        ctx,
        "transfers.create",
        TRANSFERS_CREATE,
        transfer_body(p, rid, book_id),
        rid,
        obligation_key(book_id, p["obligation_id"]),
    )


class ConversionMismatch(K1Error):
    """A landed conversion does not match the approved quote or sell amount (M3)."""


def check_landed_conversions(
    results: Sequence[OperationResult], approved: Sequence[Mapping[str, Any]]
) -> None:
    """M3: every landed conversion booked the approved quote at the approved
    sell. A provider that did not honour the quote fails the run closed before any transfer."""
    for r, c in zip(results, approved, strict=True):
        body = r.body or {}
        qid, sell = body.get("quote_id"), body.get("sell_amount")
        if qid != c["quote_id"]:
            raise ConversionMismatch(
                f"conversion {r.request_id} booked quote {qid}, not {c['quote_id']}"
            )
        try:
            got = to_decimal(sell)
        except BadProviderData as exc:
            raise ConversionMismatch(f"conversion {r.request_id} has no sell amount") from exc
        if got != c["sell"].amount:
            raise ConversionMismatch(
                f"conversion {r.request_id} sold {got}, the approval bound {c['sell'].amount}"
            )


def refused_transfer(sent: Sequence[OperationResult]) -> bool:
    return bool(sent) and not any(landed_or_unknown(r) for r in sent)


# --- shared policy prelude ------------------------------------------------------------------


def lock_age(lock, now: datetime) -> str:
    since = lock.submitted_at
    if since is None:
        return ""
    return f", {(now - since).days} days since submission"


def book_prelude(
    ids: Sequence[str], ctx: PolicyContext, cfg: PolicyConfig
) -> tuple[TreasuryBook, str] | PolicyDecision:
    """The checks every action that may move an obligation's money runs first."""
    book = load_book(ctx.store)
    if book is None:
        return PolicyDecision(RiskTier.BLOCKED, (NO_BOOK,))
    book_id = load_book_id(ctx.store)
    if not book_id:  # M2: never a default
        return PolicyDecision(RiskTier.BLOCKED, ("the treasury book has no book id; reseed it",))
    unknown = sorted(set(ids) - {o.obligation_id for o in book.obligations})
    if unknown:
        return PolicyDecision(RiskTier.BLOCKED, (f"unknown obligations {unknown}",))
    locked = []
    for oid in ids:
        ob = book.obligation(oid)
        lock = read_lock(ctx.store, oid) if ctx.store else None
        if lock is not None and lock.state in BLOCKING:
            stale = (
                lock.state is LockState.SUBMITTED
                and lock.submitted_at is not None
                and ctx.now - lock.submitted_at > cfg.dedup_window
            )
            tail = "; needs a person (propose_release_obligation)" if stale else ""
            if lock.state is LockState.NEEDS_PERSON:
                tail = f"; needs a person ({lock.note or 'no note'})"
            locked.append(
                f"{oid} is {lock.state.value} (v{lock.obligation_version}, now v{ob.version}"
                f"{lock_age(lock, ctx.now)}){tail}"
            )
    if locked:
        return PolicyDecision(
            RiskTier.BLOCKED,
            tuple(f"already paid or in flight: {r}" for r in locked),
            {"book_version": book.book_version},
        )
    return book, book_id


def usable_ratelock(
    ctx: PolicyContext, book_id: str, currency: str, cfg: PolicyConfig
) -> tuple[Mapping[str, Any] | None, str]:
    """The stored rate lock for `currency` if it can still be booked, else (None, why)."""
    lock = read_ratelock(ctx.store, currency)
    if lock is None:
        return None, f"no rate lock for {currency}"
    if lock.get("book_id") != book_id:
        return None, f"the {currency} rate lock belongs to another book"
    if lock["valid_to"] - ctx.now < cfg.quote_margin:
        return None, f"the {currency} rate lock expired at {lock['valid_to'].isoformat()}"
    if quote_owner(ctx.store, lock["quote_id"]) is not None:
        return None, f"the {currency} rate lock was already used"
    return lock, ""


def locked_rate(lock: Mapping[str, Any]) -> IndicativeRate:
    """The quote's own price as a rate, for planning (feasibility only; the exact sell comes
    from the quote)."""
    buy: Money = lock["buy"]
    sell: Money = lock["sell"]
    return IndicativeRate(
        buy.currency, sell.currency, buy.currency + sell.currency, sell.amount / buy.amount
    )


def with_exact_sells(plan: Plan, sells: Mapping[str, Money], floors) -> Plan:
    """The plan with each conversion's sell replaced by the locked quote's exact sell."""
    convs = tuple(ConversionLeg(c.buy, sells[c.buy.currency]) for c in plan.conversions)
    return replace(
        plan,
        conversions=convs,
        projected=project(plan.balances, convs, plan.payouts),
        breaches=whole_plan_breaches(plan.balances, convs, plan.payouts, floors),
    )


def build_execute_spec(
    config: PolicyConfig | None = None, *, transfer_advance: SandboxAdvance | None = None
) -> KitWorkflowSpec:
    """`transfer_advance` is the sandbox finality driver (rule 10):
    awx_common.sandbox.make_transfer_advance("k1") in workflow.SPECS."""
    cfg = config or PolicyConfig()

    def book_for(ctx: StepContext) -> TreasuryBook:
        store = need_store(ctx.policy.store)
        book = load_book(store)
        computed = ctx.decision.computed
        if (
            book is None
            or book.book_version != computed.get("book_version")
            or load_book_id(store) != book_id_of(computed)
        ):
            raise BookChanged("the treasury book changed after this proposal; re-propose")
        for p in payouts_of(computed):
            ob = book.obligation(p["obligation_id"])
            if (ob.version, ob.beneficiary_id, ob.amount, ob.method.value) != (
                p["obligation_version"],
                p["beneficiary_id"],
                p["amount"],
                p["method"],
            ):
                raise BookChanged(f"{ob.obligation_id} changed in the book")
        return book

    def locks_for(ctx: StepContext, *, reenter_only: bool = False) -> dict[str, int]:
        """The run's obligation locks (owner: its proposal_id, M4).

        The first step takes them all or none and records the attempts it took under
        run/<proposal_id>. Every later step, and any redelivery of the first, only re-enters:
        the lock must still be LOCKED, this run's, at the recorded attempt. A
        person's release in between therefore fails the run closed."""
        store = need_store(ctx.policy.store)
        pid = ctx.proposal_id
        payouts = payouts_of(ctx.decision.computed)
        recorded = run_attempts(store, pid)
        if reenter_only or recorded is not None:
            if recorded is None:
                raise ObligationLocked("this run never took its obligation locks")
            return {
                p["obligation_id"]: reenter(
                    store,
                    p["obligation_id"],
                    p["obligation_version"],
                    pid,
                    recorded[p["obligation_id"]],
                )
                for p in payouts
            }
        taken: list[tuple[str, Acquired]] = []
        try:
            for p in payouts:
                oid = p["obligation_id"]
                # Never retake a RELEASED lock this same run held before
                # (a person released it while the run's attempts were not yet recorded).
                taken.append(
                    (
                        oid,
                        acquire(
                            store,
                            oid,
                            p["obligation_version"],
                            pid,
                            ctx.policy.now,
                            refuse_own_release=True,
                        ),
                    )
                )
        except ObligationLocked:
            for oid, got in reversed(taken):
                undo_acquire(store, oid, pid, got)
            raise
        return record_run_attempts(store, pid, {oid: got.attempt for oid, got in taken})

    def validate(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        require_keys(raw, _FIELDS, "propose_execute_plan input")
        return {
            "obligation_ids": parse_obligation_ids(raw.get("obligation_ids")),
            "contradictions": parse_contradictions(raw.get("contradictions")),
        }

    def policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        ids = list(intent["obligation_ids"])
        pre = book_prelude(ids, ctx, cfg)
        if isinstance(pre, PolicyDecision):
            return pre
        book, book_id = pre
        bad_receipts = sorted(
            {
                c["receipt_id"]
                for c in intent["contradictions"]
                if not book.has_forecast(c["receipt_id"])
            }
        )
        if bad_receipts:
            return PolicyDecision(RiskTier.BLOCKED, (f"unknown receipts {bad_receipts}",))
        balances = read_balances(ctx.read, book)
        buy_currencies = {book.obligation(i).amount.currency for i in ids} - {book.funding_currency}
        locks: dict[str, Mapping[str, Any]] = {}
        why_not: dict[str, str] = {}
        for cur in sorted(buy_currencies):
            lock, why = usable_ratelock(ctx, book_id, cur, cfg)
            if lock is not None:
                locks[cur] = lock
            else:
                why_not[cur] = why
        rates = {cur: locked_rate(lock) for cur, lock in locks.items()}
        if why_not:
            # Plan the unlocked currencies at live rates only to say what to lock.
            rates.update(read_rates(ctx.read, book, set(why_not)))
        plan = build_plan(book, balances, rates, cfg.plan, only=ids)
        needs: list[str] = []
        sells: dict[str, Money] = {}
        for c in plan.conversions:
            cur = c.buy.currency
            lock = locks.get(cur)
            if lock is None:
                needs.append(f"{why_not.get(cur, 'no rate lock')}; lock {c.buy} first")
                continue
            if lock["buy"] != c.buy:
                needs.append(
                    f"the {cur} rate lock buys {lock['buy']}, the plan needs {c.buy}; re-lock"
                )
                continue
            sells[cur] = lock["sell"]
        if needs:
            return PolicyDecision(
                RiskTier.BLOCKED,
                tuple(needs),
                {"needs_rate_lock": [{"buy": c.buy} for c in plan.conversions]},
            )
        plan = with_exact_sells(plan, sells, book.floors)
        band = confidence_band(book.forecasts, {c["receipt_id"] for c in intent["contradictions"]})
        tier = approval_tier(plan, band, book, cfg, requested=ids)
        converted = {d.obligation_id: d.buy for d in plan.decisions if d.buy is not None}
        computed = {
            "book_version": book.book_version,
            "book_id": book_id,
            # The attempt each obligation's lock last reached. A re-proposal after a definitive
            # failure (RELEASED) therefore gets a new proposal ID (the execution name), instead
            # of reaching the finished execution of the failed attempt.
            "prior_attempts": {
                i: (lk.attempt if (lk := read_lock(ctx.store, i)) is not None else 0) for i in ids
            },
            "band": band.value,
            "floors": dict(book.floors),
            "balances_seen": dict(plan.balances),
            "projected": dict(plan.projected),
            "conversions": [
                {
                    "currency": c.buy.currency,
                    "buy": c.buy,
                    "sell": c.sell,  # exact, from the locked quote (M3)
                    "quote_id": locks[c.buy.currency]["quote_id"],
                    "quote_valid_to": locks[c.buy.currency]["valid_to"],
                    "funds": sorted(
                        o for o, b in converted.items() if b.currency == c.buy.currency
                    ),
                }
                for c in plan.conversions
            ],
            "payouts": [
                {
                    "obligation_id": p.obligation_id,
                    "obligation_version": book.obligation(p.obligation_id).version,
                    "beneficiary_id": p.beneficiary_id,
                    "amount": p.amount,
                    "fee": p.fee,
                    "method": p.method.value,
                    "converted": converted.get(p.obligation_id),
                }
                for p in plan.payouts
            ],
            "decisions": [
                {"obligation_id": d.obligation_id, "kind": d.kind.value, "reason": d.reason}
                for d in plan.decisions
            ],
        }
        return PolicyDecision(tier.tier, tier.reasons, computed)

    def build_conversions(ctx: StepContext) -> Sequence[Operation]:
        book = book_for(ctx)
        computed = ctx.decision.computed
        store = need_store(ctx.policy.store)
        attempts = locks_for(ctx)  # before anything else: the obligations are now ours
        convs = conversions_of(computed)
        if not convs:
            return ()
        rows: list[tuple[dict[str, Any], str, bool]] = []
        for c in convs:
            rid = conversion_request_id(computed, c, attempts)
            rows.append(
                (c, rid, find_conversion(conversion_op(ctx, c, rid), ctx.policy.read) is not None)
            )
        pending = [(c, rid) for c, rid, landed in rows if not landed]
        if pending:
            for c, _rid in pending:
                if c["quote_valid_to"] - ctx.policy.now < cfg.quote_margin:
                    raise QuoteExpired(
                        f"the {c['currency']} rate lock expires too soon; re-lock and re-propose"
                    )
            payouts = payout_legs(computed)
            fresh = read_balances(ctx.policy.read, book)
            if not any(landed for _, _, landed in rows):
                seen: Mapping[str, Money] = computed["balances_seen"]
                involved = {c["currency"] for c in convs} | {c["sell"].currency for c in convs}
                involved |= {p.amount.currency for p in payouts}
                for cur in sorted(involved):
                    if fresh[cur] < seen[cur]:
                        raise BalanceChanged(
                            f"{cur} is {fresh[cur]}, below the {seen[cur]} the approver saw"
                        )
            legs = [ConversionLeg(c["buy"], c["sell"]) for c, _ in pending]
            breaches = whole_plan_breaches(fresh, legs, payouts, book.floors)
            if breaches:
                raise ReserveBreach(
                    f"the whole plan at locked rates breaches the floor in {breaches}"
                )
            for c, rid in pending:
                # Record first, claim second. The submission record and its
                # per-obligation emission index exist before the CAS claim, so a release that
                # reads the ledger at any point after the claim sees the emission, and a release
                # that lands before the claim makes the claim (and so the send) fail.
                burn_quote(store, c["quote_id"], rid)
                record_submission(
                    store,
                    rid,
                    "conversion",
                    conversion_op(ctx, c, rid).body,
                    recorded_at=ctx.policy.now,
                )
                for oid in c["funds"]:
                    index_emission(store, oid, attempts[oid], rid, "conversion")
                for oid in c["funds"]:
                    claim_for_emission(
                        store,
                        oid,
                        ctx.proposal_id,
                        attempts[oid],
                        ctx.policy.now,
                        conversion_rids=(rid,),
                    )
        ops = []
        for c, rid, _landed in rows:
            if get_submission(store, rid) is None:
                raise MissingRecord(f"conversion {rid} exists at the provider with no record")
            ops.append(conversion_op(ctx, c, rid))
        return ops

    def build_transfers(ctx: StepContext) -> Sequence[Operation]:
        book = book_for(ctx)
        computed = ctx.decision.computed
        store = need_store(ctx.policy.store)
        attempts = locks_for(ctx, reenter_only=True)  # never acquire here
        rows = []
        for p in payouts_of(computed):
            t = transfer_op(ctx, p, attempts[p["obligation_id"]])
            rows.append((p, t, find_transfer(t, ctx.policy.read) is not None))
        pending = [(p, t) for p, t, landed in rows if not landed]
        if pending:
            conv = ctx.prior.get("convert", ())
            if len(conv) != len(conversions_of(computed)) or any(
                r.status is not OperationStatus.SUCCEEDED for r in conv
            ):
                raise PriorStepFailed("a conversion did not succeed; no transfer is sent")
            check_landed_conversions(conv, conversions_of(computed))
            # Last line of defence after the conversions moved money.
            fresh = read_balances(ctx.policy.read, book)
            legs = [
                PayoutLeg(
                    p["obligation_id"],
                    p["beneficiary_id"],
                    p["amount"],
                    p["fee"],
                    TransferMethod(p["method"]),
                )
                for p, _ in pending
            ]
            breaches = whole_plan_breaches(fresh, (), legs, book.floors)
            if breaches:
                raise ReserveBreach(f"transfers would breach the floor in {breaches}; escalate")
        for p, t, _landed in rows:
            # Record (with its emission index) BEFORE the CAS claim. A release
            # whose policy ran after the record sees the emission and is blocked; one that read
            # the ledger before it binds a lock version the claim then moves (LockMoved); one
            # whose write lands before the claim makes this claim raise, so nothing is sent.
            record_submission(store, t.request_id, "transfer", t.body, recorded_at=ctx.policy.now)
            oid = p["obligation_id"]
            index_emission(store, oid, attempts[oid], t.request_id, "transfer")
            claim_for_emission(
                store,
                oid,
                ctx.proposal_id,
                attempts[oid],
                ctx.policy.now,
                request_ids=(t.request_id,),
            )
        return [t for _, t, _ in rows]

    def postcondition(ctx: StepContext) -> PostconditionResult:
        store = ctx.policy.store
        computed = ctx.decision.computed
        payouts = payouts_of(computed)
        if store is None:
            return PostconditionResult(False, "no kit store; locks not updated")
        owner = ctx.proposal_id  # M4: never the intent hash
        owned: dict[str, int] = {}
        lock_state: dict[str, LockState] = {}
        submitted_at: dict[str, datetime | None] = {}
        for p in payouts:
            lock = read_lock(store, p["obligation_id"])
            if (
                lock is not None
                and lock.owner == owner
                and lock.obligation_version == p["obligation_version"]
                and lock.state is not LockState.RELEASED
            ):
                owned[p["obligation_id"]] = lock.attempt
                lock_state[p["obligation_id"]] = lock.state
                submitted_at[p["obligation_id"]] = lock.submitted_at
        if len(owned) != len(payouts):
            # This run does not own every lock, so its builds emitted nothing (locks_for is all
            # or none and undoes its own). Touch nothing.
            return PostconditionResult(False, "this run does not hold every obligation lock")

        def results_for(step: str, rid: str) -> list[OperationResult]:
            return [r for r in ctx.prior.get(step, ()) if r.request_id == rid]

        def sent_ids(step: str, rid: str) -> set[str]:
            return {r.provider_id for r in results_for(step, rid) if r.provider_id}

        def probe(fn, operation, ids: set[str], id_key: str) -> str:
            """ "landed" | "absent" | "unknown". A found object whose id is not the one this run
            was given for the request_id is another object: unknown."""
            try:
                found = fn(operation, ctx.policy.read)
            except (ReconcileUndetermined, BadProviderData, KeyError):
                return "unknown"
            if found is None:
                return "absent"
            if ids and found.get(id_key) not in ids:
                return "unknown"
            return "landed"

        # Per obligation: did a write for it land or end unknown; did THIS run receive a
        # definitive refusal; and does the submission ledger show anything was ever emitted
        # under this attempt (by any delivery of any run)?
        moved: dict[str, bool] = {p["obligation_id"]: False for p in payouts}
        refused: dict[str, bool] = {p["obligation_id"]: False for p in payouts}
        emitted: dict[str, bool] = {p["obligation_id"]: False for p in payouts}
        for c in conversions_of(computed):
            rid = conversion_request_id(computed, c, owned)
            seen = probe(
                find_conversion,
                conversion_op(ctx, c, rid),
                sent_ids("convert", rid),
                "conversion_id",
            )
            sent = results_for("convert", rid)
            ledger = (
                get_submission(store, rid) is not None or quote_owner(store, c["quote_id"]) == rid
            )
            moved_here = seen != "absent" or any(landed_or_unknown(r) for r in sent)
            refused_here = bool(sent) and not any(landed_or_unknown(r) for r in sent)
            for oid in c["funds"]:
                moved[oid] = moved[oid] or moved_here
                refused[oid] = refused[oid] or refused_here
                emitted[oid] = emitted[oid] or ledger
        money_moved = any(moved.values())

        now = ctx.policy.now
        states: dict[str, LockState] = {}
        notes: dict[str, str] = {}
        for p in payouts:
            oid = p["obligation_id"]
            t = transfer_op(ctx, p, owned[oid])
            sent = results_for("transfer", t.request_id)
            emitted[oid] = emitted[oid] or get_submission(store, t.request_id) is not None
            try:
                found = find_transfer(t, ctx.policy.read)
            except (ReconcileUndetermined, BadProviderData, KeyError):
                states[oid], notes[oid] = LockState.NEEDS_PERSON, "reconcile undetermined"
                continue
            ids = sent_ids("transfer", t.request_id)
            if found is not None and ids and found.get("id") not in ids:
                # The listed object is not the one this run was given (an older object under
                # the same request_id while ours lags): a person decides.
                states[oid] = LockState.NEEDS_PERSON
                notes[oid] = "listed transfer is not this run's object"
                continue
            if found is None:
                if any(
                    r.status is OperationStatus.SUCCEEDED or r.error_type == "DuplicateRequest"
                    for r in sent
                ):
                    states[oid] = LockState.SUBMITTED  # accepted; the list lags
                elif any(landed_or_unknown(r) for r in sent):
                    states[oid], notes[oid] = LockState.NEEDS_PERSON, "outcome unknown"
                elif money_moved:
                    states[oid] = LockState.NEEDS_PERSON
                    notes[oid] = "converted, not paid" + (
                        f"; {NOTE_REFUSED}" if sent and refused_transfer(sent) else ""
                    )
                elif (sent or refused[oid]) and not moved[oid]:
                    states[oid] = LockState.RELEASED  # this run read a definitive refusal
                elif lock_state[oid] is LockState.LOCKED and not emitted[oid]:
                    # A build raised before emitting anything. The
                    # submission ledger (written before any operation is returned, and by every
                    # delivery) shows nothing was ever sent under this attempt, and no quote was
                    # burned for it: nothing can have landed. Judged from the ledger, never from
                    # ctx.prior, which a re-delivery sees empty. Needs a consistent store read.
                    states[oid] = LockState.RELEASED
                    notes[oid] = "nothing emitted under this attempt"
                else:
                    states[oid], notes[oid] = LockState.NEEDS_PERSON, "no definitive outcome read"
                prev = submitted_at.get(oid)
                if (
                    states[oid] is LockState.SUBMITTED
                    and prev is not None
                    and now - prev > cfg.dedup_window
                ):
                    states[oid] = LockState.NEEDS_PERSON
                    notes[oid] = "submitted past the dedup window, provider does not show it"
            elif found.get("status") == "PAID":
                states[oid] = LockState.PAID
            elif found.get("status") == "CANCELLED":
                definitive = isinstance(found.get("failure"), Mapping)
                if definitive and not money_moved:
                    states[oid] = LockState.RELEASED
                else:
                    states[oid], notes[oid] = LockState.NEEDS_PERSON, "cancelled after conversion"
            else:
                states[oid] = LockState.SUBMITTED
        for oid, state in states.items():
            set_state(store, oid, owner, state, notes.get(oid, ""), now=now)

        try:
            book = load_book(store)
            fresh = read_balances(ctx.policy.read, book) if book else None
        except BadProviderData as exc:
            return PostconditionResult(False, f"balance read failed: {exc}")
        if fresh is None:
            return PostconditionResult(False, "no treasury book")
        involved = {c["currency"] for c in conversions_of(computed)}
        involved |= {c["sell"].currency for c in conversions_of(computed)}
        involved |= {p["amount"].currency for p in payouts}
        floors = computed["floors"]
        below = [c for c in sorted(involved) if fresh[c] < floors[c]]
        lock_summary = ", ".join(f"{o} {s.value}" for o, s in sorted(states.items()))
        if below:
            return PostconditionResult(False, f"reserve floor breached in {below}; {lock_summary}")
        if any(s is not LockState.PAID for s in states.values()):
            return PostconditionResult(False, f"not every obligation is PAID: {lock_summary}")
        detail = ", ".join(
            f"{c} {fresh[c].amount} (floor {floors[c].amount})" for c in sorted(involved)
        )
        return PostconditionResult(True, f"remaining reserve: {detail}")

    return KitWorkflowSpec(
        kit=KIT,
        action=ACTION,
        validate=validate,
        policy=policy,
        steps=(
            StepSpec("convert", build_conversions, CONVERSION_FINALITY, find_conversion),
            StepSpec(
                "transfer", build_transfers, transfer_finality(transfer_advance), find_transfer
            ),
        ),
        approval_ttl=cfg.approval_ttl,
        read_allowlist=READ_ALLOWLIST,
        postcondition=postcondition,
        # DESIGN.md "AUTO for agent proposals".
        agent_min_tier=RiskTier.AUTO,
    )


def build_specs(
    config: PolicyConfig | None = None, *, transfer_advance: SandboxAdvance | None = None
) -> dict[str, KitWorkflowSpec]:
    from .ratelock import build_lock_spec
    from .release import build_release_spec

    specs = (
        build_lock_spec(config),
        build_execute_spec(config, transfer_advance=transfer_advance),
        build_release_spec(config),
    )
    return {s.action: s for s in specs}


def build_spec(
    config: PolicyConfig | None = None, *, transfer_advance: SandboxAdvance | None = None
) -> KitWorkflowSpec:
    """The execute_plan spec; the same as build_execute_spec."""
    return build_execute_spec(config, transfer_advance=transfer_advance)


def _sandbox_advance() -> SandboxAdvance:
    from awx_common.sandbox import make_transfer_advance

    return make_transfer_advance(KIT)


SPECS: dict[str, KitWorkflowSpec] = build_specs(transfer_advance=_sandbox_advance())

__all__ = ["SPECS", "InvalidInput", "build_execute_spec", "build_spec", "build_specs"]
