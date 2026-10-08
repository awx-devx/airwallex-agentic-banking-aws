"""The platform capital ledger for bridge advances.

One currency, held in a platform-scoped `awx_common.store.KitStore` as one versioned document.
Every change is a read, a check and a conditional write (`expected_version`), retried on
`VersionConflict`, so the reserve-floor check (I4) holds across processes and Lambdas: two
bridges racing for the last headroom cannot both place a hold.

Money only moves in the ledger when the provider object has reached SETTLED; before that a
bridge is a *hold* that counts against headroom. Every posting is idempotent on its key (the
Airwallex request_id), so a duplicate delivery or a re-run postcondition posts once.

Invariants, checked before every write and by `check_invariants`:

  I1  cash == contributed + repaid + fees - advanced          (recomputed from the entries)
  I2  cash + sum(receivables) == contributed + fees           (capital is conserved)
  I3  every receivable >= 0, every hold > 0, cash >= 0
  I4  cash - held >= reserve_floor whenever a hold is placed  (the floor binds the allocator)

I4 is enforced at `place_hold`, the only operation that commits capital. Settling a held advance
moves the amount from held to advanced and leaves cash - held unchanged; releases, repayments
and fees only raise it. So once true, `cash - held >= reserve_floor` stays true.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from awx_common import Money
from awx_common.store import KitStore, VersionConflict

LEDGER_KEY = "ledger"
_CAS_ATTEMPTS = 8


class LedgerError(Exception):
    """A posting the ledger refuses. Nothing changed."""


class ReserveBreach(LedgerError):
    """A hold would take platform cash below the reserve floor."""


class StaleObservation(LedgerError):
    """The ledger moved after the wallet was observed; observe the wallet again."""


@dataclass(frozen=True)
class WalletObservation:
    """The platform wallet's available balance, read while the ledger stood at `version`."""

    version: int
    available: Money


class EntryKind(StrEnum):
    CAPITAL_IN = "CAPITAL_IN"
    ADVANCE = "ADVANCE"  # platform -> customer, settled
    REPAYMENT = "REPAYMENT"  # customer -> platform, settled, reduces the receivable
    FEE = "FEE"  # customer -> platform, settled, fee revenue


@dataclass(frozen=True)
class LedgerEntry:
    kind: EntryKind
    key: str  # the Airwallex request_id, or a funding reference
    tenant_id: str | None
    amount: Money
    provider_id: str | None
    at: datetime


@dataclass(frozen=True)
class Hold:
    key: str  # the bridge request_id
    tenant_id: str
    amount: Money
    owner: str  # M4: the proposal_id of the run that placed it


@dataclass(frozen=True)
class ProviderMovement:
    """A platform movement as Airwallex reports it, for reconciliation."""

    provider_id: str
    request_id: str
    kind: str  # "connected_account_transfer" or "charge"
    status: str
    amount: Money
    connected_account_id: str


