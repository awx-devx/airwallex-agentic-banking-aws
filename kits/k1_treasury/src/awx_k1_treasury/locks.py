"""Kit 1 state in PolicyContext.store: obligation locks, quote burn and submission records.

Obligation lock, key obligation/<id> (one per obligation, across book versions; the value
records the version and attempt being paid). Taken by the first step of a run, before any
money moves, and owned by the run's proposal_id (M4). Policy BLOCKs an obligation whose lock is
LOCKED, SUBMITTED, PAID or NEEDS_PERSON, so a second proposal for an obligation that is paid
or in flight cannot pay it again, whatever evidence it cites. The postcondition moves the lock
on: PAID or SUBMITTED when the transfer was accepted, NEEDS_PERSON when money moved but the
obligation is not paid (or the outcome is unknown), and RELEASED only when nothing landed. A
RELEASED lock is retaken with attempt + 1, and the attempt is part of the request_id step, so
a retry never reuses a request_id whose earlier body the spine's operation store has seen.

Quote burn, key quote/<quote_id>. The sandbox accepts a quote again under a new request_id
(FAKE-FIDELITY.md, usage MULTI_USE), so one quote books at most one conversion request.

Submission record, key submission/<request_id>. The exact body (and quote) sent under a
request_id, written before it is emitted, so a retried step resends the identical body and
re-runs its checks against it. It is never trusted on its own: the step reconciles with the
provider first (rule 9) and re-checks reserves before any resend.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from awx_common import canonical_json
from awx_common.store import KitStore, VersionConflict

from .money_rules import K1Error


class LockState(StrEnum):
    LOCKED = "LOCKED"  # a run owns the obligation; money may be moving
    SUBMITTED = "SUBMITTED"  # transfer accepted by the provider, not yet PAID
    PAID = "PAID"
    NEEDS_PERSON = "NEEDS_PERSON"  # money moved (or may have) but the obligation is unpaid
    RELEASED = "RELEASED"  # nothing landed; the obligation may be proposed again


BLOCKING = frozenset(
    {LockState.LOCKED, LockState.SUBMITTED, LockState.PAID, LockState.NEEDS_PERSON}
)


class ObligationLocked(K1Error):
    """Another run owns this obligation, or it is already paid or needs a person."""


class QuoteAlreadyBurned(K1Error):
    """The quote was already booked by another conversion request. Get a fresh quote."""


class SubmissionConflict(K1Error):
    """The same request_id was recorded earlier with a different body."""


class NoStore(K1Error):
    """Kit 1 needs PolicyContext.store."""


def need_store(store: KitStore | None) -> KitStore:
    if store is None:
        raise NoStore("kit 1 needs PolicyContext.store for its book, locks and quote burn")
    return store


def lock_key(obligation_id: str) -> str:
    """One lock per obligation, whatever its book version."""
    return f"obligation/{obligation_id}"


@dataclass(frozen=True)
class Lock:
    state: LockState
    owner: str  # the proposal_id of the run that took it (M4), never an intent hash
    attempt: int
    obligation_version: str  # the book version this attempt pays
    version: int  # store version, for optimistic writes
    note: str = ""
    submitted_at: datetime | None = None  # first time this attempt was recorded SUBMITTED
    locked_at: datetime | None = None  # when this attempt was taken
    claimed_at: datetime | None = None  # last emission claim (claim_for_emission)
    claims: int = 0  # emission claims made under this attempt
    conversion_rids: tuple[str, ...] = ()  # conversions claimed for this attempt
    # Every request_id an emission claim covered under this attempt. A request_id recorded
    # under the attempt but absent here was never returned by its build, so never dispatched.
    claimed_rids: tuple[str, ...] = ()

    def extra(self) -> dict[str, Any]:
        return {
            "locked_at": self.locked_at,
            "claimed_at": self.claimed_at,
            "claims": self.claims,
            "conversion_rids": list(self.conversion_rids),
            "claimed_rids": list(self.claimed_rids),
        }


def read_lock(store: KitStore, obligation_id: str) -> Lock | None:
    item = store.get(lock_key(obligation_id))
    if item is None:
        return None
    v = item.value
    return Lock(
        LockState(v["state"]),
        v["owner"],
        int(v["attempt"]),
        str(v["obligation_version"]),
        item.version,
        v.get("note", ""),
        v.get("submitted_at"),
        v.get("locked_at"),
        v.get("claimed_at"),
        int(v.get("claims", 0)),
        tuple(v.get("conversion_rids", ())),
        tuple(v.get("claimed_rids", ())),
    )


def _value(
    state: LockState,
    owner: str,
    attempt: int,
    ob_version: str,
    note: str = "",
    submitted_at: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "state": state.value,
        "owner": owner,
        "attempt": attempt,
        "obligation_version": ob_version,
        "note": note,
        "submitted_at": submitted_at,
        **extra,
    }


@dataclass(frozen=True)
class Acquired:
    attempt: int
    previous: Lock | None  # what was there before this call, if this call took the lock
    fresh: bool  # True when this call took it (not a re-entry)


def acquire(
    store: KitStore,
    obligation_id: str,
    ob_version: str,
    owner: str,
    now: datetime | None = None,
    *,
    refuse_own_release: bool = False,
) -> Acquired:
    """Take or re-enter the obligation's lock for `owner` (a proposal_id).

    A new owner, or a new book version, may take the lock only when it is absent or RELEASED.
    The attempt rises only on a retake from RELEASED, which only a definitive outcome sets
    (M1). The same owner re-enters at the same attempt and must name the same book version.
    """
    key = lock_key(obligation_id)
    for _ in range(3):
        lock = read_lock(store, obligation_id)
        try:
            if lock is None:
                store.put(
                    key,
                    _value(LockState.LOCKED, owner, 1, ob_version, locked_at=now),
                    expected_version=None,
                )
                return Acquired(1, None, True)
            if lock.state is LockState.RELEASED:
                if refuse_own_release and lock.owner == owner:
                    raise ObligationLocked(
                        f"{obligation_id} was released while this run held it; not retaken"
                    )
                nxt = lock.attempt + 1
                store.put(
                    key,
                    _value(LockState.LOCKED, owner, nxt, ob_version, locked_at=now),
                    expected_version=lock.version,
                )
                return Acquired(nxt, lock, True)
        except VersionConflict:
            continue  # someone wrote between our read and write; read again
        if lock.owner == owner and lock.obligation_version == ob_version:
            return Acquired(lock.attempt, None, False)
        raise ObligationLocked(
            f"{obligation_id} is {lock.state.value} (v{lock.obligation_version}, "
            f"attempt {lock.attempt}) under another run"
        )
    raise ObligationLocked(f"{obligation_id}: lock contended")


def reenter(store: KitStore, obligation_id: str, ob_version: str, owner: str, attempt: int) -> int:
    """A later step of a run only re-enters the lock it took, never acquires.
    Raises ObligationLocked unless the lock is LOCKED, owned by this run's proposal_id, for the
    same book version and at the attempt the run took. A person's release (RELEASED) or anyone
    else's retake therefore fails the run closed instead of being silently undone."""
    lock = read_lock(store, obligation_id)
    if (
        lock is None
        or lock.owner != owner
        or lock.state is not LockState.LOCKED
        or lock.obligation_version != ob_version
        or lock.attempt != attempt
    ):
        state = lock.state.value if lock else "absent"
        raise ObligationLocked(
            f"{obligation_id} is no longer this run's (now {state}); nothing more is sent"
        )
    return lock.attempt


