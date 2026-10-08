"""The loan subledger (an AWS-authored assumption: the guide does not specify one).

Double-entry, one currency per portfolio. A line's amount is signed: positive is a debit,
negative a credit, and every entry sums to exactly zero.

Accounts:

    PLATFORM_CASH        asset     portfolio cash held in the platform's own Airwallex wallet
    PORTFOLIO_CAPITAL    equity    capital the platform committed to this portfolio
    PRINCIPAL_RECEIVABLE asset     per loan: advance principal not yet due
    FEE_RECEIVABLE       asset     per loan: the fixed advance fee not yet due
    DUE_RECEIVABLE       asset     per loan: amounts that fell due and were not collected
                                   (arrears)
    UNEARNED_FEE         liability per loan: fee charged but not yet earned
    FEE_INCOME           income    per loan: fee earned as it falls due

Entries:

    CAPITAL      Dr PLATFORM_CASH / Cr PORTFOLIO_CAPITAL
    DISBURSEMENT Dr PRINCIPAL + Dr FEE_RECEIVABLE / Cr PLATFORM_CASH + Cr UNEARNED_FEE
                 (posted only after the connected_account_transfer reads SETTLED)
    WEEK_CLOSE   one per loan per week, posted once the week's charge is terminal (or when
                 no charge was sent): the week's due moves out of PRINCIPAL and FEE (pro rata,
                 largest remainder) into DUE_RECEIVABLE, the fee part billed moves from
                 UNEARNED_FEE to FEE_INCOME, then the settled collection moves from
                 DUE_RECEIVABLE into PLATFORM_CASH. Whatever stays in DUE_RECEIVABLE is
                 arrears, so a shortfall is a receivable, never a second charge.

Fee income is recognised only as the fee falls due, so a loan that stops paying does not
overstate income.

Entries are keyed. Posting the same key with identical lines is a no-op (duplicate delivery);
the same key with different lines raises DuplicatePosting (replay or tampering).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from awx_common import Money
from awx_common.intent import canonical_json


class Acct(StrEnum):
    PLATFORM_CASH = "PLATFORM_CASH"
    PORTFOLIO_CAPITAL = "PORTFOLIO_CAPITAL"
    PRINCIPAL_RECEIVABLE = "PRINCIPAL_RECEIVABLE"
    FEE_RECEIVABLE = "FEE_RECEIVABLE"
    DUE_RECEIVABLE = "DUE_RECEIVABLE"
    UNEARNED_FEE = "UNEARNED_FEE"
    FEE_INCOME = "FEE_INCOME"


PORTFOLIO_ACCOUNTS = frozenset({Acct.PLATFORM_CASH, Acct.PORTFOLIO_CAPITAL})
LOAN_ACCOUNTS = frozenset(set(Acct) - PORTFOLIO_ACCOUNTS)


class EntryKind(StrEnum):
    CAPITAL = "CAPITAL"
    DISBURSEMENT = "DISBURSEMENT"
    WEEK_CLOSE = "WEEK_CLOSE"


class LedgerError(Exception):
    """The subledger refused an entry."""


class DuplicatePosting(LedgerError):
    """A key was posted before with different lines."""


@dataclass(frozen=True)
class Line:
    account: Acct
    loan_id: str | None
    amount: Money  # signed: + debit, - credit

    def __post_init__(self) -> None:
        if self.account in PORTFOLIO_ACCOUNTS and self.loan_id is not None:
            raise LedgerError(f"{self.account} is a portfolio account; it takes no loan_id")
        if self.account in LOAN_ACCOUNTS and not self.loan_id:
            raise LedgerError(f"{self.account} needs a loan_id")


@dataclass(frozen=True)
class Entry:
    key: str
    kind: EntryKind
    lines: tuple[Line, ...]
    provider_id: str | None = None  # the Airwallex object this entry reflects, if any
    request_id: str | None = None  # the request_id this book sent for that object
    memo: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key:
            raise LedgerError("entry key is required")
        lines = tuple(line for line in self.lines if not line.amount.is_zero())
        if not lines:
            raise LedgerError("an entry needs at least one non-zero line")
        currencies = {line.amount.currency for line in lines}
        if len(currencies) != 1:
            raise LedgerError("an entry is single-currency")
        total = sum((line.amount for line in lines), Money.zero(currencies.pop()))
        if not total.is_zero():
            raise LedgerError(f"entry {self.key} does not balance: {total}")
        object.__setattr__(self, "lines", lines)
        object.__setattr__(self, "memo", dict(self.memo))

    @property
    def currency(self) -> str:
        return self.lines[0].amount.currency

    def fingerprint(self) -> str:
        return canonical_json(
            {
                "key": self.key,
                "kind": self.kind,
                "provider_id": self.provider_id,
                "request_id": self.request_id,
                "lines": [[ln.account, ln.loan_id, ln.amount] for ln in self.lines],
            }
        )

    def to_state(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "provider_id": self.provider_id,
            "request_id": self.request_id,
            "memo": dict(self.memo),
            "lines": [
                {"account": ln.account.value, "loan_id": ln.loan_id, "amount": ln.amount}
                for ln in self.lines
            ],
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> Entry:
        return cls(
            key=data["key"],
            kind=EntryKind(data["kind"]),
            provider_id=data.get("provider_id"),
            request_id=data.get("request_id"),
            memo=dict(data.get("memo") or {}),
            lines=tuple(
                Line(Acct(ln["account"]), ln["loan_id"], ln["amount"]) for ln in data["lines"]
            ),
        )


class Subledger:
    """Append-only. Persisted by LoanBook through the kit's KitStore."""

    def __init__(self, currency: str, entries: Iterable[Entry] = ()) -> None:
        self.currency = currency
        self._entries: list[Entry] = []
        self._by_key: dict[str, Entry] = {}
        self._balances: dict[tuple[Acct, str | None], Money] = {}
        for entry in entries:
            self.post(entry)

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def has(self, key: str) -> bool:
        return key in self._by_key

    def get(self, key: str) -> Entry | None:
        return self._by_key.get(key)

    def post(self, entry: Entry) -> bool:
        """Append an entry. Returns False if this exact entry was already posted."""
        if entry.currency != self.currency:
            raise LedgerError(f"ledger is {self.currency}, entry is {entry.currency}")
        prior = self._by_key.get(entry.key)
        if prior is not None:
            if prior.fingerprint() == entry.fingerprint():
                return False
            raise DuplicatePosting(f"key {entry.key} already posted with different lines")
        for line in entry.lines:
            k = (line.account, line.loan_id)
            self._balances[k] = self.balance(line.account, line.loan_id) + line.amount
        self._entries.append(entry)
        self._by_key[entry.key] = entry
        return True

    def balance(self, account: Acct, loan_id: str | None = None) -> Money:
        """Signed (debit-positive) balance."""
        return self._balances.get((account, loan_id), Money.zero(self.currency))

    def trial_balance(self) -> Money:
        return sum(self._balances.values(), Money.zero(self.currency))

    def outstanding(self, loan_id: str) -> Money:
        return (
            self.balance(Acct.PRINCIPAL_RECEIVABLE, loan_id)
            + self.balance(Acct.FEE_RECEIVABLE, loan_id)
            + self.balance(Acct.DUE_RECEIVABLE, loan_id)
        )

    def arrears(self, loan_id: str) -> Money:
        return self.balance(Acct.DUE_RECEIVABLE, loan_id)

    def platform_cash(self) -> Money:
        return self.balance(Acct.PLATFORM_CASH)


