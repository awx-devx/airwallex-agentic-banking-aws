"""Kit 8 state on `PolicyContext.store`: an awx_common KitStore already scoped to kit 8 and ONE
seller tenant, so nothing here can name, read or write another seller.

Keys (all tenant-local):

    book                    {"book_id"}: random id minted once with the book (rule M2)
    profile                 SellerProfile
    cycle/<cycle_id>        CycleData
    orders                  the seller's order book
    evidence/<event_id>     raw carrier event (indexed per seller, so a forged `seller` field in
                            another seller's evidence is never even reachable)
    override/<event_id>     an approved evidence-driven reserve hold, carried forward
    ledger                  {"ledger": SellerLedger, "postings": {key: payload},
                             "recoveries": {attempt: RecoveryAttempt}}

The ledger, its posting keys and the recovery-charge claims live in ONE item, written with
optimistic concurrency. A posting and its idempotency marker land together or not at all, and a
settlement that nets a receivable and a recovery claim on that receivable can never both win:
whichever writes second fails with a version conflict and re-reads.
Values round-trip through awx_common.transport, so dataclasses are stored as records and
rebuilt on read.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any

from awx_common import Money
from awx_common.errors import NotFound, StateConflict
from awx_common.store import KitStore, VersionConflict
from awx_common.workflow import PolicyContext

from .evidence import Order
from .ledger import CycleSettlement, SellerLedger


def _record(obj: Any) -> dict[str, Any]:
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


@dataclass(frozen=True)
class SellerProfile:
    seller: str
    currency: str
    tenure_days: int
    trailing_refund_rate: Decimal  # from the seller's order history; a fraction in [0, 1]


@dataclass(frozen=True)
class CycleData:
    cycle_id: str
    entitlement: Money
    refunds: Money
    volume_base: Money  # trailing sales volume the reserve rate applies to


@dataclass(frozen=True)
class ReserveOverride:
    """An approved, evidence-driven reserve hold. It applies to every later cycle and decays as
    its orders are delivered or refunded (see workflow.planned_target)."""

    event_id: str
    cycle_id: str  # the cycle it was approved against
    target: Money
    order_ids: tuple[str, ...]
    owner_proposal_id: str  # rule M4: the run that took this hold


@dataclass(frozen=True)
class RecoveryAttempt:
    attempt: int
    amount: Money
    request_id: str
    outcome: str  # PENDING | SETTLED | FAILED | INSUFFICIENT_FUNDS | SKIPPED_STALE_BALANCE
    # | SUSPENDED
    owner_proposal_id: str = ""  # rule M4: the run that took the claim; kept on every update


PENDING_RECOVERY = frozenset({"PENDING", "SUSPENDED"})


def kit_store(ctx: PolicyContext) -> KitStore:
    if ctx.store is None:
        raise StateConflict("kit 8 needs PolicyContext.store; refusing to run without state")
    return ctx.store


# Reads (pure) -----------------------------------------------------------------------------------


def _get(store: KitStore, key: str, what: str) -> Mapping[str, Any]:
    item = store.get(key)
    if item is None:
        raise NotFound(f"no {what} for this seller")
    return item.value


def profile(store: KitStore) -> SellerProfile:
    v = _get(store, "profile", "seller profile")
    return SellerProfile(v["seller"], v["currency"], v["tenure_days"], v["trailing_refund_rate"])


def cycle(store: KitStore, cycle_id: str) -> CycleData:
    v = _get(store, f"cycle/{cycle_id}", "cycle data")
    return CycleData(v["cycle_id"], v["entitlement"], v["refunds"], v["volume_base"])


def orders(store: KitStore, seller: str) -> tuple[Order, ...]:
    item = store.get("orders")
    if item is None:
        return ()
    out = []
    for o in item.value["orders"]:
        out.append(Order(o["order_id"], o["seller"], o["amount"], o["refunded"], o["delivered"]))
    return tuple(o for o in out if o.seller == seller)


def carrier_event(store: KitStore, event_id: str) -> Mapping[str, Any]:
    return _get(store, f"evidence/{event_id}", "carrier event")


def overrides(store: KitStore) -> tuple[ReserveOverride, ...]:
    return tuple(
        ReserveOverride(
            v["event_id"],
            v["cycle_id"],
            v["target"],
            tuple(v["order_ids"]),
            v["owner_proposal_id"],
        )
        for v in (i.value for i in store.query("override/"))
    )


@dataclass(frozen=True)
class LedgerState:
    ledger: SellerLedger
    postings: Mapping[str, Any]
    version: int | None  # KitStore version; None when the ledger has never been written
    recoveries: Mapping[str, Any] = None  # attempt (str) -> RecoveryAttempt record

    def __post_init__(self) -> None:
        if self.recoveries is None:
            object.__setattr__(self, "recoveries", {})


def ledger_state(store: KitStore, seller: str, currency: str) -> LedgerState:
    item = store.get("ledger")
    if item is None:
        return LedgerState(SellerLedger.open(seller, currency), {}, None, {})
    led = SellerLedger.from_record(item.value["ledger"])
    if led.seller != seller:
        raise StateConflict("stored ledger belongs to a different seller")
    return LedgerState(
        led,
        dict(item.value["postings"]),
        item.version,
        dict(item.value.get("recoveries", {})),
    )


def settlement(state: LedgerState, cycle_id: str) -> CycleSettlement | None:
    rec = state.postings.get(f"settle:{cycle_id}")
    return None if rec is None else CycleSettlement.from_record(rec)


def payout_outcome(state: LedgerState, cycle_id: str) -> str | None:
    rec = state.postings.get(f"payout-outcome:{cycle_id}")
    return None if rec is None else rec["outcome"]


def unresolved_payouts(state: LedgerState) -> tuple[str, ...]:
    """Cycles whose payout was posted pending and has no recorded outcome yet."""
    out = []
    for key, rec in sorted(state.postings.items()):
        if key.startswith("settle:"):
            cid = key.removeprefix("settle:")
            if not rec["payout"].is_zero() and payout_outcome(state, cid) is None:
                out.append(cid)
    return tuple(out)


def _attempt(v: Mapping[str, Any]) -> RecoveryAttempt:
    return RecoveryAttempt(
        v["attempt"], v["amount"], v["request_id"], v["outcome"], v["owner_proposal_id"]
    )


def attempts_in(state: LedgerState) -> tuple[RecoveryAttempt, ...]:
    return tuple(
        _attempt(v) for _, v in sorted(state.recoveries.items(), key=lambda kv: int(kv[0]))
    )


def recovery_attempts(store: KitStore) -> tuple[RecoveryAttempt, ...]:
    item = store.get("ledger")
    if item is None:
        return ()
    recs = item.value.get("recoveries", {})
    return tuple(_attempt(v) for _, v in sorted(recs.items(), key=lambda kv: int(kv[0])))


def charges_in_flight(state: LedgerState) -> Money | None:
    """Receivable a pending or suspended recovery charge may already be collecting, read from
    the SAME ledger item (and version) that a settlement nets against."""
    total = None
    for a in attempts_in(state):
        if a.outcome in PENDING_RECOVERY:
            total = a.amount if total is None else total + a.amount
    return total


# Writes (builds, postconditions; idempotent) -----------------------------------------------------


def post(
    store: KitStore,
    state: LedgerState,
    key: str,
    new: SellerLedger,
    payload: Mapping[str, Any] | None = None,
    recovery: RecoveryAttempt | None = None,
) -> bool:
    """Write the ledger and one posting key together (and, optionally, a recovery attempt's
    new outcome in the same write). False when the key was already posted."""
    if key in state.postings:
        return False
    postings = {**state.postings, key: dict(payload or {})}
    recoveries = dict(state.recoveries)
    if recovery is not None:
        recoveries[str(recovery.attempt)] = _with_owner(state, recovery)
    try:
        store.put(
            "ledger",
            {"ledger": new.to_record(), "postings": postings, "recoveries": recoveries},
            expected_version=state.version,
        )
    except VersionConflict:
        fresh = store.get("ledger")
        if fresh is not None and key in fresh.value["postings"]:
            return False  # a duplicate delivery got there first
        raise StateConflict("ledger changed since this posting was computed") from None
    return True


def mark_sent(
    store: KitStore,
    kind: str,
    ref: str,
    request_id: str,
    now: datetime,
    intent_hash: str,
    owner_proposal_id: str,
) -> datetime:
    """Record when money first left on request_id (idempotent). Starts the dedup window and
    keeps the original intent_hash, so a resend re-emits a fully identical Operation."""
    key = f"sent/{kind}/{ref}"
    existing = store.get(key)
    if existing is not None:
        if existing.value["request_id"] != request_id:
            raise StateConflict("a different request_id is already in flight for this money")
        return existing.value["first_sent_at"]
    store.put(
        key,
        {
            "request_id": request_id,
            "first_sent_at": now,
            "intent_hash": intent_hash,
            "owner_proposal_id": owner_proposal_id,
        },  # rule M4: the claim's owner
        expected_version=None,
    )
    return now


def first_sent_at(store: KitStore, kind: str, ref: str) -> datetime | None:
    item = store.get(f"sent/{kind}/{ref}")
    return None if item is None else item.value["first_sent_at"]


def flag_attention(store: KitStore, kind: str, ref: str, reason: str) -> None:
    """Persist that a person must act (e.g. a SUSPENDED payout), so it is visible outside the
    run that saw it. Idempotent; cleared when a definitive outcome is recorded."""
    key = f"attention/{kind}/{ref}"
    existing = store.get(key)
    value = {"kind": kind, "ref": ref, "reason": reason}
    if existing is None or existing.value != value:
        store.put(key, value, expected_version=None if existing is None else existing.version)


def clear_attention(store: KitStore, kind: str, ref: str) -> None:
    key = f"attention/{kind}/{ref}"
    existing = store.get(key)
    if existing is not None and existing.value.get("reason") != "resolved":
        store.put(key, {**existing.value, "reason": "resolved"}, expected_version=existing.version)


def needs_person(store: KitStore) -> tuple[Mapping[str, Any], ...]:
    return tuple(i.value for i in store.query("attention/") if i.value["reason"] != "resolved")


def record_resolution(store: KitStore, kind: str, ref: str, decision: Mapping[str, Any]) -> None:
    """An operator's decision on in-flight money past the window (resolve.py). Written once."""
    store.put(f"resolution/{kind}/{ref}", dict(decision), expected_version=None)