def claim_for_emission(
    store: KitStore,
    obligation_id: str,
    owner: str,
    attempt: int,
    now: datetime,
    *,
    conversion_rids: tuple[str, ...] = (),
    request_ids: tuple[str, ...] = (),
) -> None:
    """A compare-and-swap write on the obligation lock, made before a build
    records or returns anything that moves money. operator_set is a CAS on the same key, so a
    person's release and an emission conflict and exactly one wins: a release that lands first
    makes this raise ObligationLocked; a claim that lands first moves the store version the
    release approval bound, so operator_set raises LockMoved."""
    for _ in range(3):
        lock = read_lock(store, obligation_id)
        if (
            lock is None
            or lock.owner != owner
            or lock.state is not LockState.LOCKED
            or lock.attempt != attempt
        ):
            state = lock.state.value if lock else "absent"
            raise ObligationLocked(f"{obligation_id} is {state}; this run may not emit")
        extra = lock.extra()
        extra["claimed_at"] = now
        extra["claims"] = lock.claims + 1
        extra["conversion_rids"] = sorted(set(lock.conversion_rids) | set(conversion_rids))
        extra["claimed_rids"] = sorted(
            set(lock.claimed_rids) | set(conversion_rids) | set(request_ids)
        )
        value = _value(
            lock.state,
            lock.owner,
            lock.attempt,
            lock.obligation_version,
            lock.note,
            lock.submitted_at,
            **extra,
        )
        try:
            store.put(lock_key(obligation_id), value, expected_version=lock.version)
            return
        except VersionConflict:
            continue
    raise ObligationLocked(f"{obligation_id}: claim contended")


