"""The treasury book: obligations, receipt forecasts and reserve floors.

The book is kit-owned state in `PolicyContext.store` under BOOK_KEY (the DynamoDB kit store
when deployed). The model never supplies an amount, a beneficiary or a floor; it can only name
obligation and receipt IDs that already exist here. Each obligation carries a `version`, the
operator's label for its terms. request_ids derive from the book id, the obligation and its
lock attempt (see keys.py), never from the version, and the obligation lock stops a paid
obligation from being paid again.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

from awx_common import Money
from awx_common.store import KitStore, VersionConflict

BOOK_KEY = "book/current"
BOOK_ID_KEY = "book/id"
# What every tool says on an empty kit store (a deployed kit 1 starts with none): seed it with
# the operator command `seed_treasury_book` (README, "Setting up a deployed kit").
NO_BOOK = "no treasury book seeded; run the kit 1 seed step (README, Deploy)"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class Priority(IntEnum):
    """Lower value is funded first."""

    CRITICAL_OPS = 0  # non-payment stops operations (the shipping invoice)
    TIME_SENSITIVE = 1  # a supplier with a hard deadline
    STANDARD = 2
    DEFERRABLE = 3  # may be deferred when cash is tight


class TransferMethod(StrEnum):
    LOCAL = "LOCAL"
    SWIFT = "SWIFT"


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    description: str
    beneficiary_id: str
    amount: Money
    method: TransferMethod
    due_at: datetime
    priority: Priority
    policy_exception: str | None = None  # set means a person must decide; never executed
    # The operator's label for this obligation's terms, recorded on each lock attempt. Locks are
    # keyed by obligation_id alone, and a run re-checks beneficiary, amount and method
    # themselves against its approval, so a change needs no bump to be safe; a reseed keeps it.
    version: str = "1"

    def __post_init__(self) -> None:
        if not _ID.match(self.obligation_id) or not _ID.match(self.version):
            raise ValueError("obligation_id and version must be short lower-case slugs")
        if not self.amount.amount > 0:
            raise ValueError(f"obligation {self.obligation_id} must have a positive amount")
        if self.due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")


@dataclass(frozen=True)
class Forecast:
    """An expected receipt. Corroboration flags come from bank and invoice data, not the model."""

    receipt_id: str
    payer: str
    amount: Money
    expected_at: datetime
    bank_corroborated: bool
    invoice_matched: bool


@dataclass(frozen=True)
class TreasuryBook:
    book_version: str
    funding_currency: str  # the wallet every conversion sells from
    obligations: tuple[Obligation, ...]
    forecasts: tuple[Forecast, ...]
    floors: Mapping[str, Money] = field(default_factory=dict)  # reserve floor per wallet

    def __post_init__(self) -> None:
        ids = [o.obligation_id for o in self.obligations]
        if len(ids) != len(set(ids)):
            raise ValueError("obligation ids must be unique")
        rids = [f.receipt_id for f in self.forecasts]
        if len(rids) != len(set(rids)):
            raise ValueError("receipt ids must be unique")
        for cur in {self.funding_currency} | {o.amount.currency for o in self.obligations}:
            floor = self.floors.get(cur)
            if floor is None:
                raise ValueError(f"no reserve floor configured for {cur}")
            if floor.currency != cur or floor.is_negative():
                raise ValueError(f"floor for {cur} must be a non-negative {cur} amount")
        object.__setattr__(self, "floors", dict(self.floors))

    def obligation(self, obligation_id: str) -> Obligation:
        for o in self.obligations:
            if o.obligation_id == obligation_id:
                return o
        raise KeyError(obligation_id)

    def has_forecast(self, receipt_id: str) -> bool:
        return any(f.receipt_id == receipt_id for f in self.forecasts)

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted(self.floors))


def book_to_dict(book: TreasuryBook) -> dict[str, Any]:
    """Store form. Money and datetimes survive awx_common.transport exactly."""
    return {
        "book_version": book.book_version,
        "funding_currency": book.funding_currency,
        "floors": dict(book.floors),
        "obligations": [
            {
                "obligation_id": o.obligation_id,
                "version": o.version,
                "description": o.description,
                "beneficiary_id": o.beneficiary_id,
                "amount": o.amount,
                "method": o.method.value,
                "due_at": o.due_at,
                "priority": o.priority.name,
                "policy_exception": o.policy_exception,
            }
            for o in book.obligations
        ],
        "forecasts": [
            {
                "receipt_id": f.receipt_id,
                "payer": f.payer,
                "amount": f.amount,
                "expected_at": f.expected_at,
                "bank_corroborated": f.bank_corroborated,
                "invoice_matched": f.invoice_matched,
            }
            for f in book.forecasts
        ],
    }


def book_from_dict(d: Mapping[str, Any]) -> TreasuryBook:
    return TreasuryBook(
        book_version=d["book_version"],
        funding_currency=d["funding_currency"],
        floors=dict(d["floors"]),
        obligations=tuple(
            Obligation(
                obligation_id=o["obligation_id"],
                description=o["description"],
                beneficiary_id=o["beneficiary_id"],
                amount=o["amount"],
                method=TransferMethod(o["method"]),
                due_at=o["due_at"],
                priority=Priority[o["priority"]],
                policy_exception=o["policy_exception"],
                version=o["version"],
            )
            for o in d["obligations"]
        ),
        forecasts=tuple(
            Forecast(
                f["receipt_id"],
                f["payer"],
                f["amount"],
                f["expected_at"],
                bool(f["bank_corroborated"]),
                bool(f["invoice_matched"]),
            )
            for f in d["forecasts"]
        ),
    )


def load_book(store: KitStore | None) -> TreasuryBook | None:
    if store is None:
        return None
    item = store.get(BOOK_KEY)
    return book_from_dict(item.value) if item else None


def load_book_id(store: KitStore | None) -> str | None:
    if store is None:
        return None
    item = store.get(BOOK_ID_KEY)
    return str(item.value["book_id"]) if item else None


def ensure_book_id(store: KitStore) -> str:
    """Kit rule M2: a random book id, minted once when this kit's state is first created and
    kept across every later reseed. It goes into every request_id built from kit-local values,
    so a fresh book seeded like an earlier one (e.g. from DEMO_BOOK) never reuses, and never
    adopts, an earlier book's objects on the shared sandbox. Conditional create: of two
    concurrent first writers exactly one wins, and both then read the winner's id."""
    existing = load_book_id(store)
    if existing:
        return existing
    try:
        store.put(BOOK_ID_KEY, {"book_id": uuid.uuid4().hex[:16]}, expected_version=None)
    except VersionConflict:
        pass
    book_id = load_book_id(store)
    if not book_id:
        raise RuntimeError("book id could not be created")
    return book_id


