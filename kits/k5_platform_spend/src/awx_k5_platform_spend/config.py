"""Server-side configuration for kit 5. None of it is ever model input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from awx_common import Money

KIT = "k5"
REFERENCE_PREFIX = "poc-k5-"  # every sandbox object kit 5 creates carries this

# `reason` on charges/create and connected_account_transfers/create is validated twice; values
# accepted live are listed in awx_spine FAKE-FIDELITY.md (Platform money movement, row (b)):
# professional_business_services is verified on both endpoints, loan_credit_repayment is on the
# accepted list. There is no platform_fee value.
BRIDGE_REASON = "professional_business_services"
FEE_REASON = "professional_business_services"
REPAYMENT_REASON = "loan_credit_repayment"


@dataclass(frozen=True)
class CardProgram:
    """Card controls the platform grants. The model picks a program name, never a limit."""

    name: str
    currency: str
    per_transaction_limit: Money
    monthly_limit: Money
    allowed_currencies: tuple[str, ...]
    allowed_merchant_categories: tuple[str, ...] = ()  # empty: no MCC restriction

    def __post_init__(self) -> None:
        for m in (self.per_transaction_limit, self.monthly_limit):
            if m.currency != self.currency or m.amount <= 0:
                raise ValueError("card limits must be positive and in the program currency")
        if self.per_transaction_limit > self.monthly_limit:
            raise ValueError("per-transaction limit cannot exceed the monthly limit")
        if self.currency not in self.allowed_currencies:
            raise ValueError("the program currency must be an allowed currency")
        if any(len(m) != 4 or not m.isdigit() for m in self.allowed_merchant_categories):
            raise ValueError("merchant categories are 4-digit MCCs")


@dataclass(frozen=True)
class PlatformConfig:
    """Bridge-capital policy. `reserve_floor` is the cash the platform never lends below."""

    currency: str
    reserve_floor: Money
    high_risk_bridge: Money  # bridges above this need two approvers
    max_bridge_per_tenant: Money
    max_bridge_days: int  # how late the customer's deposit may be expected
    card_programs: Mapping[str, CardProgram] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for m in (self.reserve_floor, self.high_risk_bridge, self.max_bridge_per_tenant):
            if m.currency != self.currency:
                raise ValueError("all platform limits must be in the platform currency")
            if m.is_negative():
                raise ValueError("platform limits cannot be negative")
        if self.max_bridge_days < 0:
            raise ValueError("max_bridge_days cannot be negative")
        object.__setattr__(self, "card_programs", dict(self.card_programs))