def record_run_attempts(
    store: KitStore, proposal_id: str, attempts: Mapping[str, int]
) -> dict[str, int]:
    """The attempts a run took, written once (conditional create). A redelivered first step
    finds them and re-enters instead of acquiring again."""
    key = f"run/{proposal_id}"
    try:
        store.put(key, {"attempts": dict(attempts)}, expected_version=None)
        return dict(attempts)
    except VersionConflict:
        item = store.get(key)
        assert item is not None
        return {k: int(v) for k, v in item.value["attempts"].items()}


def run_attempts(store: KitStore, proposal_id: str) -> dict[str, int] | None:
    item = store.get(f"run/{proposal_id}")
    return {k: int(v) for k, v in item.value["attempts"].items()} if item else None


def undo_acquire(store: KitStore, obligation_id: str, owner: str, taken: Acquired) -> None:
    """Undo a lock this same build call just took, before it emitted anything (the next
    lock in the set was held by another run). Restores RELEASED at the previous attempt, so
    the never-used attempt and its request_ids are not consumed. Not a release of money in
    flight: nothing was built under the lock."""
    if not taken.fresh:
        return
    lock = read_lock(store, obligation_id)
    if lock is None or lock.owner != owner or lock.state is not LockState.LOCKED:
        return
    prev = taken.previous
    attempt = prev.attempt if prev else 0
    ob_version = prev.obligation_version if prev else lock.obligation_version
    value = _value(LockState.RELEASED, owner, attempt, ob_version, "undone: sibling locked")
    store.put(lock_key(obligation_id), value, expected_version=lock.version)


def set_state(
    store: KitStore,
    obligation_id: str,
    owner: str,
    state: LockState,
    note: str = "",
    *,
    only_from: frozenset[LockState] | None = None,
    now: datetime | None = None,
) -> bool:
    """Move a lock this owner holds. Never downgrades PAID. `only_from` limits the source
    states. Moving to SUBMITTED records submitted_at once (from `now`). Returns whether it
    wrote."""
    lock = read_lock(store, obligation_id)
    if lock is None or lock.owner != owner or lock.state is LockState.PAID:
        return False
    if only_from is not None and lock.state not in only_from:
        return False
    submitted_at = lock.submitted_at
    if state is LockState.SUBMITTED and submitted_at is None:
        submitted_at = now
    if lock.state is state and lock.note == note and lock.submitted_at == submitted_at:
        return False
    value = _value(
        state, owner, lock.attempt, lock.obligation_version, note, submitted_at, **lock.extra()
    )
    store.put(lock_key(obligation_id), value, expected_version=lock.version)
    return True


def burn_quote(store: KitStore, quote_id: str, request_id: str) -> None:
    key = f"quote/{quote_id}"
    try:
        store.put(key, {"request_id": request_id}, expected_version=None)
    except VersionConflict:
        item = store.get(key)
        if item is None or item.value.get("request_id") != request_id:
            raise QuoteAlreadyBurned(f"quote {quote_id} was already booked") from None


