"""The loan book: loan terms, the subledger, week records, in-flight movements and portfolio
settings, persisted as one versioned item in the kit's KitStore.

Loans are opened by an operator after (synthetic) underwriting, never by the model
(`open_portfolio`, `LoanBook.add_loan`, `LoanBook.fund`). The book also holds the server-side
mapping from a loan to its borrower's tenant and connected account, so no tool input ever
names an account.

Persistence: the book is portfolio-wide, so it lives in
`PolicyContext.platform_store`, which every k7 tenant shares; `loan_for_tenant` keeps each
borrower to its own loan. `load_book(store)` returns the book and its version; policy code
only loads. Builds and postconditions change the book through `update_book(store, fn)`, which
writes with `expected_version`, so two concurrent writers cannot both succeed: the loser gets
VersionConflict and its run fails closed (rule 7). Every mutation is idempotent, so a runner
that calls a build or postcondition again after a crash changes nothing.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from awx_common import ApprovalBindingError, Money, TenantContext
from awx_common.errors import TenantScopeViolation, ValidationFailed
from awx_common.ops import DEDUP_RESEND_MARGIN
from awx_common.store import KitStore

from .ledger import (
    Acct,
    DuplicatePosting,
    Entry,
    EntryKind,
    LedgerError,
    Subledger,
    capital_entry,
    disbursement_entry,
    week_close_entry,
)
from .repayment import RepaymentTerms

FEE_ROUNDING = ROUND_HALF_UP
BOOK_KEY = "portfolio"
# Airwallex deduplicates a request_id for about 7 days (FAKE-FIDELITY rows 51-52). A held
# movement older than this margin is never re-emitted: it goes to a person (rule M1). It is
# the spine's own deduplication margin, so kit and executor agree on when a resend stops.
DEDUP_MARGIN = DEDUP_RESEND_MARGIN
# A tranche created near the end of a week may be seen SETTLED just after it; the revenue
# backstop counts tranches seen SETTLED up to this long after the week.
CREDIT_MARGIN = timedelta(hours=1)
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
_LOAN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,40}$")
_BOOK_ID = re.compile(r"^[0-9a-f]{12}$")


class PortfolioNotOpen(LookupError):
    """The kit store holds no loan book."""


_DEMO = re.compile(r"^DEMO(\d{2,5})S-(\d{1,9})$")
# DEMO collection periods count from this instant. Period n of length p seconds is
# [DEMO_ANCHOR + n*p, DEMO_ANCHOR + (n+1)*p), named "DEMO<p>S-<n>".
DEMO_ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)
DEMO_MIN_S, DEMO_MAX_S = 60, 86400


def week_window(week: str) -> tuple[datetime, datetime]:
    """A collection period -> [start, end).

    ISO week 'YYYY-Www' -> [Monday 00:00 UTC, next Monday 00:00 UTC). A labelled DEMO period
    'DEMO<seconds>S-<n>' (PortfolioConfig.demo_period_s) -> its short window, so a live demo
    can close a collection period without waiting a real week.
    """
    d = _DEMO.match(week)
    if d:
        length = int(d.group(1))
        if not DEMO_MIN_S <= length <= DEMO_MAX_S:
            raise ValidationFailed(f"demo period length must be {DEMO_MIN_S}-{DEMO_MAX_S} s")
        start = DEMO_ANCHOR + timedelta(seconds=length * int(d.group(2)))
        return start, start + timedelta(seconds=length)
    m = _WEEK.match(week)
    if not m:
        raise ValidationFailed(f"week must look like 2026-W41 (or DEMO300S-123), got {week!r}")
    try:
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError as exc:
        raise ValidationFailed(f"no such ISO week {week!r}") from exc
    start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    return start, start + timedelta(days=7)


def is_demo_period(week: str) -> bool:
    return bool(_DEMO.match(week))


def week_of(start: datetime, demo_period_s: int | None = None) -> str:
    """The period starting exactly at `start`."""
    if demo_period_s is None:
        return start.strftime("%G-W%V")
    n, rem = divmod(int((start - DEMO_ANCHOR).total_seconds()), demo_period_s)
    if rem:
        raise ValueError("start is not a demo period boundary")
    return f"DEMO{demo_period_s}S-{n}"


def first_week_starting_at_or_after(moment: datetime, demo_period_s: int | None = None) -> str:
    """The first collection period whose start is at or after `moment`."""
    moment = moment.astimezone(UTC)
    if demo_period_s is not None:
        offset = (moment - DEMO_ANCHOR).total_seconds()
        n = -(-int(offset * 1_000_000) // (demo_period_s * 1_000_000))  # ceil, exact
        return f"DEMO{demo_period_s}S-{n}"
    monday = (moment - timedelta(days=moment.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if monday < moment:
        monday += timedelta(days=7)
    return week_of(monday)


def next_week(week: str) -> str:
    d = _DEMO.match(week)
    if d:
        return f"DEMO{d.group(1)}S-{int(d.group(2)) + 1}"
    return week_of(week_window(week)[1])


def check_loan_id(loan_id: object) -> str:
    if not isinstance(loan_id, str) or not _LOAN_ID.match(loan_id):
        raise ValidationFailed("loan_id must be 3-41 chars of a-z, 0-9, '_' or '-'")
    return loan_id


@dataclass(frozen=True)
class LoanTerms:
    loan_id: str
    borrower_tenant_id: str
    borrower_account: str  # acct_..., server-side only
    commitment: Money  # approved advance principal
    fee_rate: Decimal  # fixed fee as a fraction of each disbursed tranche
    repayment: RepaymentTerms
    first_week: str  # earliest ISO week a collection may run

    def __post_init__(self) -> None:
        check_loan_id(self.loan_id)
        if not self.borrower_account.startswith("acct_"):
            raise ValueError("borrower_account must be an acct_ id")
        if self.commitment.amount <= 0:
            raise ValueError("commitment must be positive")
        if isinstance(self.fee_rate, float | bool) or not isinstance(self.fee_rate, Decimal):
            raise TypeError("fee_rate must be a Decimal")
        if not Decimal(0) <= self.fee_rate < Decimal(1):
            raise ValueError("fee_rate must be in [0, 1)")
        if self.repayment.currency != self.commitment.currency:
            raise ValueError("repayment terms and commitment must share a currency")
        week_window(self.first_week)

    @property
    def currency(self) -> str:
        return self.commitment.currency

    def fee_for(self, principal: Money) -> Money:
        return principal.scale(self.fee_rate, rounding=FEE_ROUNDING)

    def to_state(self) -> dict[str, Any]:
        return {
            "loan_id": self.loan_id,
            "borrower_tenant_id": self.borrower_tenant_id,
            "borrower_account": self.borrower_account,
            "commitment": self.commitment,
            "fee_rate": self.fee_rate,
            "share_rate": self.repayment.share_rate,
            "weekly_cap": self.repayment.weekly_cap,
            "weekly_floor": self.repayment.weekly_floor,
            "first_week": self.first_week,
        }

    @classmethod
    def from_state(cls, d: Mapping[str, Any]) -> LoanTerms:
        return cls(
            loan_id=d["loan_id"],
            borrower_tenant_id=d["borrower_tenant_id"],
            borrower_account=d["borrower_account"],
            commitment=d["commitment"],
            fee_rate=d["fee_rate"],
            repayment=RepaymentTerms(d["share_rate"], d["weekly_cap"], d["weekly_floor"]),
            first_week=d["first_week"],
        )


@dataclass(frozen=True)
class PortfolioConfig:
    currency: str
    reserve_floor: Money  # platform cash must stay at or above this after any disbursement
    min_disbursement: Money  # below this a tranche is delayed, not sent
    disbursement_increment: Money  # tranches are rounded DOWN to a multiple of this
    dual_approval_above: Money  # a tranche above this needs two approvers
    # DEMO ONLY: collect over short labelled periods ("DEMO<s>S-<n>") instead of ISO weeks,
    # so a live demo can close a period. None (the default) is the real weekly product.
    demo_period_s: int | None = None

    def __post_init__(self) -> None:
        for m in (
            self.reserve_floor,
            self.min_disbursement,
            self.disbursement_increment,
            self.dual_approval_above,
        ):
            if m.currency != self.currency:
                raise ValueError("every portfolio amount must be in the portfolio currency")
        if self.reserve_floor.is_negative() or self.min_disbursement.amount <= 0:
            raise ValueError("need reserve_floor >= 0 and min_disbursement > 0")
        if self.disbursement_increment.amount <= 0:
            raise ValueError("disbursement_increment must be positive")
        if self.demo_period_s is not None and not (DEMO_MIN_S <= self.demo_period_s <= DEMO_MAX_S):
            raise ValueError(f"demo_period_s must be {DEMO_MIN_S}-{DEMO_MAX_S} seconds")

    def to_state(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "reserve_floor": self.reserve_floor,
            "min_disbursement": self.min_disbursement,
            "disbursement_increment": self.disbursement_increment,
            "dual_approval_above": self.dual_approval_above,
            "demo_period_s": self.demo_period_s,
        }

    @classmethod
    def from_state(cls, d: Mapping[str, Any]) -> PortfolioConfig:
        return cls(**dict(d))


@dataclass(frozen=True)
class WeekRecord:
    loan_id: str
    week: str
    due: Money
    collected: Money
    charge_request_id: str | None  # set whenever a charge was built for the week
    charge_id: str | None
    charge_status: str | None  # provider status, or None when no charge was sent

    @property
    def shortfall(self) -> Money:
        return self.due - self.collected


@dataclass(frozen=True)
class PendingDisbursement:
    """owner is the proposal_id of the run that placed the hold (rule M4), never intent_hash.
    A later run re-sending the same tranche leaves the owner as it was; the hold is released
    only by a run that read a definitive outcome for its request_id (M1)."""

    loan_id: str
    tranche: int
    amount: Money
    request_id: str
    owner: str
    placed_at: datetime  # when the first send was built; resends never move it

    def same_money(self, other: PendingDisbursement) -> bool:
        return (self.loan_id, self.tranche, self.amount, self.request_id) == (
            other.loan_id,
            other.tranche,
            other.amount,
            other.request_id,
        )


@dataclass(frozen=True)
class PendingCollection:
    """A charge built for a week that has not reached a terminal, posted state.

    While it exists the loan's only collectable week is this one, and a new proposal reuses
    its amount and request_id, so the same charge is re-sent (Airwallex refuses the duplicate
    and the reconcile hook reads the original) rather than a different one."""

    loan_id: str
    week: str
    due: Money
    amount: Money
    request_id: str
    owner: str  # proposal_id of the run that placed the hold (rule M4)
    placed_at: datetime  # when the first send was built; resends never move it

    def same_money(self, other: PendingCollection) -> bool:
        return (self.loan_id, self.week, self.due, self.amount, self.request_id) == (
            other.loan_id,
            other.week,
            other.due,
            other.amount,
            other.request_id,
        )


@dataclass(frozen=True)
class ReconcileRecord:
    at: datetime
    ok: bool
    breaks: tuple[str, ...]


@dataclass
class LoanBook:
    """book_id is minted once by open_portfolio and goes into every request_id business key
    and reference (rule M2), so a fresh book seeded like an old one can never reuse, and so
    adopt, an earlier run's Airwallex objects on the shared sandbox."""

    config: PortfolioConfig
    book_id: str
    ledger: Subledger = field(init=False)
    _loans: dict[str, LoanTerms] = field(default_factory=dict)
    _weeks: dict[tuple[str, str], WeekRecord] = field(default_factory=dict)
    _pending: dict[str, PendingDisbursement] = field(default_factory=dict)  # by request_id
    _collections: dict[str, PendingCollection] = field(default_factory=dict)  # by loan_id
    _tranches: dict[str, int] = field(default_factory=dict)
    _attempts: dict[str, int] = field(default_factory=dict)  # by attempt key, default 1
    _released: dict[str, tuple[str, str]] = field(default_factory=dict)  # rid -> (kind, loan)
    last_reconcile: ReconcileRecord | None = None
    _sizings: dict[str, dict[str, Any]] = field(default_factory=dict)  # operator-only

    def record_sizing(self, request_id: str, sizing: Mapping[str, Any]) -> None:
        """What sized a tranche (platform cash, wallet, in flight, usable, floor), for
        operators and audit only. First write wins, so a retried build changes nothing."""
        self._sizings.setdefault(request_id, dict(sizing))

    def sizings(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._sizings.items()}

    def __post_init__(self) -> None:
        if not _BOOK_ID.match(self.book_id):
            raise ValueError("book_id must be 12 hex chars minted by open_portfolio")
        self.ledger = Subledger(self.config.currency)

    # Attempts (rule M1): a new request_id for the same money only after a definitive failure

    @staticmethod
    def _attempt_key(kind: str, loan_id: str, slot: str) -> str:
        return f"{kind}:{loan_id}:{slot}"

    def attempt(self, kind: str, loan_id: str, slot: str) -> int:
        """The current attempt for a tranche (slot = tranche number) or a week (slot = week)."""
        return self._attempts.get(self._attempt_key(kind, loan_id, slot), 1)

    def burn_attempt(self, kind: str, loan_id: str, slot: str, request_id: str) -> None:
        """Record that `request_id` ended definitively failed, so the next proposal for the
        same money uses the next attempt. Idempotent per request_id."""
        if request_id in self._released:
            return
        k = self._attempt_key(kind, loan_id, slot)
        self._attempts[k] = self._attempts.get(k, 1) + 1
        self._released[request_id] = ("transfer" if kind == "disburse" else "charge", loan_id)

    def released(self) -> dict[str, tuple[str, str]]:
        return dict(self._released)

    # Loans and tenancy

    def add_loan(self, terms: LoanTerms) -> None:
        if terms.currency != self.config.currency:
            raise ValueError("loan currency differs from the portfolio")
        if terms.loan_id in self._loans:
            raise ValueError(f"loan {terms.loan_id} exists")
        if any(t.borrower_tenant_id == terms.borrower_tenant_id for t in self._loans.values()):
            raise ValueError("one open loan per borrower tenant")
        if any(t.borrower_account == terms.borrower_account for t in self._loans.values()):
            # Money keys on the connected account (charge source, transfer destination,
            # revenue reads): two loans on one account would share its wallet and revenue.
            raise ValueError("one open loan per borrower connected account")
        self._loans[terms.loan_id] = terms

    def loan(self, loan_id: str) -> LoanTerms:
        try:
            return self._loans[check_loan_id(loan_id)]
        except KeyError:
            raise ValidationFailed(f"unknown loan {loan_id}") from None

    def tenant_row_disagrees(self, tenant: TenantContext) -> bool:
        """The run tenant's connected account (the tenants table) is
        not the account seeded for that tenant, or is the account seeded for ANOTHER tenant.
        Either way nothing may be read or moved for it."""
        account = tenant.connected_account_id
        for t in self._loans.values():
            if t.borrower_tenant_id == tenant.tenant_id and t.borrower_account != account:
                return True
            if t.borrower_account == account and t.borrower_tenant_id != tenant.tenant_id:
                return True
        return False

    def loan_for_tenant(self, loan_id: str, tenant: TenantContext) -> LoanTerms:
        """The loan, only if the server-resolved tenant is that loan's borrower."""
        terms = self.loan(loan_id)
        if (
            tenant.tenant_id != terms.borrower_tenant_id
            or tenant.connected_account_id != terms.borrower_account
        ):
            raise TenantScopeViolation("loan does not belong to the resolved tenant")
        return terms

    def loans(self) -> tuple[LoanTerms, ...]:
        return tuple(self._loans[k] for k in sorted(self._loans))

    # Capital

    def fund(self, key: str, amount: Money) -> bool:
        return self.ledger.post(capital_entry(key, amount))

    # Disbursements

    def _disbursements(self, loan_id: str) -> list[Entry]:
        return [
            e
            for e in self.ledger.entries
            if e.kind is EntryKind.DISBURSEMENT and any(ln.loan_id == loan_id for ln in e.lines)
        ]

    def disbursed(self, loan_id: str) -> Money:
        total = Money.zero(self.config.currency)
        for e in self._disbursements(loan_id):
            for ln in e.lines:
                if ln.account is Acct.PRINCIPAL_RECEIVABLE:
                    total = total + ln.amount
        return total

    def funded_at(self, loan_id: str) -> datetime | None:
        """When the loan's first tranche was seen SETTLED."""
        first = self._disbursements(loan_id)
        if not first:
            return None
        return datetime.fromisoformat(first[0].memo["settled_at"])

    def tranche_credits(self, loan_id: str, start: datetime, end: datetime) -> Money:
        """Tranche money that may have landed in the borrower's wallet in [start, end): every
        tranche seen SETTLED in the window, plus any tranche still in flight (money moves at
        create, FAKE-FIDELITY row 110). The revenue read subtracts this whatever shape the
        balance-history row has."""
        total = Money.zero(self.config.currency)
        for e in self._disbursements(loan_id):
            if start <= datetime.fromisoformat(e.memo["settled_at"]) < end + CREDIT_MARGIN:
                for ln in e.lines:
                    if ln.account is Acct.PRINCIPAL_RECEIVABLE:
                        total = total + ln.amount
        pending = self.pending_for(loan_id)
        if pending is not None:
            total = total + pending.amount
        return total

    def commitment_remaining(self, loan_id: str) -> Money:
        return self.loan(loan_id).commitment - self.disbursed(loan_id)

    def next_tranche(self, loan_id: str) -> int:
        return self._tranches.get(loan_id, 0) + 1

    def pending(self) -> tuple[PendingDisbursement, ...]:
        return tuple(self._pending.values())

    def pending_for(self, loan_id: str) -> PendingDisbursement | None:
        return next((p for p in self._pending.values() if p.loan_id == loan_id), None)

    def pending_total(self) -> Money:
        return sum((p.amount for p in self._pending.values()), Money.zero(self.config.currency))

    def reserve_disbursement(self, pending: PendingDisbursement, *, resend: bool) -> None:
        """Hold headroom for an approved tranche until it is terminal.

        `resend` is what this run's decision saw: True only when policy found this hold and
        re-proposed it. A first-send decision that finds a hold another run placed after the
        decision was made fails closed: sending under it would let a
        refusal of this send release money that may still land. A re-send whose hold has gone
        fails closed too. Idempotent for a re-send of the same money."""
        prior = self._pending.get(pending.request_id)
        if prior is not None and prior.owner == pending.owner and prior.same_money(pending):
            return  # this proposal's own hold: a retried build (rule 0 idempotency)
        if prior is not None and not resend:
            raise ApprovalBindingError("another run holds this tranche; propose again")
        if prior is None and resend:
            raise ApprovalBindingError("the in-flight tranche this proposal re-sends is gone")
        if prior is not None:
            if not prior.same_money(pending):
                raise DuplicatePosting("request_id already reserved for a different tranche")
            return  # a re-send: the hold keeps its original owner
        if self.pending_for(pending.loan_id) is not None:
            raise LedgerError("loan already has a disbursement in flight")
        if pending.tranche != self.next_tranche(pending.loan_id):
            raise LedgerError("tranche number is stale")
        self._pending[pending.request_id] = pending

    def settle_disbursement(
        self, request_id: str, provider_id: str, amount: Money, *, settled_at: datetime
    ) -> bool:
        """Post a SETTLED tranche. A second delivery of the same settlement is a no-op."""
        key = f"disburse:{request_id}"
        prior = self.ledger.get(key)
        if prior is not None:
            if prior.provider_id != provider_id:
                raise DuplicatePosting("settlement replay names a different provider object")
            return False
        pending = self._pending.get(request_id)
        if pending is None:
            raise LedgerError("no reserved disbursement for this request_id")
        if amount != pending.amount:
            raise LedgerError(f"settled {amount} but approved {pending.amount}")
        terms = self.loan(pending.loan_id)
        posted = self.ledger.post(
            disbursement_entry(
                key,
                pending.loan_id,
                amount,
                terms.fee_for(amount),
                provider_id=provider_id,
                request_id=request_id,
                settled_at=settled_at.astimezone(UTC).isoformat(),
            )
        )
        self._tranches[pending.loan_id] = pending.tranche
        del self._pending[request_id]
        return posted

    def release_disbursement(self, request_id: str) -> None:
        """The tranche failed definitively: nothing posts, the headroom comes back, and the
        next proposal for the tranche gets the next attempt (a new request_id).

        Deliberately no owner check (rule M4): any run that has itself read a definitive
        failure of this request_id may release it; an owner check would wedge re-sends."""
        pending = self._pending.pop(request_id, None)
        if pending is not None:
            self.burn_attempt("disburse", pending.loan_id, str(pending.tranche), request_id)

    # Collections

    def week_record(self, loan_id: str, week: str) -> WeekRecord | None:
        return self._weeks.get((loan_id, week))

    def week_records_any(self) -> bool:
        """Any collection closed or held: the period mode is then fixed."""
        return bool(self._weeks) or bool(self._collections)

    def week_records(self, loan_id: str) -> tuple[WeekRecord, ...]:
        return tuple(r for (lid, _), r in sorted(self._weeks.items()) if lid == loan_id)

    def pending_collection(self, loan_id: str) -> PendingCollection | None:
        return self._collections.get(loan_id)

    def next_collectible_week(self, loan_id: str) -> str | None:
        """The only week the loan may be collected for next, or None if it is not funded.

        A collection covers revenue earned after the advance arrived: the first collectible
        week is the first ISO week starting at or after the first tranche settled (and not
        before the loan's first_week). After that, weeks are collected strictly in order, so
        a backlog cannot be drained in one go.
        """
        pending = self._collections.get(loan_id)
        if pending is not None:
            return pending.week
        records = self.week_records(loan_id)
        if records:
            return next_week(max((r.week for r in records), key=lambda w: week_window(w)[0]))
        funded = self.funded_at(loan_id)
        if funded is None:
            return None
        demo = self.config.demo_period_s
        terms = self.loan(loan_id)
        floor = week_window(terms.first_week)[0]
        candidate = first_week_starting_at_or_after(max(funded, floor), demo)
        if demo is None and floor > week_window(candidate)[0]:
            return terms.first_week
        return candidate

    def reserve_collection(self, pending: PendingCollection, *, resend: bool) -> None:
        """As reserve_disbursement: a first send never runs under another run's hold."""
        prior = self._collections.get(pending.loan_id)
        if prior is not None and prior.owner == pending.owner and prior.same_money(pending):
            return  # this proposal's own hold: a retried build (rule 0 idempotency)
        if prior is not None and not resend:
            raise ApprovalBindingError("another run holds this week's charge; propose again")
        if prior is None and resend:
            raise ApprovalBindingError("the in-flight charge this proposal re-sends is gone")
        if prior is not None:
            if not prior.same_money(pending):
                raise DuplicatePosting("a different collection is already in flight")
            return  # a re-send: the hold keeps its original owner
        if self.week_record(pending.loan_id, pending.week) is not None:
            raise DuplicatePosting(f"week {pending.week} of {pending.loan_id} is closed")
        self._collections[pending.loan_id] = pending

    def release_collection(self, loan_id: str, request_id: str) -> None:
        """The charge was refused and its read-back is empty: drop the hold, keep the week open,
        and move the week to its next attempt so the next charge has a fresh request_id.

        Deliberately no owner check (rule M4), as for release_disbursement."""
        pending = self._collections.get(loan_id)
        if pending is not None and pending.request_id == request_id:
            del self._collections[loan_id]
            self.burn_attempt("collect", loan_id, pending.week, request_id)

    def close_week(self, record: WeekRecord) -> bool:
        """Post the week. Identical replay is a no-op; a different replay raises."""
        k = (record.loan_id, record.week)
        prior = self._weeks.get(k)
        if prior is not None:
            if prior == record:
                return False
            raise DuplicatePosting(f"week {record.week} of {record.loan_id} is already closed")
        held = self._collections.get(record.loan_id)
        if (
            held is not None
            and held.week == record.week
            and (record.charge_request_id != held.request_id)
        ):
            # Rule M4: only a run that read a definitive outcome for the held request_id may
            # close the week; a run that sent nothing cannot clear someone else's hold.
            raise LedgerError(f"week {record.week} has a charge in flight on {held.request_id}")
        if record.week != self.next_collectible_week(record.loan_id):
            raise LedgerError(f"week {record.week} is not the loan's next collectible week")
        lid = record.loan_id
        entry = week_close_entry(
            f"collect:{lid}:{record.week}",
            lid,
            due=record.due,
            collected=record.collected,
            principal=self.ledger.balance(Acct.PRINCIPAL_RECEIVABLE, lid),
            fee=self.ledger.balance(Acct.FEE_RECEIVABLE, lid),
            arrears=self.ledger.balance(Acct.DUE_RECEIVABLE, lid),
            provider_id=record.charge_id,
            request_id=record.charge_request_id,
            memo={"week": record.week},
        )
        if entry is not None:
            self.ledger.post(entry)
        self._weeks[k] = record
        pending = self._collections.get(lid)
        if pending is not None and pending.week == record.week:
            del self._collections[lid]
        return True

    def issued_request_ids(self) -> dict[str, tuple[str, str]]:
        """Every request_id this book sent: request_id -> (kind, loan_id). Reconciliation
        reads exactly these, so foreign objects on the shared sandbox are never in scope."""
        out: dict[str, tuple[str, str]] = {}
        for e in self.ledger.entries:
            if e.request_id is None:
                continue
            kind = "transfer" if e.kind is EntryKind.DISBURSEMENT else "charge"
            lid = next(ln.loan_id for ln in e.lines if ln.loan_id is not None)
            out[e.request_id] = (kind, lid)
        for r in self._weeks.values():
            if r.charge_request_id is not None:
                out[r.charge_request_id] = ("charge", r.loan_id)
        for p in self._pending.values():
            out[p.request_id] = ("transfer", p.loan_id)
        for c in self._collections.values():
            out[c.request_id] = ("charge", c.loan_id)
        for rid, kind_loan in self._released.items():
            out.setdefault(rid, kind_loan)
        return out

    # Platform cash

    def usable_cash(self, provider_available: Money) -> Money:
        """min(ledger platform cash, fresh provider balance), net of in-flight tranches.

        The ledger figure keeps another portfolio's (or another kit's) money in the shared
        wallet out of reach; the provider figure stops the ledger promising cash that is not
        there."""
        return min(self.ledger.platform_cash(), provider_available) - self.pending_total()

    # Persistence

    def to_state(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "config": self.config.to_state(),
            "loans": [t.to_state() for t in self.loans()],
            "entries": [e.to_state() for e in self.ledger.entries],
            "weeks": [
                {
                    "loan_id": r.loan_id,
                    "week": r.week,
                    "due": r.due,
                    "collected": r.collected,
                    "charge_request_id": r.charge_request_id,
                    "charge_id": r.charge_id,
                    "charge_status": r.charge_status,
                }
                for _, r in sorted(self._weeks.items())
            ],
            "pending": [
                {
                    "loan_id": p.loan_id,
                    "tranche": p.tranche,
                    "amount": p.amount,
                    "request_id": p.request_id,
                    "owner": p.owner,
                    "placed_at": p.placed_at,
                }
                for p in self._pending.values()
            ],
            "collections": [
                {
                    "loan_id": c.loan_id,
                    "week": c.week,
                    "due": c.due,
                    "amount": c.amount,
                    "request_id": c.request_id,
                    "owner": c.owner,
                    "placed_at": c.placed_at,
                }
                for c in self._collections.values()
            ],
            "tranches": dict(sorted(self._tranches.items())),
            "attempts": dict(sorted(self._attempts.items())),
            "released": [[rid, k, lid] for rid, (k, lid) in sorted(self._released.items())],
            "sizings": dict(sorted(self._sizings.items())),
            "last_reconcile": None
            if self.last_reconcile is None
            else {
                "at": self.last_reconcile.at,
                "ok": self.last_reconcile.ok,
                "breaks": list(self.last_reconcile.breaks),
            },
        }

    @classmethod
    def from_state(cls, d: Mapping[str, Any]) -> LoanBook:
        book = cls(PortfolioConfig.from_state(d["config"]), d["book_id"])
        for t in d["loans"]:
            book.add_loan(LoanTerms.from_state(t))
        book.ledger = Subledger(book.config.currency, (Entry.from_state(e) for e in d["entries"]))
        book._weeks = {(w["loan_id"], w["week"]): WeekRecord(**w) for w in d["weeks"]}
        book._pending = {p["request_id"]: PendingDisbursement(**p) for p in d["pending"]}
        book._collections = {c["loan_id"]: PendingCollection(**c) for c in d["collections"]}
        book._tranches = dict(d["tranches"])
        book._attempts = dict(d["attempts"])
        book._released = {rid: (k, lid) for rid, k, lid in d["released"]}
        book._sizings = {k: dict(v) for k, v in (d.get("sizings") or {}).items()}
        rec = d.get("last_reconcile")
        if rec is not None:
            book.last_reconcile = ReconcileRecord(rec["at"], rec["ok"], tuple(rec["breaks"]))
        return book