def adopt_resolution(
    store: KitStore, kind: str, ref: str, old_owner: str, new_owner: str, now: datetime
) -> None:
    """Hand a stranded decision record to a new proposal. Versioned:
    a late-resuming original and the adopter cannot both win."""
    key = f"resolution/{kind}/{ref}"
    item = store.get(key)
    if item is None or item.value["owner_proposal_id"] not in (old_owner, new_owner):
        raise StateConflict(f"{kind} {ref} is no longer owned by proposal {old_owner}")
    if item.value["owner_proposal_id"] == new_owner:
        return  # a retried adopting build (rule 0)
    value = {
        **item.value,
        "owner_proposal_id": new_owner,
        "previous_owner_proposal_id": old_owner,
        "adopted_at": now,
    }
    try:
        store.put(key, value, expected_version=item.version)
    except VersionConflict:
        raise StateConflict(f"{kind} {ref} changed while being adopted") from None


def resolution(store: KitStore, kind: str, ref: str) -> Mapping[str, Any] | None:
    item = store.get(f"resolution/{kind}/{ref}")
    return None if item is None else item.value


def put_override(store: KitStore, override: ReserveOverride) -> bool:
    key = f"override/{override.event_id}"
    existing = store.get(key)
    value = _record(override)
    if existing is not None:
        if existing.value != {**value, "order_ids": list(override.order_ids)}:
            raise StateConflict("a different override is already recorded for this event")
        return False
    store.put(key, value, expected_version=None)
    return True