@dataclass
class _State:
    currency: str
    book_id: str | None = None  # M2: minted once, when the ledger document is first written
    entries: list[LedgerEntry] = field(default_factory=list)
    holds: dict[str, Hold] = field(default_factory=dict)
    # business_key -> (failed attempts, request_id of the last failed attempt)
    failures: dict[str, tuple[int, str | None]] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)  # every request_id recorded as failed

    def by_key(self, key: str) -> LedgerEntry | None:
        for e in self.entries:
            if e.key == key:
                return e
        return None

    def total(self, kind: EntryKind, tenant_id: str | None = None) -> Money:
        out = Money.zero(self.currency)
        for e in self.entries:
            if e.kind is kind and (tenant_id is None or e.tenant_id == tenant_id):
                out = out + e.amount
        return out

    def cash(self) -> Money:
        return (
            self.total(EntryKind.CAPITAL_IN)
            + self.total(EntryKind.REPAYMENT)
            + self.total(EntryKind.FEE)
            - self.total(EntryKind.ADVANCE)
        )

    def held(self, exclude: str | None = None) -> Money:
        out = Money.zero(self.currency)
        for h in self.holds.values():
            if h.key != exclude:
                out = out + h.amount
        return out

    def receivable(self, tenant_id: str) -> Money:
        return self.total(EntryKind.ADVANCE, tenant_id) - self.total(EntryKind.REPAYMENT, tenant_id)

    def receivables(self) -> dict[str, Money]:
        tenants = {e.tenant_id for e in self.entries if e.kind is EntryKind.ADVANCE}
        return {t: self.receivable(t) for t in sorted(t for t in tenants if t is not None)}

    def check(self) -> None:
        contributed = self.total(EntryKind.CAPITAL_IN)
        fees = self.total(EntryKind.FEE)
        cash = self.cash()
        total_recv = Money.zero(self.currency)
        for t, r in self.receivables().items():
            if r.is_negative():
                raise LedgerError(f"I3: receivable for {t} is negative")
            total_recv = total_recv + r
        if cash + total_recv != contributed + fees:
            raise LedgerError("I2: capital is not conserved")
        if cash.is_negative():
            raise LedgerError("I3: cash is negative")
        if any(h.amount.amount <= 0 for h in self.holds.values()):
            raise LedgerError("I3: non-positive hold")

    # ---- persistence ---------------------------------------------------------------------

    def encode(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "book_id": self.book_id,
            "entries": [
                {
                    "kind": e.kind.value,
                    "key": e.key,
                    "tenant_id": e.tenant_id,
                    "amount": e.amount,
                    "provider_id": e.provider_id,
                    "at": e.at,
                }
                for e in self.entries
            ],
            "holds": {
                k: {"tenant_id": h.tenant_id, "amount": h.amount, "owner": h.owner}
                for k, h in self.holds.items()
            },
            "failures": {k: [n, rid] for k, (n, rid) in self.failures.items()},
            "failed": sorted(self.failed),
        }

    @classmethod
    def decode(cls, data: Mapping[str, Any]) -> _State:
        return cls(
            currency=data["currency"],
            book_id=data["book_id"],
            entries=[
                LedgerEntry(
                    EntryKind(e["kind"]),
                    e["key"],
                    e["tenant_id"],
                    e["amount"],
                    e["provider_id"],
                    e["at"],
                )
                for e in data["entries"]
            ],
            holds={
                k: Hold(k, h["tenant_id"], h["amount"], h["owner"])
                for k, h in data["holds"].items()
            },
            failures={k: (int(v[0]), v[1]) for k, v in data["failures"].items()},
            failed=set(data.get("failed", [])),
        )