def round_down_to(amount: Money, increment: Money) -> Money:
    """Largest multiple of `increment` that is <= amount (amount >= 0)."""
    if amount.is_negative():
        return Money.zero(amount.currency)
    steps = (amount.amount / increment.amount).to_integral_value(rounding=ROUND_DOWN)
    return Money(increment.amount * steps, amount.currency)


# Store access


def mint_book_id() -> str:
    return secrets.token_hex(6)


def open_portfolio(store: KitStore, config: PortfolioConfig) -> LoanBook:
    """Operator step: create an empty book with a freshly minted book id (rule M2). Refuses
    if one exists (VersionConflict)."""
    book = LoanBook(config, mint_book_id())
    store.put(BOOK_KEY, book.to_state(), expected_version=None)
    return book


def load_book(store: KitStore | None) -> tuple[LoanBook, int]:
    if store is None:
        raise PortfolioNotOpen("kit 7 needs PolicyContext.platform_store")
    item = store.get(BOOK_KEY)
    if item is None:
        raise PortfolioNotOpen("no loan portfolio is open in this store")
    return LoanBook.from_state(item.value), item.version


def update_book[T](store: KitStore | None, change: Callable[[LoanBook], T]) -> T:
    """Load, apply `change`, write back at the version read. VersionConflict propagates."""
    book, version = load_book(store)
    result = change(book)
    assert store is not None
    store.put(BOOK_KEY, book.to_state(), expected_version=version)
    return result
