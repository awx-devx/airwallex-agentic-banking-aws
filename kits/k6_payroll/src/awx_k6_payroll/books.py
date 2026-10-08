"""Kit 6's book: the minted id that scopes every request_id (rule M2), the attempt counters
that advance only after a definitive failure (rule M1), and the record of what each
conversion attempt sent, so a resend after an unknown outcome is the same Operation.

All of it lives in ctx.platform_store: the parent, every employer child and the read
tools all see one book. Policy and read tools only read it; step builds and the postcondition
write it with versions.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from awx_common.store import KitStore, VersionConflict

from . import ids

BOOK_KEY = "book"


def read_book(store: KitStore | None) -> str | None:
    """The book id, or None when kit 6 has no state yet (so nothing has been sent)."""
    if store is None:
        raise RuntimeError("kit 6 needs ctx.platform_store")
    item = store.get(BOOK_KEY)
    return None if item is None else str(item.value["book_id"])


def ensure_book(store: KitStore | None) -> str:
    """Mint the book once, when kit 6's state is first created. Never a constant."""
    found = read_book(store)
    if found is not None:
        return found
    assert store is not None
    try:
        store.put(BOOK_KEY, {"book_id": uuid.uuid4().hex}, expected_version=None)
    except VersionConflict:
        pass  # another employer's run minted it first; use theirs
    book = read_book(store)
    assert book is not None
    return book


def _attempt_key(money_key: str) -> str:
    return f"attempts/{money_key}"


def attempt(store: KitStore | None, money_key: str) -> int:
    """1 + the number of definitively failed request_ids recorded for this money."""
    if store is None:
        raise RuntimeError("kit 6 needs ctx.platform_store")
    item = store.get(_attempt_key(money_key))
    return 1 + (len(item.value["failed"]) if item is not None else 0)


def record_failure(store: KitStore, money_key: str, request_id: str, why: str) -> None:
    """Idempotent: a request_id is counted once, however often the postcondition runs."""
    for _ in range(5):
        item = store.get(_attempt_key(money_key))
        failed = dict(item.value["failed"]) if item is not None else {}
        if request_id in failed:
            return
        failed[request_id] = why
        try:
            store.put(
                _attempt_key(money_key),
                {"failed": failed},
                expected_version=None if item is None else item.version,
            )
            return
        except VersionConflict:
            continue
    raise VersionConflict(f"could not record the failure of {request_id}")


def _sent_key(request_id: str) -> str:
    return f"sent/{request_id}"


def sent(store: KitStore, request_id: str) -> Mapping[str, Any] | None:
    """What a money operation carried when first built, and when (`first_sent_at`)."""
    item = store.get(_sent_key(request_id))
    return None if item is None else item.value


