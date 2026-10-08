"""Card controls: the enforceable form of the purchase decision.

Only card controls can block an authorization, so this is where the policy lives. A control
set always has an inclusive PER_TRANSACTION limit AND at least one cumulative limit (MONTHLY or
ALL_TIME), because Airwallex PER_TRANSACTION limits are not cumulative (guide, Sandbox
constraints > Cards). A card is single-currency: every limit is in the one allowed currency, so
no limit check ever needs an FX rate.

`control_violations` is our local model of the documented decline rules. It is used to design
probes that each isolate exactly one rule, and to explain a decline. It never replaces the
provider: the provider's failure_reason is the evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from awx_common import Money

from .amounts import parse_money

_MCC = re.compile(r"^[0-9]{4}$")


class Interval(StrEnum):
    PER_TRANSACTION = "PER_TRANSACTION"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    ALL_TIME = "ALL_TIME"


CUMULATIVE_REQUIRED = frozenset({Interval.MONTHLY, Interval.ALL_TIME})
CUMULATIVE = frozenset({Interval.DAILY, Interval.WEEKLY, Interval.MONTHLY, Interval.ALL_TIME})


class DeclineReason(StrEnum):
    """Sandbox decline reasons the guide lists (Sandbox constraints > Cards)."""

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    CURRENCY_NOT_ALLOWED = "CURRENCY_NOT_ALLOWED"
    MERCHANT_CATEGORY_NOT_ALLOWED = "MERCHANT_CATEGORY_NOT_ALLOWED"
    OUT_OF_ALLOWED_TIME_RANGE = "OUT_OF_ALLOWED_TIME_RANGE"
    CARD_INACTIVE = "CARD_INACTIVE"
    CARD_CLOSED = "CARD_CLOSED"


@dataclass(frozen=True)
class SpendLimit:
    interval: Interval
    amount: Money


@dataclass(frozen=True)
class CardControls:
    currency: str
    allowed_merchant_categories: tuple[str, ...]
    limits: tuple[SpendLimit, ...]
    single_use: bool = False

    def __post_init__(self) -> None:
        mccs = tuple(sorted(set(self.allowed_merchant_categories)))
        if not mccs:
            raise ValueError("at least one merchant category is required")
        for mcc in mccs:
            if not _MCC.match(mcc):
                raise ValueError(f"merchant category {mcc!r} must be a 4-digit MCC")
        object.__setattr__(self, "allowed_merchant_categories", mccs)
        intervals = [lim.interval for lim in self.limits]
        if len(intervals) != len(set(intervals)):
            raise ValueError("one limit per interval")
        order = list(Interval)
        limits = tuple(sorted(self.limits, key=lambda lim: order.index(lim.interval)))
        object.__setattr__(self, "limits", limits)
        for lim in limits:
            if lim.amount.currency != self.currency:
                raise ValueError("every limit must be in the card currency")
            if lim.amount.is_negative() or lim.amount.is_zero():
                raise ValueError("limits must be positive")
        per_txn = self.limit(Interval.PER_TRANSACTION)
        if per_txn is None:
            raise ValueError("a PER_TRANSACTION limit is required")
        if not CUMULATIVE_REQUIRED & set(intervals):
            raise ValueError(
                "a cumulative MONTHLY or ALL_TIME limit is required: PER_TRANSACTION alone "
                "does not cap total spend"
            )
        for lim in limits:
            if lim.interval in CUMULATIVE and lim.amount < per_txn:
                raise ValueError(f"{lim.interval} limit is below the per-transaction limit")

    @property
    def allowed_currencies(self) -> tuple[str, ...]:
        return (self.currency,)

    def limit(self, interval: Interval) -> Money | None:
        for lim in self.limits:
            if lim.interval is interval:
                return lim.amount
        return None

    def cumulative_limits(self) -> tuple[SpendLimit, ...]:
        return tuple(lim for lim in self.limits if lim.interval in CUMULATIVE)

    # ---- canonical form (intent, approval display, computed) -------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "allowed_currencies": list(self.allowed_currencies),
            "allowed_merchant_categories": list(self.allowed_merchant_categories),
            "limits": [
                {"interval": lim.interval.value, "amount": format(lim.amount.amount, "f")}
                for lim in self.limits
            ],
            "single_use": self.single_use,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CardControls:
        currency = d["currency"]
        if list(d.get("allowed_currencies", [currency])) != [currency]:
            raise ValueError("a kit 2 card allows exactly its own currency")
        return cls(
            currency=currency,
            allowed_merchant_categories=tuple(d["allowed_merchant_categories"]),
            limits=tuple(
                SpendLimit(Interval(x["interval"]), parse_money(x["amount"], currency))
                for x in d["limits"]
            ),
            single_use=bool(d.get("single_use", False)),
        )

    # ---- provider form ---------------------------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        """`authorization_controls` for POST /issuing/cards/create and /update.

        Field names were verified in the sandbox (FAKE-FIDELITY.md, Issuing); the guide only
        says "limits, a currency allowlist and merchant categories" (DESIGN.md, Guide
        ambiguities).
        """
        return {
            "allowed_currencies": list(self.allowed_currencies),
            "allowed_merchant_categories": list(self.allowed_merchant_categories),
            "allowed_transaction_count": "SINGLE" if self.single_use else "MULTIPLE",
            "transaction_limits": {
                "currency": self.currency,
                "limits": [
                    {"amount": lim.amount.amount, "interval": lim.interval.value}
                    for lim in self.limits
                ],
            },
        }

    @classmethod
    def from_provider(cls, controls: Mapping[str, Any]) -> CardControls:
        """Parse `authorization_controls` from a card read. Fails closed on any surprise."""
        tl = controls.get("transaction_limits") or {}
        currency = tl.get("currency")
        if not isinstance(currency, str):
            raise ValueError("card has no transaction_limits currency")
        allowed = list(controls.get("allowed_currencies") or [])
        if allowed != [currency]:
            raise ValueError(f"card allows currencies {allowed}, expected exactly [{currency}]")
        return cls(
            currency=currency,
            allowed_merchant_categories=tuple(controls.get("allowed_merchant_categories") or ()),
            limits=tuple(
                SpendLimit(Interval(x["interval"]), parse_money(x["amount"], currency))
                for x in tl.get("limits") or ()
            ),
            single_use=controls.get("allowed_transaction_count") == "SINGLE",
        )

    def reduces_or_equals(self, other: CardControls) -> bool:
        """True when self grants no more authority than `other` in any dimension."""
        if self.currency != other.currency:
            return False
        if not set(self.allowed_merchant_categories) <= set(other.allowed_merchant_categories):
            return False
        if other.single_use and not self.single_use:
            return False
        for lim in other.limits:
            mine = self.limit(lim.interval)
            if mine is None or mine > lim.amount:
                return False
        return True


@dataclass(frozen=True)
class SpendSoFar:
    """Spend already counted against each cumulative interval, in the card currency."""

    by_interval: Mapping[Interval, Money]

    def of(self, interval: Interval, currency: str) -> Money:
        return self.by_interval.get(interval, Money.zero(currency))


def control_violations(
    controls: CardControls,
    amount: Money,
    merchant_category: str,
    spent: SpendSoFar | None = None,
) -> frozenset[DeclineReason]:
    """Every control an authorization would break. Limits are inclusive: equal clears."""
    out: set[DeclineReason] = set()
    if amount.currency not in controls.allowed_currencies:
        out.add(DeclineReason.CURRENCY_NOT_ALLOWED)
    if merchant_category not in controls.allowed_merchant_categories:
        out.add(DeclineReason.MERCHANT_CATEGORY_NOT_ALLOWED)
    if amount.currency == controls.currency:
        spent = spent or SpendSoFar({})
        for lim in controls.limits:
            used = (
                Money.zero(controls.currency)
                if lim.interval is Interval.PER_TRANSACTION
                else spent.of(lim.interval, controls.currency)
            )
            if used + amount > lim.amount:
                out.add(DeclineReason.LIMIT_EXCEEDED)
    return frozenset(out)


def merchant_categories_for(category: str, table: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    try:
        return tuple(table[category])
    except KeyError:
        raise ValueError(f"unknown purchase category {category!r}") from None