class CapitalLedger:
    """The ledger document in a platform-scoped KitStore. Every read loads fresh state."""

    def __init__(self, store: KitStore, reserve_floor: Money) -> None:
        if reserve_floor.is_negative():
            raise ValueError("reserve floor cannot be negative")
        self.store = store
        self.currency = reserve_floor.currency
        self.reserve_floor = reserve_floor

    # ---- state access ----------------------------------------------------------------------

    def _load(self) -> tuple[_State, int | None]:
        item = self.store.get(LEDGER_KEY)
        if item is None:
            return _State(self.currency), None
        state = _State.decode(item.value)
        if state.currency != self.currency:
            raise LedgerError("stored ledger currency differs from the platform currency")
        return state, item.version

    def _mutate(self, change: Callable[..., Any], *, with_version: bool = False) -> Any:
        """Read, change, check invariants, conditional write. `change` returns (result, changed);
        changed False skips the write. With `with_version`, `change(state, version)` also gets
        the version it was read at, so it can pin a write to an observation."""
        for _ in range(_CAS_ATTEMPTS):
            state, version = self._load()
            result, changed = change(state, version or 0) if with_version else change(state)
            if not changed:
                return result
            if version is None and state.book_id is None:
                state.book_id = uuid.uuid4().hex  # first write creates the book (M2)
            state.check()
            try:
                self.store.put(LEDGER_KEY, state.encode(), expected_version=version)
            except VersionConflict:
                continue  # another writer moved the ledger; re-read and re-check
            return result
        raise LedgerError("ledger write kept conflicting; try again")

    # ---- reads ------------------------------------------------------------------------------

    @property
    def book_id(self) -> str | None:
        """The platform book id, or None before the ledger's first write (capital funding)."""
        return self._load()[0].book_id

    @property
    def version(self) -> int:
        item = self.store.get(LEDGER_KEY)
        return 0 if item is None else item.version

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._load()[0].entries)

    def entry(self, key: str) -> LedgerEntry | None:
        return self._load()[0].by_key(key)

    def holds(self) -> tuple[Hold, ...]:
        return tuple(self._load()[0].holds.values())

    def hold(self, key: str) -> Hold | None:
        return self._load()[0].holds.get(key)

    @property
    def cash(self) -> Money:
        return self._load()[0].cash()

    @property
    def held(self) -> Money:
        return self._load()[0].held()

    def receivable(self, tenant_id: str) -> Money:
        return self._load()[0].receivable(tenant_id)

    def receivables(self) -> dict[str, Money]:
        return self._load()[0].receivables()

    def fee_revenue(self) -> Money:
        return self._load()[0].total(EntryKind.FEE)

    def failed(self, request_id: str) -> bool:
        """True once `request_id` was recorded as a definitive failure."""
        return request_id in self._load()[0].failed

    def failures(self, business_key: str) -> int:
        return self._load()[0].failures.get(business_key, (0, None))[0]

    def headroom(self, *, exclude_hold: str | None = None) -> Money:
        """Capital that can still be committed without breaching the floor. May be negative."""
        state = self._load()[0]
        return state.cash() - state.held(exclude_hold) - self.reserve_floor

    def has_open_advance(self, tenant_id: str) -> bool:
        state = self._load()[0]
        return state.receivable(tenant_id).amount > 0 or any(
            h.tenant_id == tenant_id for h in state.holds.values()
        )

    def check_invariants(self) -> None:
        self._load()[0].check()

    # ---- writes -----------------------------------------------------------------------------

    def _check_money(self, amount: Money) -> None:
        if amount.currency != self.currency:
            raise LedgerError(f"ledger is {self.currency}, got {amount.currency}")
        if amount.amount <= 0:
            raise LedgerError("amounts must be positive")

    @staticmethod
    def _append(state: _State, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        existing = state.by_key(entry.key)
        if existing is not None:
            same = (existing.kind, existing.tenant_id, existing.amount) == (
                entry.kind,
                entry.tenant_id,
                entry.amount,
            )
            if not same:
                raise LedgerError("key already posted with different content")
            return existing, False  # duplicate delivery: post once
        state.entries.append(entry)
        return entry, True

    def fund(self, key: str, amount: Money, *, at: datetime) -> LedgerEntry:
        self._check_money(amount)
        entry = LedgerEntry(EntryKind.CAPITAL_IN, key, None, amount, None, at)
        return self._mutate(lambda s: self._append(s, entry))

    def place_hold(
        self,
        key: str,
        tenant_id: str,
        amount: Money,
        *,
        owner: str,
        wallet: WalletObservation | None = None,
    ) -> tuple[Hold, bool]:
        """Commit capital for a bridge about to be submitted. Idempotent on key. Returns the hold
        and whether this call created it, so a build that raises afterwards releases only a
        hold it placed itself (M4).

        `wallet` binds the floor to the platform wallet as well as the books: inside the same
        versioned write, `amount <= wallet.available - held - floor`, where `held` is read in
        that write and the write only lands while the ledger is still at the version the wallet
        was read against (else StaleObservation). So two bridges racing for the last room cannot
        both pass on a stale observation. A hold whose transfer already
        moved money is subtracted twice: that errs towards denying, never towards a breach."""
        self._check_money(amount)

        def change(state: _State, version: int) -> tuple[tuple[Hold, bool], bool]:
            if state.by_key(key) is not None:
                raise LedgerError("this bridge has already settled")
            existing = state.holds.get(key)
            if existing is not None:
                # Idempotent return: no new capital is committed, so no pin is needed.
                if (existing.tenant_id, existing.amount) != (tenant_id, amount):
                    raise LedgerError("hold key reused with a different tenant or amount")
                return (existing, False), False
            # Creating a hold commits capital: the wallet observation must still be current,
            # checked against the version this very write is conditional on.
            if wallet is not None and version != wallet.version:
                raise StaleObservation("the ledger moved since the wallet was read")
            headroom = state.cash() - state.held(key) - self.reserve_floor
            if amount > headroom:
                raise ReserveBreach("bridge would take platform cash below the reserve floor")
            if wallet is not None:
                room = wallet.available - state.held(key) - self.reserve_floor
                if amount > room:
                    raise ReserveBreach("bridge would take the platform wallet below the floor")
            hold = Hold(key, tenant_id, amount, owner)
            state.holds[key] = hold
            return (hold, True), True

        return self._mutate(change, with_version=True)

    def release_hold(self, key: str, *, owner: str) -> bool:
        """Release a hold its owner placed and never sent (M4). A hold whose operation went out
        is released only through `record_failure` or settlement, after a definitive outcome."""

        def change(state: _State) -> tuple[bool, bool]:
            hold = state.holds.get(key)
            if hold is None or hold.owner != owner:
                return False, False
            del state.holds[key]
            return True, True

        return self._mutate(change)

    def record_failure(self, business_key: str, request_id: str) -> int:
        """The attempt `request_id` for `business_key` definitely failed: a non-retryable
        rejection at create, or the provider object reached FAILED. Releases its hold and moves
        the business object to its next attempt, because the operation store (and, for a FAILED
        object, Airwallex) will never accept that request_id again. Idempotent per request_id."""

        def change(state: _State) -> tuple[int, bool]:
            count, last = state.failures.get(business_key, (0, None))
            if last == request_id:
                return count, False
            if state.by_key(request_id) is not None:
                raise LedgerError("a settled attempt cannot be recorded as failed")
            state.holds.pop(request_id, None)
            state.failures[business_key] = (count + 1, request_id)
            state.failed.add(request_id)
            return count + 1, True

        return self._mutate(change)

    def post_advance(
        self, key: str, tenant_id: str, amount: Money, *, provider_id: str, at: datetime
    ) -> LedgerEntry:
        """The bridge reached SETTLED. Converts its hold into an advance."""
        self._check_money(amount)
        entry = LedgerEntry(EntryKind.ADVANCE, key, tenant_id, amount, provider_id, at)

        def change(state: _State) -> tuple[LedgerEntry, bool]:
            if state.by_key(key) is None:
                hold = state.holds.get(key)
                if hold is None or (hold.tenant_id, hold.amount) != (tenant_id, amount):
                    raise LedgerError("a settled advance must match its hold")
                del state.holds[key]
            return self._append(state, entry)

        return self._mutate(change)

    def post_repayment(
        self, key: str, tenant_id: str, amount: Money, *, provider_id: str, at: datetime
    ) -> LedgerEntry:
        self._check_money(amount)
        entry = LedgerEntry(EntryKind.REPAYMENT, key, tenant_id, amount, provider_id, at)

        def change(state: _State) -> tuple[LedgerEntry, bool]:
            if state.by_key(key) is None and amount > state.receivable(tenant_id):
                raise LedgerError("repayment exceeds the tenant's receivable")
            return self._append(state, entry)

        return self._mutate(change)

    def post_fee(
        self, key: str, tenant_id: str, amount: Money, *, provider_id: str, at: datetime
    ) -> LedgerEntry:
        self._check_money(amount)
        entry = LedgerEntry(EntryKind.FEE, key, tenant_id, amount, provider_id, at)
        return self._mutate(lambda s: self._append(s, entry))

    # ---- reconciliation ---------------------------------------------------------------------

    def reconcile(
        self, movements: Iterable[ProviderMovement], *, accounts: Mapping[str, str]
    ) -> list[str]:
        """Compare settled postings with the provider's view. Returns discrepancies; empty is clean.

        `accounts` maps tenant_id to connected account id. Pass only the movements this kit
        created (their reference carries `poc-k5-`), never the whole platform's traffic. Every
        ADVANCE/REPAYMENT/FEE needs a SETTLED provider movement with the same amount and
        account, and every SETTLED movement needs a posting."""
        state = self._load()[0]
        problems: list[str] = []
        by_request = {m.request_id: m for m in movements}
        for e in state.entries:
            if e.kind is EntryKind.CAPITAL_IN:
                continue
            m = by_request.get(e.key)
            if m is None:
                problems.append(f"{e.kind} {e.key}: no provider movement")
                continue
            if m.status != "SETTLED":
                problems.append(f"{e.kind} {e.key}: provider status {m.status}, ledger settled")
            if m.amount != e.amount:
                problems.append(f"{e.kind} {e.key}: provider {m.amount} vs ledger {e.amount}")
            if e.tenant_id is None or accounts.get(e.tenant_id) != m.connected_account_id:
                problems.append(f"{e.kind} {e.key}: provider account differs from tenant")
        for m in by_request.values():
            if m.status == "SETTLED" and state.by_key(m.request_id) is None:
                if m.request_id in state.holds:
                    problems.append(f"{m.request_id}: SETTLED at provider, still a hold here")
                else:
                    problems.append(f"{m.request_id}: SETTLED at provider, not in ledger")
        return problems
