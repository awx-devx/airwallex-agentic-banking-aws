"""Forecast confidence and the approval matrix. Thresholds live here, never in a prompt.

The model may only report a contradiction (with a citation). It can never raise confidence:
HIGH needs bank and invoice corroboration that comes from the book, not from the model.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from awx_common import Money, RiskTier
from awx_common.ops import DEDUP_RESEND_MARGIN

from .book import Forecast, TreasuryBook
from .plan import FUNDED, Plan, PlanConfig


class ConfidenceBand(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


_BAND_ORDER = {ConfidenceBand.LOW: 0, ConfidenceBand.MEDIUM: 1, ConfidenceBand.HIGH: 2}
_TIER_ORDER = {RiskTier.AUTO: 0, RiskTier.SINGLE: 1, RiskTier.DUAL: 2, RiskTier.BLOCKED: 3}


def band_rank(band: ConfidenceBand) -> int:
    return _BAND_ORDER[band]


def tier_rank(tier: RiskTier) -> int:
    return _TIER_ORDER[tier]


def confidence_band(
    forecasts: Collection[Forecast], contradicted: Collection[str]
) -> ConfidenceBand:
    """LOW if any forecast is contradicted; HIGH only if every forecast is bank- and
    invoice-corroborated; otherwise MEDIUM. No forecasts at all is MEDIUM."""
    if any(f.receipt_id in contradicted for f in forecasts):
        return ConfidenceBand.LOW
    if forecasts and all(f.bank_corroborated and f.invoice_matched for f in forecasts):
        return ConfidenceBand.HIGH
    return ConfidenceBand.MEDIUM


def _usd(amount: str) -> Money:
    return Money.of(amount, "USD")


@dataclass(frozen=True)
class PolicyConfig:
    """Approval thresholds. Conversion limits are in the book's funding currency."""

    auto_conversion_limit: Mapping[ConfidenceBand, Money] = field(
        default_factory=lambda: {
            ConfidenceBand.HIGH: _usd("10000"),
            ConfidenceBand.MEDIUM: _usd("2500"),
            ConfidenceBand.LOW: _usd("0"),
        }
    )
    single_approver_max: Money = field(default_factory=lambda: _usd("100000"))
    auto_payout_limit: Mapping[str, Money] = field(
        default_factory=lambda: {
            "USD": _usd("15000"),
            "EUR": Money.of("10000", "EUR"),
            "GBP": Money.of("1000", "GBP"),
        }
    )
    thin_reserve_ratio: Decimal = Decimal("0.10")  # headroom under floor * ratio needs a person
    plan: PlanConfig = field(default_factory=PlanConfig)
    quote_validity: str = "MIN_1"  # required by fx/quotes/create; MIN_1 sandbox-verified
    quote_margin: timedelta = timedelta(seconds=20)  # least validity left when booking
    approval_ttl: timedelta = timedelta(hours=4)
    lock_validity: str = "HR_1"  # rate-lock quote lifetime; HR_1 is sandbox-verified
    lock_approval_ttl: timedelta = timedelta(minutes=15)
    # M1: past the spine's resend margin (awx_common.ops.DEDUP_RESEND_MARGIN, 6 days, just
    # inside the provider's ~7-day dedupe) money still in flight goes to a person.
    dedup_window: timedelta = DEDUP_RESEND_MARGIN

    def __post_init__(self) -> None:
        if set(self.auto_conversion_limit) != set(ConfidenceBand):
            raise ValueError("auto_conversion_limit needs every confidence band")
        limits = [self.auto_conversion_limit[b] for b in ConfidenceBand]
        if any(m.currency != self.single_approver_max.currency for m in limits):
            raise ValueError("conversion limits and single_approver_max need one currency")
        lo = self.auto_conversion_limit[ConfidenceBand.LOW]
        med = self.auto_conversion_limit[ConfidenceBand.MEDIUM]
        hi = self.auto_conversion_limit[ConfidenceBand.HIGH]
        if not lo <= med <= hi <= self.single_approver_max:
            raise ValueError("limits must rise with confidence and stay under the approver max")
        if self.quote_margin <= timedelta(0):
            raise ValueError("quote_margin must be positive")


@dataclass(frozen=True)
class TierResult:
    tier: RiskTier
    reasons: tuple[str, ...]


def approval_tier(
    plan: Plan,
    band: ConfidenceBand,
    book: TreasuryBook,
    config: PolicyConfig,
    *,
    requested: Collection[str],
) -> TierResult:
    """The approval matrix. The strictest rule that applies wins.

    BLOCKED  any requested obligation is not funded by the plan, or the whole plan breaches a
             reserve floor
    DUAL     total exposure (conversion sell ceilings plus funding-currency payouts) above
             single_approver_max
    SINGLE   conversion total above the band's autonomous limit; a payout above its currency's
             autonomous payout limit; funding-wallet headroom under floor * thin_reserve_ratio
    AUTO     otherwise
    """
    reasons: list[str] = []
    tier = RiskTier.AUTO

    def raise_to(new: RiskTier, why: str) -> None:
        nonlocal tier
        reasons.append(why)
        if tier_rank(new) > tier_rank(tier):
            tier = new

    for oid in sorted(requested):
        d = plan.decision(oid)
        if d.kind not in FUNDED:
            raise_to(RiskTier.BLOCKED, f"{oid}: {d.kind.value.lower()} ({d.reason})")
    if plan.breaches:
        raise_to(RiskTier.BLOCKED, f"whole plan breaches the reserve floor in {plan.breaches}")
    if tier is RiskTier.BLOCKED:
        return TierResult(tier, tuple(reasons))

    funding = book.funding_currency
    zero = Money.zero(config.single_approver_max.currency)
    conv = plan.conversion_total or zero
    if conv.currency != funding:
        raise ValueError("approval limits must be in the book's funding currency")
    exposure = conv
    for p in plan.payouts:
        if p.amount.currency == funding:
            exposure = exposure + p.need
    if exposure > config.single_approver_max:
        raise_to(RiskTier.DUAL, f"exposure {exposure} above {config.single_approver_max}")

    auto = config.auto_conversion_limit[band]
    if conv > auto:
        raise_to(
            RiskTier.SINGLE,
            f"conversion up to {conv} exceeds the {band.value} confidence autonomous limit {auto}",
        )
    for p in plan.payouts:
        limit = config.auto_payout_limit.get(p.amount.currency)
        if limit is None or p.amount > limit:
            raise_to(
                RiskTier.SINGLE, f"{p.obligation_id}: payout {p.amount} above autonomous limit"
            )
    # The thin-reserve rule watches the funding wallet only: a bought currency deliberately
    # lands on its floor, because the plan converts the minimum.
    if plan.conversions or any(p.amount.currency == funding for p in plan.payouts):
        floor = book.floors[funding]
        cushion = floor.scale(config.thin_reserve_ratio, rounding=ROUND_CEILING)
        if plan.projected[funding] - floor < cushion:
            raise_to(RiskTier.SINGLE, f"{funding} would end within {cushion} of its reserve floor")
    if not reasons:
        reasons.append(f"within {band.value} confidence autonomous limits")
    return TierResult(tier, tuple(reasons))
