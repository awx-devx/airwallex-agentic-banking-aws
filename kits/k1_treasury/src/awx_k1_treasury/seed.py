"""Operator seeding of kit 1's treasury book.

A deployed kit 1 starts with an empty kit store (the platform tenant's ledger partition), so
every read tool answers NOT_CONFIGURED and every proposal BLOCKS until a person seeds the book.
`seed_deployed` writes the book and mints the M2 book id (`book.seed_book`) from REAL Airwallex
beneficiary ids: the demo book's `ben-poc-k1-*` ids are placeholders and are refused. Only the
obligations given a beneficiary are kept, so nothing can be paid to a beneficiary that does not
exist.

The operator command `seed_treasury_book` (commands.py) calls it; `awx-deploy apply` runs that
command from `deploy/k1.yaml`, and `kits/k1_treasury/scripts/seed_deployed_book.py` is a
stand-alone wrapper around the same command.
This module imports only awx_common and kit 1, so it is testable without AWS.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from awx_common import ReadRequest
from awx_common.store import KitStore, VersionConflict

from .book import BOOK_KEY, DEMO_BOOK, TreasuryBook, load_book, load_book_id, seed_book
from .locks import LockState, lock_key, read_lock

_FINAL = (LockState.PAID, LockState.RELEASED)

PLACEHOLDER_PREFIX = "ben-poc-k1-"
LABEL_PREFIX = "poc-i1-"


class SeedRefused(ValueError):
    pass


def build_seed_book(
    beneficiaries: Mapping[str, str],
    *,
    template: TreasuryBook = DEMO_BOOK,
    book_version: str,
) -> TreasuryBook:
    """The template book with each named obligation pointed at its real beneficiary."""
    if not beneficiaries:
        raise SeedRefused("name at least one obligation=beneficiary_id")
    unknown = sorted(set(beneficiaries) - {o.obligation_id for o in template.obligations})
    if unknown:
        raise SeedRefused(f"no such obligations in the template: {unknown}")
    for oid, ben in beneficiaries.items():
        if not ben or ben.startswith(PLACEHOLDER_PREFIX):
            raise SeedRefused(f"{oid}: {ben!r} is a placeholder, not a real beneficiary id")
    obligations = tuple(
        replace(o, beneficiary_id=beneficiaries[o.obligation_id])
        for o in template.obligations
        if o.obligation_id in beneficiaries
    )
    return replace(template, book_version=book_version, obligations=obligations)


def seed_deployed(
    store: KitStore,
    beneficiaries: Mapping[str, str],
    *,
    book_version: str,
    read: Callable[[ReadRequest], Mapping[str, Any]] | None = None,
    template: TreasuryBook = DEMO_BOOK,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Write the book into `store` (deployed: DynamoDbKitStore for kit k1, tenant platform).

    With `read` (an Airwallex GET), each beneficiary must exist and carry a `poc-i1-` nickname.
    An existing book is kept unless `replace_existing` (use a new book_version); the book id is
    minted once and never changes on a replace (M2)."""
    # Validate everything, then write. The book's store version is read once here and the
    # write is a CAS against it, so a seed that lands while this one validates is refused,
    # never overwritten. The book id is minted only after
    # every check has passed (M2: once, kept by a replace).
    checked = store.get(BOOK_KEY)
    if checked is not None and not replace_existing:
        raise SeedRefused("a treasury book is already seeded; pass replace_existing to reseed")
    before = load_book(store)
    book = build_seed_book(beneficiaries, template=template, book_version=book_version)
    if read is not None:
        for oid, ben in sorted(beneficiaries.items()):
            try:
                body = read(ReadRequest(f"/api/v1/beneficiaries/{ben}"))
            except Exception as exc:  # any failure (provider, transport, bug) is a refusal
                raise SeedRefused(
                    f"{oid}: beneficiary {ben} could not be read ({type(exc).__name__}: {exc})"
                ) from None
            if not isinstance(body, Mapping):
                raise SeedRefused(f"{oid}: beneficiary {ben} read returned no object")
            nick = str(body.get("nickname") or "")
            if not nick.startswith(LABEL_PREFIX):
                raise SeedRefused(f"{oid}: beneficiary {ben} is not a {LABEL_PREFIX} beneficiary")
    try:
        seed_book(store, book, expected_version=checked.version if checked else None)
    except VersionConflict:
        raise SeedRefused("another seed landed first; re-run with replace if intended") from None
    # Report every obligation/ lock in the store that is mid-flight, whichever book it came
    # from, and which of those this replace drops.
    prefix = lock_key("")
    open_locks: dict[str, str] = {}
    for item in sorted(store.query(prefix), key=lambda i: i.key):
        oid = item.key[len(prefix) :]
        lock = read_lock(store, oid)
        if lock is not None and lock.state not in _FINAL:
            open_locks[oid] = lock.state.value
    kept = {o.obligation_id for o in book.obligations}
    was = {o.obligation_id for o in before.obligations} if before is not None else set()
    dropped = [
        f"{oid} has a {state} lock and is no longer in the book; its release needs it in the "
        "book or a recorded submission"
        for oid, state in open_locks.items()
        if oid in was and oid not in kept
    ]
    return {
        "book_id": load_book_id(store),
        "book_version": book.book_version,
        "obligations": {o.obligation_id: o.beneficiary_id for o in book.obligations},
        "open_locks": open_locks,
        "dropped_with_open_lock": dropped,
    }
