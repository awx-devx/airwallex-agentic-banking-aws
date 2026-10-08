"""Dispute economics: is contesting worth it? Pure Decimal arithmetic, rounded explicitly.

    cost_accept           = A + H_accept (+ F when fee_on_accept)
    cost_contest_best     = F + H_contest                     (win)
    cost_contest_worst    = F + H_contest + A                 (lose)
    cost_contest_expected = F + H_contest + (1 - p) * A       (rounded half-even to minor units)
    saving                = cost_accept - cost_contest_expected

A is the disputed amount from the provider read; F, H and p come from DisputeConfig and the
code-derived strength band. F and H are MODELLED: the sandbox never deducts them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from awx_common import Money

from .config import BAND_ORDER, CurrencyTerms, DisputeConfig, StrengthBand


class Recommendation(StrEnum):
    CONTEST = "CONTEST"
    ACCEPT = "ACCEPT"
    REFER = "REFER"


@dataclass(frozen=True)
class Economics:
    disputed_amount: Money
    dispute_fee_modelled: Money
    contest_handling_modelled: Money
    accept_handling_modelled: Money
    win_probability: Decimal
    band: StrengthBand
    cost_accept: Money
    cost_contest_best: Money
    cost_contest_worst: Money
    cost_contest_expected: Money
    expected_saving: Money
    contest_can_beat_accept: bool  # even a certain win saves money
    recommendation: Recommendation
    reasons: tuple[str, ...]

    def to_computed(self) -> dict[str, object]:
        return {
            "disputed_amount": self.disputed_amount,
            "dispute_fee_modelled": self.dispute_fee_modelled,
            "contest_handling_modelled": self.contest_handling_modelled,
            "accept_handling_modelled": self.accept_handling_modelled,
            "win_probability": self.win_probability,
            "band": self.band.value,
            "cost_accept": self.cost_accept,
            "cost_contest_best": self.cost_contest_best,
            "cost_contest_worst": self.cost_contest_worst,
            "cost_contest_expected": self.cost_contest_expected,
            "expected_saving": self.expected_saving,
            "contest_can_beat_accept": self.contest_can_beat_accept,
            "recommendation": self.recommendation.value,
        }


class UnsupportedCurrency(ValueError):
    """No configured fee schedule for this currency; the kit does not convert."""


def compute_economics(
    amount: Money,
    band: StrengthBand,
    config: DisputeConfig,
    *,
    merchant_fault: bool = False,
    contradictory: bool = False,
) -> Economics:
    terms: CurrencyTerms | None = config.terms_for(amount.currency)
    if terms is None:
        raise UnsupportedCurrency(f"no dispute economics configured for {amount.currency}")
    if amount.is_negative() or amount.is_zero():
        raise ValueError("disputed amount must be positive")
    p = config.win_probability[band]
    fee = terms.dispute_fee
    accept_fee = fee if config.fee_on_accept else Money.zero(amount.currency)
    cost_accept = amount + terms.accept_handling_cost + accept_fee
    best = fee + terms.contest_handling_cost
    worst = best + amount
    expected_loss = amount.scale(Decimal(1) - p, rounding=ROUND_HALF_EVEN)
    expected = best + expected_loss
    saving = cost_accept - expected
    can_beat = best < cost_accept

    reasons: list[str] = [
        f"accept costs {cost_accept}; contesting costs {best} to {worst}, "
        f"expected {expected} at win probability {p} ({band.value})",
        "dispute fee and handling costs are modelled; the sandbox does not deduct them",
    ]
    if not can_beat:
        rec = Recommendation.ACCEPT
        reasons.append("fee plus handling is at least the cost of accepting; contesting cannot pay")
    elif merchant_fault or contradictory:
        rec = Recommendation.REFER
        reasons.append("evidence points to merchant fault or contradicts itself; a person decides")
    elif BAND_ORDER[band] < BAND_ORDER[config.min_band_to_contest]:
        rec = (
            Recommendation.ACCEPT if saving <= Money.zero(amount.currency) else Recommendation.REFER
        )
        reasons.append(f"evidence band {band.value} is below {config.min_band_to_contest.value}")
    elif saving >= terms.min_saving_to_contest:
        rec = Recommendation.CONTEST
        reasons.append(f"expected saving {saving} meets the {terms.min_saving_to_contest} bar")
    elif saving <= -terms.min_saving_to_contest:
        rec = Recommendation.ACCEPT
        reasons.append(f"contesting is expected to cost {abs(saving)} more than accepting")
    else:
        rec = Recommendation.REFER
        reasons.append(f"expected saving {saving} is inside the indifference band")
    return Economics(
        disputed_amount=amount,
        dispute_fee_modelled=fee,
        contest_handling_modelled=terms.contest_handling_cost,
        accept_handling_modelled=terms.accept_handling_cost,
        win_probability=p,
        band=band,
        cost_accept=cost_accept,
        cost_contest_best=best,
        cost_contest_worst=worst,
        cost_contest_expected=expected,
        expected_saving=saving,
        contest_can_beat_accept=can_beat,
        recommendation=rec,
        reasons=tuple(reasons),
    )