def record_sent(
    store: KitStore,
    request_id: str,
    now: datetime,
    run_token: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Before the first send of a money operation (conditional create, so a re-emit keeps the
    original time): rule M1's deduplication-window clock starts here."""
    if store.get(_sent_key(request_id)) is not None:
        return
    try:
        store.put(
            _sent_key(request_id),
            {"first_sent_at": now, "first_sent_by": run_token, **dict(extra or {})},
            expected_version=None,
        )
    except VersionConflict:
        pass


def _person_prefix(book: str, employer: str, period: str) -> str:
    return f"needs-person/{book}:{employer}:{period}/"


def record_needs_person(store: KitStore, k: Keys, request_id: str, why: str) -> None:
    key = _person_prefix(k.book, k.employer, k.period) + request_id
    if store.get(key) is None:
        try:
            store.put(key, {"request_id": request_id, "why": why}, expected_version=None)
        except VersionConflict:
            pass


def needs_person(store: KitStore | None, k: Keys | None) -> list[Mapping[str, Any]]:
    if store is None or k is None:
        return []
    return [i.value for i in store.query(_person_prefix(k.book, k.employer, k.period))]


@dataclass(frozen=True)
class Keys:
    """Request ids for one employer and pay period in one book."""

    book: str
    store: KitStore
    employer: str
    period: str

    def payout_key(self, contractor: str) -> str:
        return ids.payout_key(self.book, self.employer, contractor, self.period)

    def payout_rid(self, contractor: str) -> str:
        n = attempt(self.store, self.payout_key(contractor))
        return ids.payout_request_id(self.book, self.employer, contractor, self.period, n)

    def conversion_key(self, buy: str, sell: str) -> str:
        return ids.conversion_key(self.book, self.employer, self.period, buy, sell)

    def conversion_rid(self, buy: str, sell: str) -> str:
        n = attempt(self.store, self.conversion_key(buy, sell))
        return ids.conversion_request_id(self.book, self.employer, self.period, buy, sell, n)

    def fee_key(self) -> str:
        return ids.fee_key(self.book, self.employer, self.period)

    def fee_rid(self) -> str:
        n = attempt(self.store, self.fee_key())
        return ids.fee_request_id(self.book, self.employer, self.period, n)

    def nickname(self, contractor: str) -> str:
        return ids.beneficiary_nickname(self.book, self.employer, contractor)


# ---------------------------------------------------------------- rate locks (rule M3)


def _lock_key(k: Keys, ccy: str) -> str:
    return f"lock/{k.book}:{k.employer}:{k.period}/{ccy}"


def read_lock(store: KitStore | None, k: Keys | None, ccy: str) -> Mapping[str, Any] | None:
    """The stored FX quote locked for one conversion: quote_id, buy, sell, valid_to_at and
    the conversion request_id (attempt) it was locked for."""
    if store is None or k is None:
        return None
    item = store.get(_lock_key(k, ccy))
    return None if item is None else item.value


def write_lock(
    store: KitStore, k: Keys, ccy: str, value: Mapping[str, Any], *, keep_until: datetime
) -> bool:
    """Store a lock. An existing lock for the same conversion attempt that is still valid past
    `keep_until` is kept (returns False), so the AUTO lock action can never re-quote under a
    payroll approval that binds the current lock."""
    for _ in range(5):
        item = store.get(_lock_key(k, ccy))
        if (
            item is not None
            and item.value.get("for_request_id") == value.get("for_request_id")
            and item.value["valid_to_at"] > keep_until
        ):
            return False
        try:
            store.put(
                _lock_key(k, ccy), value, expected_version=None if item is None else item.version
            )
            return True
        except VersionConflict:
            continue
    raise VersionConflict(f"could not store the rate lock for {ccy}")


# ---------------------------------------------------------------- run claims


def _claim_key(k: Keys) -> str:
    return f"claim/{k.book}:{k.employer}:{k.period}"


def read_claim(store: KitStore | None, k: Keys | None) -> Mapping[str, Any] | None:
    """The run that currently owns this employer and pay period, or None (rule M4)."""
    if store is None or k is None:
        return None
    item = store.get(_claim_key(k))
    if item is None or item.value.get("owner") is None:
        return None
    return item.value


def take_claim(
    store: KitStore,
    k: Keys,
    proposal_id: str,
    now: datetime,
    *,
    root_proposal_id: str | None = None,
    root_action: str | None = None,
) -> str | None:
    """Conditional take, owned by proposal_id. Returns None when taken (or already ours),
    else the other owner's proposal_id."""
    for _ in range(5):
        item = store.get(_claim_key(k))
        owner = None if item is None else item.value.get("owner")
        if owner == proposal_id:
            return None
        if owner is not None:
            return str(owner)
        try:
            store.put(
                _claim_key(k),
                {
                    "owner": proposal_id,
                    "taken_at": now,
                    # The root run: what a person's release guards with run_status.
                    "root_proposal_id": root_proposal_id,
                    "root_action": root_action,
                },
                expected_version=None if item is None else item.version,
            )
            return None
        except VersionConflict:
            continue
    raise VersionConflict("could not take the run claim")


def release_claim(store: KitStore, k: Keys, proposal_id: str) -> None:
    """Only the owner releases its claim (rule M4)."""
    item = store.get(_claim_key(k))
    if item is None or item.value.get("owner") != proposal_id:
        return
    store.put(item.key, {"owner": None, "released_by": proposal_id}, expected_version=item.version)


class ClaimMoved(Exception):
    """The claim is not the one the approval names; nothing released."""


def force_release_claim(
    store: KitStore, k: Keys, *, owner: str, taken_at: datetime, by: str
) -> None:
    """A person-approved release (release.py): only the exact claim the approval names."""
    item = store.get(_claim_key(k))
    if item is None or item.value.get("owner") is None:
        return  # already released (a redelivered build)
    if item.value.get("owner") != owner or item.value.get("taken_at") != taken_at:
        raise ClaimMoved("the run claim changed since the release was approved")
    store.put(
        item.key,
        {"owner": None, "released_by": by, "forced_from": owner},
        expected_version=item.version,
    )