_CURRENT = object()


def seed_book(store: KitStore, book: TreasuryBook, *, expected_version: Any = _CURRENT) -> None:
    """Write (or replace) the book. Operator tooling and tests only; never a step. The book
    id is minted on the first seed and never changes after it.

    `expected_version` is the book's store version the caller checked (None: no book). Given,
    the write is a CAS against it, so a seed that landed after the caller's check makes this
    raise VersionConflict instead of being overwritten."""
    if expected_version is _CURRENT:
        item = store.get(BOOK_KEY)
        expected_version = item.version if item else None
    ensure_book_id(store)
    store.put(BOOK_KEY, book_to_dict(book), expected_version=expected_version)


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def _m(amount: str, currency: str) -> Money:
    return Money.of(amount, currency)


# The guide's scenario: five obligations across three currencies over 72 hours. Beneficiary IDs
# are placeholders: `seed_treasury_book` refuses them and maps each kept obligation to a real
# `poc-i1-` sandbox beneficiary.
DEMO_BOOK = TreasuryBook(
    book_version="demo-2026-10-06",
    funding_currency="USD",
    floors={"USD": _m("20000", "USD"), "EUR": _m("500", "EUR"), "GBP": _m("500", "GBP")},
    obligations=(
        Obligation(
            "ob-shipping",
            "Freight forwarder invoice; unpaid freight stops outbound shipping",
            "ben-poc-k1-freight-us",
            _m("12000", "USD"),
            TransferMethod.LOCAL,
            _at(19, 9),
            Priority.CRITICAL_OPS,
        ),
        Obligation(
            "ob-supplier-eur",
            "Component supplier in Germany; price lock expires at due time",
            "ben-poc-k1-supplier-de",
            _m("7525.70", "EUR"),
            TransferMethod.SWIFT,
            _at(20, 12),
            Priority.TIME_SENSITIVE,
        ),
        Obligation(
            "ob-saas-gbp",
            "Monthly SaaS licence",
            "ben-poc-k1-saas-gb",
            _m("300", "GBP"),
            TransferMethod.LOCAL,
            _at(21, 0),
            Priority.STANDARD,
        ),
        Obligation(
            "ob-office-usd",
            "Office supplies, payable any time this month",
            "ben-poc-k1-office-us",
            _m("450", "USD"),
            TransferMethod.LOCAL,
            _at(21, 9),
            Priority.DEFERRABLE,
        ),
        Obligation(
            "ob-contractor-gbp",
            "New contractor; beneficiary added less than 24 hours ago",
            "ben-poc-k1-contractor-gb",
            _m("900", "GBP"),
            TransferMethod.LOCAL,
            _at(20, 17),
            Priority.STANDARD,
            policy_exception="beneficiary added within the last 24 hours",
        ),
    ),
    forecasts=(
        Forecast(
            "rcpt-acme",
            "Acme Retail",
            _m("8000", "USD"),
            _at(19, 21),
            bank_corroborated=True,
            invoice_matched=True,
        ),
    ),
)