def _with_owner(state: LedgerState, attempt: RecoveryAttempt) -> dict[str, Any]:
    value = _record(attempt)
    existing = state.recoveries.get(str(attempt.attempt))
    if existing is not None:  # rule M4: the claim stays the taker's
        value["owner_proposal_id"] = existing["owner_proposal_id"]
    return value


def claim_recovery(store: KitStore, state: LedgerState, attempt: RecoveryAttempt) -> None:
    """Take a NEW recovery claim in the ledger item, conditional on the version `state` was
    read at. If a settlement posted since (it may have netted this receivable), this raises
    StateConflict and nothing may be sent; the caller re-proposes."""
    key = str(attempt.attempt)
    if key in state.recoveries:
        raise StateConflict("this recovery attempt was already claimed")
    recoveries = {**state.recoveries, key: _record(attempt)}
    try:
        store.put(
            "ledger",
            {
                "ledger": state.ledger.to_record(),
                "postings": dict(state.postings),
                "recoveries": recoveries,
            },
            expected_version=state.version,
        )
    except VersionConflict:
        raise StateConflict("ledger changed before the recovery claim; propose again") from None


def put_recovery_attempt(store: KitStore, attempt: RecoveryAttempt) -> None:
    """Record an EXISTING claim's outcome (or a skip that moves no money). Moves no money and
    never changes the receivable, so it re-reads and retries on a concurrent write."""
    for _ in range(5):
        item = store.get("ledger")
        if item is None:
            raise StateConflict("no ledger for this seller")
        state = LedgerState(
            SellerLedger.from_record(item.value["ledger"]),
            dict(item.value["postings"]),
            item.version,
            dict(item.value.get("recoveries", {})),
        )
        recoveries = {**state.recoveries, str(attempt.attempt): _with_owner(state, attempt)}
        try:
            store.put(
                "ledger",
                {**item.value, "recoveries": recoveries},
                expected_version=item.version,
            )
            return
        except VersionConflict:
            continue
    raise StateConflict("ledger kept changing while recording a recovery outcome")


