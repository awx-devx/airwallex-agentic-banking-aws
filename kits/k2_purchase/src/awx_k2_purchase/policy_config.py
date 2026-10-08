"""Server-side purchase policy. The model never sets any of these values.

Every number here is an AWS-authored PoC assumption (DESIGN.md, Policy values), versioned so an
approval binds to the policy it was evaluated under.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

from awx_common import Money

POLICY_VERSION = "k2-policy-2026-10-06.1"

_MCC_TABLE: dict[str, tuple[str, ...]] = {
    # 5734 computer software stores, 5817 digital goods (software), 7372 programming and SaaS
    "SAAS_SUBSCRIPTION": ("5734", "5817", "7372"),
}


def _m(amount: str, currency: str) -> Money:
    return Money.of(amount, currency)


@dataclass(frozen=True)
class PurchasePolicy:
    version: str = POLICY_VERSION
    # Minimum cash that must remain in the card currency wallet at every point of the horizon.
    reserve_floor: MappingProxyType = field(
        default_factory=lambda: MappingProxyType(
            {c: _m("25000", c) for c in ("USD", "AUD", "GBP", "EUR", "SGD", "HKD")}
        )
    )
    horizon_days: int = 91  # 13 weeks of cash curve
    review_interval_days: int = 90  # reconsider at least this often
    review_lead_days: int = 14  # reconsider this long before a cancellation deadline
    # A new or raised spending authority above this ALL_TIME total needs two approvers.
    dual_approval_above: MappingProxyType = field(
        default_factory=lambda: MappingProxyType(
            {c: _m("10000", c) for c in ("USD", "AUD", "GBP", "EUR", "SGD", "HKD")}
        )
    )
    # Never create authority above this, whoever approves.
    max_all_time: MappingProxyType = field(
        default_factory=lambda: MappingProxyType(
            {c: _m("50000", c) for c in ("USD", "AUD", "GBP", "EUR", "SGD", "HKD")}
        )
    )
    mcc_table: MappingProxyType = field(default_factory=lambda: MappingProxyType(_MCC_TABLE))
    # Probe values for the limit-boundary acceptance test. Neither may be in mcc_table values.
    disallowed_mcc: str = "5812"  # eating places and restaurants
    probe_currencies: tuple[str, ...] = ("EUR", "GBP", "USD", "AUD")
    probe_small_amount: str = "1.00"

    @property
    def currencies(self) -> frozenset[str]:
        return frozenset(self.reserve_floor)

    def floor(self, currency: str) -> Money:
        return self.reserve_floor[currency]


POLICY = PurchasePolicy()
