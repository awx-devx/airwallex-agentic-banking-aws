"""Portfolio reconciliation invariants (an AWS-authored assumption: the guide gives none).

Reconciliation is driven by this book's own records: it reads back exactly the request_ids
the book sent, by `?request_id=` (FAKE-FIDELITY rows 51-52). It never lists the shared sandbox
account, so another run's `poc-k7-` objects are out of scope and a clean book reconciles clean.

  R1  The subledger balances: the trial balance is zero and the cached balances equal a
      fresh recomputation from the entries.
  R2  Per loan, no receivable or unearned bucket is negative; fee income plus unearned fee
      equals the fee charged on settled tranches; principal + fee + arrears equals
      disbursed + fee charged - collected.
  R3  Per loan, cash collected in the ledger equals the sum of the loan's closed week records.
  R4  Every request_id the book sent resolves to at most one Airwallex object, of the right
      kind, and it is consistent with the book:
        posted with cash      -> the object is SETTLED, same id, same amount, the loan's own
                                 borrower as counterparty;
        posted as arrears     -> nothing SETTLED under that request_id;
        not posted            -> the object is not SETTLED (a settled-but-unposted movement
                                 is a break), unless it is a charge whose week is still open.
      Per loan, the sum of settled charge amounts equals ledger cash collected.
  R5  The platform wallet backs the portfolio: provider available balance >= ledger
      PLATFORM_CASH. The sandbox account is shared, so equality cannot be required.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from awx_common import Money, ReadRequest

from .book import LoanBook
from .ledger import Acct, EntryKind, sum_lines
from .provider import find_by_request_id


@dataclass(frozen=True)
class Reconciliation:
    ok: bool
    breaks: tuple[str, ...]
    ledger_cash: Money
    provider_available: Money | None


def _collected(book: LoanBook, loan_id: str) -> Money:
    total = Money.zero(book.config.currency)
    for e in book.ledger.entries:
        if e.kind is EntryKind.WEEK_CLOSE and any(ln.loan_id == loan_id for ln in e.lines):
            for ln in e.lines:
                if ln.account is Acct.PLATFORM_CASH:
                    total = total + ln.amount
    return total


def _fee_charged(book: LoanBook, loan_id: str) -> Money:
    total = Money.zero(book.config.currency)
    for e in book.ledger.entries:
        if e.kind is EntryKind.DISBURSEMENT:
            for ln in e.lines:
                if ln.account is Acct.FEE_RECEIVABLE and ln.loan_id == loan_id:
                    total = total + ln.amount
    return total


def reconcile_ledger(book: LoanBook) -> list[str]:
    """R1 to R3: internal consistency, no provider data needed."""
    led = book.ledger
    cur = led.currency
    breaks: list[str] = []
    if not led.trial_balance().is_zero():
        breaks.append(f"R1 trial balance is {led.trial_balance()}")
    accounts = {(ln.account, ln.loan_id) for e in led.entries for ln in e.lines}
    for account, loan_id in sorted(accounts, key=lambda k: (k[0], k[1] or "")):
        if sum_lines(led.entries, cur, account, loan_id) != led.balance(account, loan_id):
            breaks.append(f"R1 cached {account}/{loan_id} differs from entries")
    for terms in book.loans():
        lid = terms.loan_id
        p = led.balance(Acct.PRINCIPAL_RECEIVABLE, lid)
        f = led.balance(Acct.FEE_RECEIVABLE, lid)
        a = led.balance(Acct.DUE_RECEIVABLE, lid)
        unearned = -led.balance(Acct.UNEARNED_FEE, lid)
        income = -led.balance(Acct.FEE_INCOME, lid)
        for name, value in (("principal", p), ("fee", f), ("arrears", a), ("unearned", unearned)):
            if value.is_negative():
                breaks.append(f"R2 {lid} {name} is negative ({value})")
        charged = _fee_charged(book, lid)
        if income + unearned != charged:
            breaks.append(f"R2 {lid} fee income + unearned fee != fee charged {charged}")
        collected = _collected(book, lid)
        if p + f + a != book.disbursed(lid) + charged - collected:
            breaks.append(f"R2 {lid} outstanding does not equal disbursed + fee - collected")
        weeks = sum((r.collected for r in book.week_records(lid)), Money.zero(cur))
        if weeks != collected:
            breaks.append(f"R3 {lid} week records collected {weeks}, ledger has {collected}")
    return breaks


def reconcile_provider(
    book: LoanBook,
    read: Callable[[ReadRequest], Mapping[str, Any]],
    provider_available: Money | None,
) -> list[str]:
    """R4 and R5 against headerless platform reads."""
    led = book.ledger
    cur = led.currency
    breaks: list[str] = []
    posted = {e.request_id: e for e in led.entries if e.request_id is not None}
    settled_charges: dict[str, Money] = {}
    for rid, (kind, lid) in sorted(book.issued_request_ids().items()):
        try:
            found = find_by_request_id(read, kind, rid, cur)
        except Exception as exc:  # an ambiguous read-back is itself a break
            breaks.append(f"R4 {rid}: {exc}")
            continue
        entry = posted.get(rid)
        borrower = book.loan(lid).borrower_account
        if found is not None and found.status == "SETTLED" and kind == "charge":
            settled_charges[lid] = settled_charges.get(lid, Money.zero(cur)) + found.amount
        if entry is None:
            week_open = kind == "charge" and (
                (pc := book.pending_collection(lid)) is not None and pc.request_id == rid
            )
            if found is not None and found.status == "SETTLED" and not week_open:
                breaks.append(f"R4 {found.provider_id} ({rid}) settled but not in the ledger")
            continue
        cash = sum(
            (ln.amount for ln in entry.lines if ln.account is Acct.PLATFORM_CASH),
            Money.zero(cur),
        )
        want = -cash if kind == "transfer" else cash
        if want.is_zero():
            # A week closed as arrears: its charge was refused or failed, so nothing may
            # have settled under its request_id.
            if found is not None and found.status == "SETTLED":
                breaks.append(f"R4 {entry.key} books no cash but {found.provider_id} SETTLED")
            continue
        if found is None:
            breaks.append(f"R4 {entry.key} posts {rid}, which Airwallex does not have")
            continue
        if found.status != "SETTLED":
            breaks.append(f"R4 {entry.key} is posted but {found.provider_id} is {found.status}")
        if found.provider_id != entry.provider_id:
            breaks.append(
                f"R4 {entry.key} names {entry.provider_id}, Airwallex has {found.provider_id}"
            )
        if found.amount != want:
            breaks.append(f"R4 {entry.key} posts {want} but Airwallex shows {found.amount}")
        if found.counterparty != borrower:
            breaks.append(f"R4 {found.provider_id} counterparty is not the loan's borrower")
    for terms in book.loans():
        lid = terms.loan_id
        provider_total = settled_charges.get(lid, Money.zero(cur))
        if provider_total != _collected(book, lid) and book.pending_collection(lid) is None:
            breaks.append(f"R4 {lid} settled charges {provider_total} != ledger collected")
    if provider_available is not None and provider_available < led.platform_cash():
        breaks.append(
            f"R5 platform wallet {provider_available} is below ledger cash {led.platform_cash()}"
        )
    return breaks


def reconcile(
    book: LoanBook,
    read: Callable[[ReadRequest], Mapping[str, Any]],
    provider_available: Money | None,
) -> Reconciliation:
    breaks = reconcile_ledger(book) + reconcile_provider(book, read, provider_available)
    return Reconciliation(
        ok=not breaks,
        breaks=tuple(breaks),
        ledger_cash=book.ledger.platform_cash(),
        provider_available=provider_available,
    )
