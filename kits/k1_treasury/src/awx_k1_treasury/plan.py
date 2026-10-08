"""The treasury plan: fund, convert-and-fund, defer or escalate each obligation.

Greedy in priority order, then one whole-plan reserve check over the merged legs. The
whole-plan check is what decides; the greedy pass only proposes. Checking one leg at a time is
the trap the review names: the conversion settles at once, then the SWIFT fee on the transfer
pushes the wallet under its floor with no clean unwind.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from awx_common import Money

from .book import Priority, TreasuryBook
from .money_rules import (
    SANDBOX_SWIFT_FEES,
    ConversionLeg,
    IndicativeRate,
    PayoutLeg,
    ReserveBreach,
    UnknownFee,
    min_conversion,
    payout_fee,
    project,
    whole_plan_breaches,
)


class DecisionKind(StrEnum):
    FUND = "FUND"  # pay from cash already in that currency's wallet
    CONVERT_AND_FUND = "CONVERT_AND_FUND"  # buy the minimum, then pay
    DEFER = "DEFER"
    ESCALATE = "ESCALATE"  # a person decides; never executed by this kit


FUNDED = frozenset({DecisionKind.FUND, DecisionKind.CONVERT_AND_FUND})


@dataclass(frozen=True)
class PlanConfig:
    rate_tolerance: Decimal = Decimal("0.02")  # headroom between indicative rate and quote
    deferral_buffer: Decimal = Decimal("0.25")  # a deferrable payment must leave floor * 1.25
    swift_fees: Mapping[str, Money] = field(default_factory=lambda: dict(SANDBOX_SWIFT_FEES))

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.rate_tolerance <= Decimal("0.2"):
            raise ValueError("rate_tolerance must be between 0 and 0.2")
        if self.deferral_buffer < 0:
            raise ValueError("deferral_buffer must be non-negative")


@dataclass(frozen=True)
class Decision:
    obligation_id: str
    kind: DecisionKind
    reason: str
    need: Money | None = None  # amount plus fee
    buy: Money | None = None  # converted for this obligation


@dataclass(frozen=True)
class Plan:
    decisions: tuple[Decision, ...]
    conversions: tuple[ConversionLeg, ...]  # one per bought currency
    payouts: tuple[PayoutLeg, ...]  # execution order
    balances: Mapping[str, Money]
    projected: Mapping[str, Money]
    breaches: tuple[str, ...]

    def decision(self, obligation_id: str) -> Decision:
        return next(d for d in self.decisions if d.obligation_id == obligation_id)

    @property
    def conversion_total(self) -> Money | None:
        if not self.conversions:
            return None
        total = self.conversions[0].sell
        for c in self.conversions[1:]:
            total = total + c.sell
        return total


def _floor_plus_buffer(floor: Money, buffer: Decimal) -> Money:
    return floor + floor.scale(buffer, rounding=ROUND_CEILING)


def build_plan(
    book: TreasuryBook,
    balances: Mapping[str, Money],
    rates: Mapping[str, IndicativeRate],
    config: PlanConfig,
    *,
    only: Collection[str] | None = None,
) -> Plan:
    """Plan the book's obligations (or just `only`) against current balances.

    `rates` is keyed by bought currency; each sells the book's funding currency.
    """
    funding = book.funding_currency
    floors = book.floors
    remaining = {c: balances.get(c, Money.zero(c)) for c in floors}
    obligations = [o for o in book.obligations if only is None or o.obligation_id in only]
    obligations.sort(key=lambda o: (o.priority, o.due_at, o.obligation_id))

    decisions: list[Decision] = []
    payouts: list[PayoutLeg] = []
    buys: dict[str, Money] = {}
    sells: dict[str, Money] = {}
    unfunded_critical = False

    for ob in obligations:
        oid = ob.obligation_id
        cur = ob.amount.currency
        deferrable = ob.priority is Priority.DEFERRABLE

        def unfundable(
            why: str, need: Money | None = None, oid: str = oid, deferrable: bool = deferrable
        ) -> Decision:
            kind = DecisionKind.DEFER if deferrable else DecisionKind.ESCALATE
            return Decision(oid, kind, why, need)

        if ob.policy_exception:
            # A policy exception is a person's call, not a cash shortfall, so it does not hold
            # back deferrable payments.
            decisions.append(Decision(oid, DecisionKind.ESCALATE, ob.policy_exception))
            continue
        try:
            fee = payout_fee(ob.method, cur, config.swift_fees)
        except UnknownFee as exc:
            decisions.append(Decision(oid, DecisionKind.ESCALATE, str(exc)))
            unfunded_critical |= not deferrable
            continue
        need = ob.amount + fee
        if deferrable and unfunded_critical:
            decisions.append(
                Decision(oid, DecisionKind.DEFER, "a higher-priority obligation is unfunded", need)
            )
            continue
        # A deferrable payment must leave a buffer above the floor; the rest only the floor.
        floor_cur = floors[cur]
        floor_fund = floors[funding]
        if deferrable:
            floor_cur = _floor_plus_buffer(floor_cur, config.deferral_buffer)
            floor_fund = _floor_plus_buffer(floor_fund, config.deferral_buffer)
        try:
            buy = min_conversion(need, remaining[cur], floor_cur)
        except ReserveBreach as exc:
            why = "paying now would cut into the deferral buffer" if deferrable else str(exc)
            decisions.append(unfundable(why, need))
            unfunded_critical |= not deferrable
            continue

        if buy.is_zero():
            remaining[cur] = remaining[cur] - need
            decisions.append(Decision(oid, DecisionKind.FUND, "cash on hand covers it", need))
            payouts.append(PayoutLeg(oid, ob.beneficiary_id, ob.amount, fee, ob.method))
            continue
        if cur == funding:
            decisions.append(unfundable(f"{cur} cash above the reserve floor is short", need))
            unfunded_critical |= not deferrable
            continue
        rate = rates.get(cur)
        if rate is None or rate.sell_currency != funding:
            decisions.append(unfundable(f"no indicative {funding}->{cur} rate", need))
            unfunded_critical |= not deferrable
            continue
        # Price the merged conversion so its rounding is the one the plan carries.
        merged_buy = buys.get(cur, Money.zero(cur)) + buy
        merged_sell = rate.max_sell(merged_buy, config.rate_tolerance)
        extra_sell = merged_sell - sells.get(cur, Money.zero(funding))
        if remaining[funding] - extra_sell < floor_fund:
            decisions.append(
                unfundable(
                    f"{funding} cash above the reserve floor cannot fund the conversion", need
                )
            )
            unfunded_critical |= not deferrable
            continue
        buys[cur] = merged_buy
        sells[cur] = merged_sell
        remaining[funding] = remaining[funding] - extra_sell
        remaining[cur] = remaining[cur] + buy - need
        decisions.append(
            Decision(
                oid, DecisionKind.CONVERT_AND_FUND, "converts the minimum, fee included", need, buy
            )
        )
        payouts.append(PayoutLeg(oid, ob.beneficiary_id, ob.amount, fee, ob.method))

    conversions = tuple(ConversionLeg(buys[c], sells[c]) for c in sorted(buys))
    start = {c: balances.get(c, Money.zero(c)) for c in floors}
    projected = project(start, conversions, payouts)
    return Plan(
        decisions=tuple(decisions),
        conversions=conversions,
        payouts=tuple(payouts),
        balances=start,
        projected=projected,
        breaches=whole_plan_breaches(start, conversions, payouts, floors),
    )


def affected_decisions(old: Plan, new: Plan) -> tuple[str, ...]:
    """Obligations whose decision changed after new cash (or a new forecast) arrived.

    The agent reopens only these; every other decision stands as it was.
    """
    before = {d.obligation_id: (d.kind, d.need, d.buy) for d in old.decisions}
    after = {d.obligation_id: (d.kind, d.need, d.buy) for d in new.decisions}
    return tuple(sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k)))