# Entry builders. Pure: they read the balances passed in and return an Entry.


def capital_entry(key: str, amount: Money, *, provider_id: str | None = None) -> Entry:
    if amount.amount <= 0:
        raise LedgerError("capital must be positive")
    return Entry(
        key=key,
        kind=EntryKind.CAPITAL,
        provider_id=provider_id,
        lines=(
            Line(Acct.PLATFORM_CASH, None, amount),
            Line(Acct.PORTFOLIO_CAPITAL, None, -amount),
        ),
    )


def disbursement_entry(
    key: str,
    loan_id: str,
    principal: Money,
    fee: Money,
    *,
    provider_id: str,
    request_id: str,
    settled_at: str,
) -> Entry:
    if principal.amount <= 0 or fee.is_negative():
        raise LedgerError("principal must be positive and fee non-negative")
    return Entry(
        key=key,
        kind=EntryKind.DISBURSEMENT,
        provider_id=provider_id,
        request_id=request_id,
        memo={"settled_at": settled_at},
        lines=(
            Line(Acct.PRINCIPAL_RECEIVABLE, loan_id, principal),
            Line(Acct.FEE_RECEIVABLE, loan_id, fee),
            Line(Acct.PLATFORM_CASH, None, -principal),
            Line(Acct.UNEARNED_FEE, loan_id, -fee),
        ),
    )


def split_due(due: Money, principal: Money, fee: Money) -> tuple[Money, Money]:
    """Split a billed amount between principal and fee, pro rata, summing exactly to `due`.

    `due` must not exceed principal + fee. Neither part exceeds its bucket.
    """
    if due.is_negative() or principal.is_negative() or fee.is_negative():
        raise LedgerError("split_due takes non-negative amounts")
    if due > principal + fee:
        raise LedgerError("cannot bill more than principal plus fee")
    if due.is_zero():
        return Money.zero(due.currency), Money.zero(due.currency)
    p_part, f_part = due.allocate([principal.to_minor(), fee.to_minor()])
    return p_part, f_part


def week_close_entry(
    key: str,
    loan_id: str,
    *,
    due: Money,
    collected: Money,
    principal: Money,
    fee: Money,
    arrears: Money,
    provider_id: str | None,
    request_id: str | None,
    memo: Mapping[str, str] | None = None,
) -> Entry | None:
    """Bill the week's due, then apply the settled collection.

    principal, fee and arrears are the loan's balances before this entry. Only the part of the
    due still sitting in principal or fee is billed; any rest is already in arrears. Returns
    None when nothing is billed or collected (the book still records the week).
    """
    if collected.is_negative() or collected > due:
        raise LedgerError("collected must be between 0 and the week's due")
    if due > principal + fee + arrears:
        raise LedgerError("due exceeds what the loan still owes")
    billed = min(due, principal + fee)
    p_part, f_part = split_due(billed, principal, fee)
    lines = [
        Line(Acct.PRINCIPAL_RECEIVABLE, loan_id, -p_part),
        Line(Acct.FEE_RECEIVABLE, loan_id, -f_part),
        Line(Acct.DUE_RECEIVABLE, loan_id, billed - collected),
        Line(Acct.PLATFORM_CASH, None, collected),
        Line(Acct.UNEARNED_FEE, loan_id, f_part),
        Line(Acct.FEE_INCOME, loan_id, -f_part),
    ]
    if all(line.amount.is_zero() for line in lines):
        return None
    return Entry(
        key=key,
        kind=EntryKind.WEEK_CLOSE,
        provider_id=provider_id,
        request_id=request_id,
        lines=tuple(lines),
        memo=dict(memo or {}),
    )


def sum_lines(
    entries: Iterable[Entry], currency: str, account: Acct, loan_id: str | None = None
) -> Money:
    """Recompute a balance from entries (used by reconciliation, independent of the cache)."""
    total = Money.zero(currency)
    for entry in entries:
        for line in entry.lines:
            if line.account is account and line.loan_id == loan_id:
                total = total + line.amount
    return total
