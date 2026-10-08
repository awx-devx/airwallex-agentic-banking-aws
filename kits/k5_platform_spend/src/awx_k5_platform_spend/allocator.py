"""The deterministic reserve allocator: who gets bridged when capital is scarce.

Each candidate is a code-computed shortfall for a payment due today, built inside that tenant's
own workflow from its own balance read, so the allocator compares tenant summaries that carry no
account identifiers or credentials. The model supplies none of these facts; it only explains.

Rules:
- all or nothing: a bridge that covers part of a payment does not make the payment, so a
  candidate is funded for its whole shortfall or not at all;
- eligibility first (see `eligibility`), then priority order, then greedy fit against headroom
  (platform cash minus committed holds minus the reserve floor);
- priority key: fewer late repayments first, then the smaller shortfall (more customers served
  per dollar), then tenant_id and obligation_id so the order is total and reproducible.

The result never depends on input order.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from awx_common import Money

from .config import PlatformConfig

DENY_FLOOR = "would breach the reserve floor"


@dataclass(frozen=True)
class BridgeCandidate:
    tenant_id: str
    obligation_id: str
    shortfall: Money
    due_on: date
    expected_deposit_on: date
    late_repayments: int = 0
    has_open_advance: bool = False
    tenant_active: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.tenant_id, self.obligation_id)

    def priority(self) -> tuple[int, object, str, str]:
        return (self.late_repayments, self.shortfall.amount, self.tenant_id, self.obligation_id)


@dataclass(frozen=True)
class Allocation:
    funded: tuple[BridgeCandidate, ...]
    denied: tuple[tuple[BridgeCandidate, str], ...]
    headroom: Money
    remaining: Money

    def decision_for(self, key: tuple[str, str]) -> tuple[bool, str, int | None]:
        """(funded, reason, 1-based rank among funded)."""
        for rank, c in enumerate(self.funded, start=1):
            if c.key == key:
                return True, "funded", rank
        for c, reason in self.denied:
            if c.key == key:
                return False, reason, None
        raise KeyError(key)


def eligibility(c: BridgeCandidate, config: PlatformConfig, today: date) -> str | None:
    """None when eligible, else the reason."""
    if c.shortfall.currency != config.currency:
        return "shortfall is not in the platform currency"
    if not c.tenant_active:
        return "tenant is not active"
    if c.shortfall.amount <= 0:
        return "no shortfall"
    if c.due_on != today:
        return "payment is not due today"
    if c.expected_deposit_on < c.due_on:
        return "deposit is expected before the payment is due"
    if (c.expected_deposit_on - c.due_on).days > config.max_bridge_days:
        return "deposit is expected too late to bridge"
    if c.shortfall > config.max_bridge_per_tenant:
        return "shortfall exceeds the per-tenant bridge limit"
    if c.has_open_advance:
        return "tenant already has an open advance"
    return None


def allocate(
    candidates: Iterable[BridgeCandidate],
    headroom: Money,
    config: PlatformConfig,
    today: date,
) -> Allocation:
    pool = list(candidates)
    keys = [c.key for c in pool]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate bridge candidate")
    if headroom.currency != config.currency:
        raise ValueError("headroom must be in the platform currency")
    remaining = headroom
    funded: list[BridgeCandidate] = []
    denied: list[tuple[BridgeCandidate, str]] = []
    for c in sorted(pool, key=BridgeCandidate.priority):
        reason = eligibility(c, config, today)
        if reason is not None:
            denied.append((c, reason))
        elif c.shortfall <= remaining:
            funded.append(c)
            remaining = remaining - c.shortfall
        else:
            denied.append((c, DENY_FLOOR))
    return Allocation(tuple(funded), tuple(denied), headroom, remaining)