def quote_owner(store: KitStore, quote_id: str) -> str | None:
    item = store.get(f"quote/{quote_id}")
    return item.value["request_id"] if item else None


def record_submission(
    store: KitStore, request_id: str, kind: str, body: Mapping[str, Any], **extra: Any
) -> Mapping[str, Any]:
    """Record the body sent under a request_id. Same body again is a no-op; another refuses."""
    key = f"submission/{request_id}"
    value = {"kind": kind, "body": dict(body), **extra}
    try:
        store.put(key, value, expected_version=None)
        return value
    except VersionConflict:
        item = store.get(key)
        if item is None or canonical_json(item.value["body"]) != canonical_json(body):
            raise SubmissionConflict(
                f"request_id {request_id} recorded with another body"
            ) from None
        return item.value


def get_submission(store: KitStore, request_id: str) -> Mapping[str, Any] | None:
    item = store.get(f"submission/{request_id}")
    return item.value if item else None


# Lock note the postcondition writes when this run's own first send of the transfer was refused
# at create (a definitive refusal, M1): the release tool may then release without waiting out
# the dedup window.
NOTE_REFUSED = "transfer refused at create"


class LockMoved(K1Error):
    """The lock changed after the release proposal read it. Re-propose."""


def operator_set(
    store: KitStore,
    obligation_id: str,
    *,
    expected_store_version: int,
    state: LockState,
    note: str,
) -> Lock:
    """The release tool's only write (release.py): move a lock that a PERSON decided about,
    whoever owns it, but only if it is still exactly the lock the approved proposal saw
    (store version bound in computed). Keeps the attempt, so a RELEASED lock retaken later
    gets attempt + 1 and new request_ids (M1)."""
    lock = read_lock(store, obligation_id)
    if lock is None or lock.version != expected_store_version:
        raise LockMoved(f"{obligation_id} changed since the release was proposed")
    if lock.state is LockState.PAID:
        raise LockMoved(f"{obligation_id} is PAID; nothing to release")
    value = _value(
        state,
        lock.owner,
        lock.attempt,
        lock.obligation_version,
        note,
        lock.submitted_at,
        **lock.extra(),
    )
    try:  # CAS: a claim_for_emission that landed after the proposal makes this lose
        store.put(lock_key(obligation_id), value, expected_version=lock.version)
    except VersionConflict:
        raise LockMoved(f"{obligation_id} changed since the release was proposed") from None
    found = read_lock(store, obligation_id)
    assert found is not None
    return found


# --- rate locks (ratelock.py) ---------------------------------------------------------------


def ratelock_key(currency: str) -> str:
    return f"ratelock/{currency}"


def read_ratelock(store: KitStore | None, currency: str) -> Mapping[str, Any] | None:
    if store is None:
        return None
    item = store.get(ratelock_key(currency))
    return item.value if item else None


def write_ratelock(store: KitStore, currency: str, value: Mapping[str, Any]) -> None:
    """Latest lock wins: a re-lock replaces the stored quote for that currency."""
    item = store.get(ratelock_key(currency))
    store.put(ratelock_key(currency), dict(value), expected_version=item.version if item else None)


def emission_prefix(obligation_id: str, attempt: int) -> str:
    return f"emitted/{obligation_id}/a{attempt}/"


def index_emission(
    store: KitStore, obligation_id: str, attempt: int, request_id: str, kind: str
) -> None:
    """Per-obligation index of every submission recorded under an attempt, so
    the release tool sees a recorded conversion as well as a transfer. Idempotent."""
    try:
        store.put(
            emission_prefix(obligation_id, attempt) + request_id,
            {"kind": kind, "request_id": request_id},
            expected_version=None,
        )
    except VersionConflict:
        pass


def emissions(store: KitStore, obligation_id: str, attempt: int) -> list[Mapping[str, Any]]:
    return [i.value for i in store.query(emission_prefix(obligation_id, attempt))]
