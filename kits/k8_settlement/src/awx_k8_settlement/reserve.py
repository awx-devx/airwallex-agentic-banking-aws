"""Reserve policy: the percentage, its bounds, and what counts as a material increase.

The model never chooses a reserve. It may summarise carrier evidence, but the reserve target is
always max(rate target, incident exposure), both computed here from ledger and order data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal

from awx_common import Money


@dataclass(frozen=True)
class ReservePolicy:
    """Policy constants. Amounts are major units of the seller's currency.

    rate = clamp(max(trailing_refund_rate * refund_multiplier, tenure floor), 0, max_rate).
    The tenure floor falls as a seller ages, which is how the newest seller gets the highest
    reserve (the guide's instruction) without the model ranking sellers.
    """

    refund_multiplier: Decimal = Decimal(2)
    # (max tenure in days, floor rate), checked in order; older sellers get base_floor.
    tenure_floors: tuple[tuple[int, Decimal], ...] = field(
        default=((90, Decimal("0.20")), (365, Decimal("0.10")))
    )
    base_floor: Decimal = Decimal("0.05")
    max_rate: Decimal = Decimal("0.30")
    # A reserve increase is material when it exceeds EITHER threshold.
    material_increase_abs: Decimal = Decimal(500)
    material_increase_ratio: Decimal = Decimal("0.25")
    # Any single payout, recovery charge or reserve increase at or above this needs two approvers.
    dual_approval_abs: Decimal = Decimal(10000)
    # Net an outstanding receivable against the next payout (with approval) instead of charging.
    net_receivables: bool = True

    def __post_init__(self) -> None:
        rates = [self.refund_multiplier, self.base_floor, self.max_rate]
        rates += [r for _, r in self.tenure_floors]
        if any(isinstance(r, float) or not isinstance(r, Decimal) for r in rates):
            raise TypeError("reserve policy rates must be Decimal")
        if not Decimal(0) <= self.base_floor <= self.max_rate <= Decimal(1):
            raise ValueError("need 0 <= base_floor <= max_rate <= 1")
        days = [d for d, _ in self.tenure_floors]
        if days != sorted(days) or any(
            not self.base_floor <= r <= self.max_rate for _, r in self.tenure_floors
        ):
            raise ValueError("tenure floors must be ascending and within [base_floor, max_rate]")


DEFAULT_POLICY = ReservePolicy()


def tenure_floor(policy: ReservePolicy, tenure_days: int) -> Decimal:
    if tenure_days < 0:
        raise ValueError("tenure_days must be non-negative")
    for max_days, rate in policy.tenure_floors:
        if tenure_days < max_days:
            return rate
    return policy.base_floor


def reserve_rate(policy: ReservePolicy, trailing_refund_rate: Decimal, tenure_days: int) -> Decimal:
    if isinstance(trailing_refund_rate, float) or not isinstance(trailing_refund_rate, Decimal):
        raise TypeError("trailing_refund_rate must be a Decimal")
    if not Decimal(0) <= trailing_refund_rate <= Decimal(1):
        raise ValueError("trailing_refund_rate must be within [0, 1]")
    raw = max(trailing_refund_rate * policy.refund_multiplier, tenure_floor(policy, tenure_days))
    return min(raw, policy.max_rate)


def rate_target(volume_base: Money, rate: Decimal) -> Money:
    """Reserve against the trailing volume, rounded UP so rounding never under-reserves."""
    if volume_base.is_negative():
        raise ValueError("volume base must be non-negative")
    return volume_base.scale(rate, rounding=ROUND_UP)


def incident_target(rate_based: Money, exposure: Money) -> Money:
    """New evidence can only raise a reserve: hold at least the whole evidenced exposure."""
    return exposure if exposure > rate_based else rate_based


def reserve_increase(before: Money, after: Money) -> Money:
    delta = after - before
    return delta if delta > Money.zero(delta.currency) else Money.zero(delta.currency)


def is_material_increase(policy: ReservePolicy, before: Money, after: Money) -> bool:
    inc = reserve_increase(before, after)
    if inc.is_zero():
        return False
    if inc.amount > policy.material_increase_abs:
        return True
    if before.is_zero():
        return True  # any increase from nothing is a new hold on the seller's cash
    return inc.amount > before.amount * policy.material_increase_ratio


def needs_dual(policy: ReservePolicy, *amounts: Money) -> bool:
    return any(a.amount >= policy.dual_approval_abs for a in amounts)
