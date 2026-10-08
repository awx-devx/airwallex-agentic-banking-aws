"""Kit 3's own state in the KitStore: the book id and the per-transfer replacement locks.

Book (M2). `book` holds a random id minted once, by the first build that needs it, with a
conditional create. Every request_id built from a kit-local value (the supplier payment's
`payment_key`) includes it, so a fresh book on the shared sandbox never adopts an earlier
run's transfers. The replacement's request_id is keyed on the original's Airwallex transfer id,
an Airwallex object id, so it is deliberately book-independent: whichever book or store sees
the incident, one original always maps to the same replacement request_ids and the provider's
deduplication and the `?request_id=` check still catch a second replacement.

Lock (M1, M4). `lock:<original transfer id>` records the replacement attempt in flight:

    owner        proposal_id of the run that took it (StepContext.proposal_id), never intent_hash
    attempt      1, 2 or 3; request_id is replacement_request_id(original, attempt)
    state        SENDING      taken by the build, send not yet settled
                 IN_FLIGHT    the send ended OUTCOME_UNKNOWN or was never shown to land: the
                              money stays on this request_id (M1)
                 LANDED       a replacement exists under this request_id
                 REPLACEMENT_FAILED  the replacement itself ended CANCELLED: a person decides
                 RELEASED     the attempt was definitively refused at its first send, or
                              nothing was sent: the next attempt may use attempt + 1 (M1)
    taken_at     when the attempt was first sent; the ~6-day margin is measured from here
    joined_by    proposal_ids of other runs that resumed this attempt and may have sent on it.
                 A run joins with a versioned write BEFORE it emits, so the owner's own
                 evidence ("my send was refused") never covers a joiner's send: the owner may
                 release only while joined_by is empty, and a join racing the release loses
                 on the version.

Only the owner's postcondition releases a lock. Any later run that has READ a definitive
outcome (the replacement listed under the request_id, or its CANCELLED failure state) may
mark it LANDED or REPLACEMENT_FAILED. on_abandon never touches a lock.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from awx_common.ops import DEDUP_RESEND_MARGIN
from awx_common.store import KitStore, StoredItem, VersionConflict

BOOK_KEY = "book"
LOCK_PREFIX = "lock:"
SENT_PREFIX = "sent:"
MAX_REPLACEMENT_ATTEMPTS = 3
# Airwallex deduplicates a request_id for about 7 days (guide; the fake models it). Past this
# margin a resend could pay twice, so in-flight money goes to a person and nothing is sent.
# The spine's own margin (DEDUP_RESEND_MARGIN, 6 days): the executor never resends past it,
# so kit and spine agree.
DEDUPE_MARGIN = DEDUP_RESEND_MARGIN

SENDING = "SENDING"
IN_FLIGHT = "IN_FLIGHT"
LANDED = "LANDED"
REPLACEMENT_FAILED = "REPLACEMENT_FAILED"
RELEASED = "RELEASED"
LIVE_STATES = frozenset({SENDING, IN_FLIGHT})


class LedgerConflict(Exception):
    """A lock or book write lost a race, or the stored state is not what this run expects."""


# ---- book ---------------------------------------------------------------------------------


def book_id(store: KitStore | None) -> str | None:
    if store is None:
        return None
    item = store.get(BOOK_KEY)
    value = None if item is None else item.value.get("book_id")
    return value if isinstance(value, str) and len(value) == 16 else None


def ensure_book(store: KitStore, now: datetime) -> str:
    """The book id, minting it on first use. Idempotent; exactly one concurrent create wins
    (KitStore.put(expected_version=None) is a conditional create in every store)."""
    existing = book_id(store)
    if existing is not None:
        return existing
    try:
        store.put(
            BOOK_KEY, {"book_id": secrets.token_hex(8), "created_at": now}, expected_version=None
        )
    except VersionConflict:
        pass
    minted = book_id(store)
    if minted is None:
        raise LedgerConflict("book record exists without a valid book_id")
    return minted


# ---- replacement lock ---------------------------------------------------------------------


@dataclass(frozen=True)
class Lock:
    original_transfer_id: str
    owner: str
    attempt: int
    request_id: str
    state: str
    taken_at: datetime
    updated_at: datetime
    note: str = ""
    joined_by: tuple[str, ...] = ()
    version: int = 0

    def record(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("version")
        out["joined_by"] = list(self.joined_by)
        return out

    def age(self, now: datetime) -> timedelta:
        return now - self.taken_at

    def past_margin(self, now: datetime) -> bool:
        return self.age(now) >= DEDUPE_MARGIN


def lock_key(original_transfer_id: str) -> str:
    return f"{LOCK_PREFIX}{original_transfer_id}"


def _from(item: StoredItem) -> Lock:
    v: Mapping[str, Any] = item.value
    return Lock(
        original_transfer_id=str(v["original_transfer_id"]),
        owner=str(v["owner"]),
        attempt=int(v["attempt"]),
        request_id=str(v["request_id"]),
        state=str(v["state"]),
        taken_at=v["taken_at"],
        updated_at=v["updated_at"],
        note=str(v.get("note", "")),
        joined_by=tuple(str(j) for j in v.get("joined_by") or ()),
        version=item.version,
    )


def get_lock(store: KitStore | None, original_transfer_id: str) -> Lock | None:
    if store is None:
        return None
    item = store.get(lock_key(original_transfer_id))
    return None if item is None else _from(item)


def take_lock(
    store: KitStore,
    *,
    original_transfer_id: str,
    owner: str,
    attempt: int,
    request_id: str,
    now: datetime,
    over: Lock | None,
) -> Lock:
    """Take the lock for a new attempt: a conditional create, or a versioned overwrite of a
    RELEASED lock. Losing the race raises LedgerConflict, so the build sends nothing."""
    if over is not None and over.state != RELEASED:
        raise LedgerConflict(f"lock is {over.state}, not RELEASED")
    lock = Lock(original_transfer_id, owner, attempt, request_id, SENDING, now, now)
    try:
        item = store.put(
            lock_key(original_transfer_id),
            lock.record(),
            expected_version=None if over is None else over.version,
        )
    except VersionConflict as exc:
        raise LedgerConflict("another run took the replacement lock first") from exc
    return replace(lock, version=item.version)


def join_lock(store: KitStore, lock: Lock, joiner: str, now: datetime) -> Lock:
    """Record that `joiner` may send on this live attempt. Versioned: a lost race (the owner
    released, or another run moved the lock) raises LedgerConflict and the joiner sends
    nothing. Idempotent for a joiner already recorded."""
    if lock.state not in LIVE_STATES:
        raise LedgerConflict(f"lock is {lock.state}, not live")
    if joiner == lock.owner or joiner in lock.joined_by:
        return lock
    joined = replace(lock, joined_by=(*lock.joined_by, joiner), updated_at=now)
    try:
        item = store.put(
            lock_key(lock.original_transfer_id), joined.record(), expected_version=lock.version
        )
    except VersionConflict as exc:
        raise LedgerConflict("the replacement lock changed before this run could join") from exc
    return replace(joined, version=item.version)


def move_lock(store: KitStore, lock: Lock, state: str, now: datetime, note: str) -> Lock:
    """Versioned state change. A lost race is reported, never retried blindly."""
    if state == RELEASED and lock.joined_by:
        raise LedgerConflict("another run joined this attempt; it may have sent: never release")
    moved = replace(lock, state=state, updated_at=now, note=note[:300])
    try:
        item = store.put(
            lock_key(lock.original_transfer_id), moved.record(), expected_version=lock.version
        )
    except VersionConflict as exc:
        raise LedgerConflict("the replacement lock changed under this run") from exc
    return replace(moved, version=item.version)


# ---- supplier payment claim (the original transfer) ---------------------------------------


def sent_key(payment_key: str) -> str:
    return f"{SENT_PREFIX}{payment_key}"


def sent_record(store: KitStore | None, payment_key: str) -> Mapping[str, Any] | None:
    if store is None:
        return None
    item = store.get(sent_key(payment_key))
    return None if item is None else item.value


def claim_send(
    store: KitStore, *, payment_key: str, request_id: str, owner: str, now: datetime
) -> None:
    """Record the first send of a payment. A redelivered or resumed build finds the record and
    re-emits the same request_id; the record's sent_at drives the ~6-day margin."""
    try:
        store.put(
            sent_key(payment_key),
            {"request_id": request_id, "owner": owner, "sent_at": now},
            expected_version=None,
        )
    except VersionConflict:
        existing = sent_record(store, payment_key)
        if existing is None or existing.get("request_id") != request_id:
            raise LedgerConflict("payment_key is recorded under another request_id") from None
