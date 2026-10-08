"""Deterministic policy: collection sizing, disbursement sizing and the approval matrix.

Approval matrix (the model never sets a tier; this code does):

collect_weekly_repayment
    AUTO     scheduled run (Origin.SCHEDULE): the formula amount, already bounded by cap,
             amount due and wallet balance
    SINGLE   the same proposal started by the model (Origin.AGENT, the default agent approval floor)
    BLOCKED  not the loan's next collectible week (before the first full week after
             funding, out of order, or already closed), week not over, nothing owed,
             no portfolio
disburse_advance
    SINGLE   every tranche at or below dual_approval_above (never AUTO, any origin)
    DUAL     a tranche above dual_approval_above
    BLOCKED  delay (headroom above the floor below the minimum tranche), commitment
             fully disbursed, no portfolio
reconcile_portfolio
    AUTO     any origin (agent_min_tier AUTO): reads plus a balance report, no money moves
    refused  when called with a borrower tenant

An in-flight charge or tranche is re-proposed unchanged (same amount, same request_id) at the
tier above, never resized.

Prohibited, so no tool exists: waivers, write-offs, term changes, arrears catch-up charges, any
amount chosen by the model, and any movement between two borrowers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from awx_common import Money

from .book import PortfolioConfig, round_down_to


class TrancheOutcome(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    DELAY = "DELAY"


@dataclass(frozen=True)
class DisbursementPlan:
    commitment_remaining: Money
    usable_cash: Money
    reserve_floor: Money
    headroom: Money
    amount: Money
    cash_after: Money
    outcome: TrancheOutcome


def size_disbursement(
    commitment_remaining: Money, usable_cash: Money, config: PortfolioConfig
) -> DisbursementPlan:
    """The largest tranche that keeps platform cash at or above the reserve floor.

    A full remaining commitment that fits is sent exactly. Otherwise the tranche is rounded down
    to the disbursement increment; if that is under the minimum tranche the advance is delayed.
    """
    zero = Money.zero(config.currency)
    if commitment_remaining.is_negative():
        raise ValueError("commitment_remaining cannot be negative")
    headroom = max(usable_cash - config.reserve_floor, zero)
    if commitment_remaining.is_zero():
        amount, outcome = zero, TrancheOutcome.DELAY
    elif commitment_remaining <= headroom:
        amount, outcome = commitment_remaining, TrancheOutcome.FULL
    else:
        amount = round_down_to(headroom, config.disbursement_increment)
        outcome = TrancheOutcome.PARTIAL
        if amount < config.min_disbursement or amount.is_zero():
            amount, outcome = zero, TrancheOutcome.DELAY
    return DisbursementPlan(
        commitment_remaining=commitment_remaining,
        usable_cash=usable_cash,
        reserve_floor=config.reserve_floor,
        headroom=headroom,
        amount=amount,
        cash_after=usable_cash - amount,
        outcome=outcome,
    )
