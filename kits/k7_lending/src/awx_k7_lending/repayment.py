"""The weekly repayment formula (an AWS-authored assumption: the guide gives none).

The guide says borrowers repay "a share of that week's revenue" and gives no formula. We use a
bounded revenue share:

    share     = round_down(share_rate * weekly_revenue)
    due       = min(max(share, weekly_floor), weekly_cap, outstanding)
    collected = min(due, max(available_wallet_balance, 0))
    shortfall = due - collected          -> arrears in the subledger, never re-charged

Why this shape:

- Revenue share is what the guide describes, and it makes repayment track the borrower's cash
  flow, which is the point of a revenue-based advance.
- The weekly cap bounds every single charge. A shortfall becomes arrears and is recovered only
  through later weeks' normal formula, so a catch-up charge larger than the cap cannot exist.
- The weekly floor keeps the advance amortising in a zero-revenue week, so the term is finite.
- Every rounding is ROUND_DOWN to the currency's minor unit: the code may under-collect by less
  than one minor unit in a week, never over-collect. Rounding happens once, on the share; the
  min/max steps compare exact Money values and cannot create or lose a cent.

All inputs are Money or Decimal. Nothing here reads, writes or guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from awx_common import Money

SHARE_ROUNDING = ROUND_DOWN


@dataclass(frozen=True)
class RepaymentTerms:
    """Per-loan repayment parameters. Rates are exact Decimals in [0, 1]."""

    share_rate: Decimal
    weekly_cap: Money
    weekly_floor: Money

    def __post_init__(self) -> None:
        if isinstance(self.share_rate, float | bool) or not isinstance(self.share_rate, Decimal):
            raise TypeError("share_rate must be a Decimal")
        if not Decimal(0) < self.share_rate <= Decimal(1):
            raise ValueError("share_rate must be in (0, 1]")
        if self.weekly_cap.currency != self.weekly_floor.currency:
            raise ValueError("cap and floor must share a currency")
        if self.weekly_floor.is_negative() or self.weekly_cap.amount <= 0:
            raise ValueError("need 0 <= floor and 0 < cap")
        if self.weekly_floor > self.weekly_cap:
            raise ValueError("weekly_floor must not exceed weekly_cap")

    @property
    def currency(self) -> str:
        return self.weekly_cap.currency


@dataclass(frozen=True)
class WeeklyCollection:
    """The code's answer for one loan and one week."""

    revenue: Money
    share: Money
    due: Money
    available: Money
    collect: Money
    shortfall: Money


def weekly_due(revenue: Money, outstanding: Money, terms: RepaymentTerms) -> tuple[Money, Money]:
    """Return (share, due). due never exceeds the cap or what is still owed."""
    if revenue.is_negative():
        raise ValueError("weekly revenue cannot be negative")
    if outstanding.is_negative():
        raise ValueError("outstanding cannot be negative")
    share = revenue.scale(terms.share_rate, rounding=SHARE_ROUNDING)
    bounded = min(max(share, terms.weekly_floor), terms.weekly_cap)
    return share, min(bounded, outstanding)


def plan_collection(
    revenue: Money, outstanding: Money, available: Money, terms: RepaymentTerms
) -> WeeklyCollection:
    """Size this week's charge. A short wallet lowers the charge; it never raises a later one."""
    share, due = weekly_due(revenue, outstanding, terms)
    usable = max(available, Money.zero(available.currency))
    collect = min(due, usable)
    return WeeklyCollection(
        revenue=revenue,
        share=share,
        due=due,
        available=available,
        collect=collect,
        shortfall=due - collect,
    )