# Seeding (tests and the operator commands) ----------------------------------------------------


def seeded_account(store: KitStore) -> str | None:
    """The connected account seed_seller checked and bound to this seller."""
    item = store.get("account")
    return None if item is None else item.value["connected_account_id"]


def book_id(store: KitStore) -> str:
    """The seller book's id (rule M2). Missing means the book was never created: fail closed."""
    item = store.get("book")
    if item is None:
        raise StateConflict("this seller has no book id; seed the book before moving money")
    return item.value["book_id"]


def create_book(store: KitStore) -> str:
    """Mint the book id once, with the book's first state. Never a constant or default."""
    minted = uuid.uuid4().hex
    store.put("book", {"book_id": minted}, expected_version=None)  # refuses a second mint
    return minted


def seed_seller(
    store: KitStore,
    prof: SellerProfile,
    cycles: tuple[CycleData, ...] = (),
    order_book: tuple[Order, ...] = (),
    events: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    account: str | None = None,
) -> None:
    create_book(store)
    store.put("profile", _record(prof), expected_version=None)
    if account is not None:  # tests; a deploy writes it through the seed_seller command
        store.put(
            "account",
            {"connected_account_id": account, "label": f"poc-i8-{prof.seller}"},
            expected_version=None,
        )
    for c in cycles:
        put_cycle(store, c)
    if order_book:
        put_orders(store, order_book)
    for event_id, raw in (events or {}).items():
        store.put(f"evidence/{event_id}", dict(raw), expected_version=None)


def put_cycle(store: KitStore, c: CycleData) -> None:
    key = f"cycle/{c.cycle_id}"
    existing = store.get(key)
    store.put(key, _record(c), expected_version=None if existing is None else existing.version)


def put_orders(store: KitStore, order_book: tuple[Order, ...]) -> None:
    existing = store.get("orders")
    store.put(
        "orders",
        {"orders": [_record(o) for o in order_book]},
        expected_version=None if existing is None else existing.version,
    )
