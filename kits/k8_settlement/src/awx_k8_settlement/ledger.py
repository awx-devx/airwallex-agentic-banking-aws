"""Seller reserve ledger and the settlement cycle identity.

Airwallex has no escrow or held-balance object, and the platform wallet cannot say which seller
owns which retained dollar. This ledger can. Every field is a non-negative Money in the seller's
currency, cumulative since the seller was opened:

    entitled        what the seller earned (net of platform fees), all cycles
    refunds_paid    buyer refunds the platform paid for this seller
    reserve_held    unreleased reserve the platform holds for this seller now
    paid_out        payouts that reached SETTLED
    payout_pending  payouts created but not yet SETTLED (cash still with the platform)
    payable         payouts that failed or never reached Airwallex; owed and re-sent next cycle
    recovered       recovery charges that reached SETTLED (seller -> platform)
    receivable      what the seller owes the platform (a refund shortfall), not yet recovered

Seller identity (always zero):

    paid_out + payout_pending + payable + refunds_paid + reserve_held
        - (entitled + recovered + receivable) = 0

Cycle identity (the guide's "payouts, refunds and released reserve equal what the seller was
owed"), per seller per cycle, always zero:

    entitlement + payable_before + released + drawn + shortfall
        - payout - refunds - retained - receivable_offset = 0

Platform identity: the platform's cash attributable to sellers equals
    sum(reserve_held + payable - receivable)
which is the guide's "remaining balance equals unreleased reserves" once no seller owes the
platform. payout_pending is NOT platform cash: Airwallex moves the money when the transfer is
created (sandbox, FAKE-FIDELITY.md), so pending is cash that has left but is not yet confirmed
SETTLED. A posted payout that never reached Airwallex therefore shows as a platform residual of
exactly its amount until the postcondition or a reconcile resolves it to payable, and a recovery
charge that has landed but is still pending or suspended shows as a residual of its amount
until it is confirmed SETTLED.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

from awx_common import CurrencyMismatch, Money

_MONEY_FIELDS = (
    "entitled",
    "refunds_paid",
    "reserve_held",
    "paid_out",
    "payout_pending",
    "payable",
    "recovered",
    "receivable",
)


class LedgerInvariantError(ValueError):
    """A posting would break an identity or drive a balance negative. Nothing was changed."""


def _min(a: Money, b: Money) -> Money:
    return a if a <= b else b


def _max0(a: Money) -> Money:
    zero = Money.zero(a.currency)
    return a if a > zero else zero


@dataclass(frozen=True)
class SellerLedger:
    seller: str  # the server-resolved tenant_id; never a connected account id from the model
    currency: str
    entitled: Money
    refunds_paid: Money
    reserve_held: Money
    paid_out: Money
    payout_pending: Money
    payable: Money
    recovered: Money
    receivable: Money
    version: int = 0

    @classmethod
    def open(cls, seller: str, currency: str) -> SellerLedger:
        z = Money.zero(currency)
        return cls(seller, currency, z, z, z, z, z, z, z, z)

    def __post_init__(self) -> None:
        if not self.seller:
            raise ValueError("seller is required")
        for name in _MONEY_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, Money):
                raise TypeError(f"{name} must be Money")
            if value.currency != self.currency:
                raise CurrencyMismatch(f"{name} is {value.currency}, ledger is {self.currency}")
            if value.is_negative():
                raise LedgerInvariantError(f"{name} would go negative ({value})")
        if self.version < 0:
            raise ValueError("version must be non-negative")

    def residual(self) -> Money:
        left = (
            self.paid_out
            + self.payout_pending
            + self.payable
            + self.refunds_paid
            + self.reserve_held
        )
        return left - (self.entitled + self.recovered + self.receivable)

    def platform_share(self) -> Money:
        """Platform cash attributable to this seller (negative when the seller owes more).

        payout_pending is excluded: the provider debits the platform at create.
        """
        return self.reserve_held + self.payable - self.receivable

    def _bump(self, **changes: Money) -> SellerLedger:
        new = replace(self, version=self.version + 1, **changes)
        if not new.residual().is_zero():
            raise LedgerInvariantError(f"seller identity off by {new.residual()}")
        return new

    # Payout outcomes -------------------------------------------------------------------------

    def payout_settled(self, amount: Money) -> SellerLedger:
        if amount > self.payout_pending:
            raise LedgerInvariantError("settling more than is pending")
        return self._bump(
            payout_pending=self.payout_pending - amount, paid_out=self.paid_out + amount
        )

    def payout_failed(self, amount: Money) -> SellerLedger:
        """A CANCELLED or FAILED payout: the cash never left, so the seller is still owed it."""
        if amount > self.payout_pending:
            raise LedgerInvariantError("failing more than is pending")
        return self._bump(
            payout_pending=self.payout_pending - amount, payable=self.payable + amount
        )

    def payout_found_after_failed(self, amount: Money) -> SellerLedger:
        """A payout posted as failed turned up SETTLED on a later read-back.

        The seller was paid after all. Whatever of it is still payable stops being owed; any
        excess (the payable was already re-sent) is an overpayment the seller owes back.
        """
        from_payable = _min(self.payable, amount)
        return self._bump(
            payable=self.payable - from_payable,
            paid_out=self.paid_out + amount,
            receivable=self.receivable + (amount - from_payable),
        )

    # Recovery outcomes -----------------------------------------------------------------------

    def recovery_settled(self, amount: Money) -> SellerLedger:
        if amount > self.receivable:
            raise LedgerInvariantError("recovering more than the seller owes")
        return self._bump(receivable=self.receivable - amount, recovered=self.recovered + amount)

    def recovery_found_after_failed(self, amount: Money) -> SellerLedger:
        """A charge posted as failed turned up SETTLED: any over-collection is owed back."""
        from_receivable = _min(self.receivable, amount)
        return self._bump(
            receivable=self.receivable - from_receivable,
            recovered=self.recovered + amount,
            payable=self.payable + (amount - from_receivable),
        )

    def to_record(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> SellerLedger:
        return cls(**{f.name: data[f.name] for f in fields(cls)})


@dataclass(frozen=True)
class CycleInput:
    cycle_id: str
    entitlement: Money  # what the seller earned this cycle, net of platform fees
    refunds: Money  # buyer refunds the platform paid for this seller this cycle
    reserve_target: Money  # computed by reserve.py, never by the model

    def __post_init__(self) -> None:
        for name in ("entitlement", "refunds", "reserve_target"):
            if getattr(self, name).is_negative():
                raise ValueError(f"{name} must be non-negative")
        if (
            len({self.entitlement.currency, self.refunds.currency, self.reserve_target.currency})
            != 1
        ):
            raise CurrencyMismatch("cycle amounts must share one currency")


@dataclass(frozen=True)
class CycleSettlement:
    """One seller's settlement for one cycle. Every amount is non-negative."""

    seller: str
    cycle_id: str
    currency: str
    entitlement: Money
    refunds: Money
    payable_before: Money  # failed payouts from earlier cycles, re-sent now
    reserve_before: Money
    reserve_target: Money
    reserve_after: Money
    released: Money  # reserve released to the seller (target fell)
    retained: Money  # reserve added from this cycle's cash (target rose)
    drawn: Money  # reserve consumed to pay refunds this cycle's cash could not cover
    unmet_reserve: Money  # target the cycle's cash could not fund; reported, never charged
    shortfall: Money  # refunds beyond cash and reserve: becomes a receivable
    receivable_offset: Money  # outstanding receivable netted against this payout
    payout: Money
    ledger_version: int  # the ledger version this was computed from

    def to_record(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> CycleSettlement:
        return cls(**{f.name: data[f.name] for f in fields(cls)})

    def residual(self) -> Money:
        credits = (
            self.entitlement + self.payable_before + self.released + self.drawn + self.shortfall
        )
        debits = self.payout + self.refunds + self.retained + self.receivable_offset
        return credits - debits


def settle_cycle(
    ledger: SellerLedger,
    cycle: CycleInput,
    *,
    net_receivables: bool = True,
    receivable_in_flight: Money | None = None,
) -> tuple[CycleSettlement, SellerLedger]:
    """Settle one seller for one cycle. Pure; touches no other seller.

    Order: refunds are paid from this cycle's cash, then from reserve; anything left is a
    shortfall (a receivable, never an automatic charge). Then the reserve moves to its target,
    funded only from this cycle's remaining cash. Then an outstanding receivable may be netted,
    except the part a recovery charge in flight may already be collecting (kit rule M1): that
    part stays a receivable until the charge has a definitive outcome.

    The payout never includes payout_pending: money in flight on an earlier request_id is not
    paid again under this cycle's request_id.
    """
    if cycle.entitlement.currency != ledger.currency:
        raise CurrencyMismatch(
            f"cycle is {cycle.entitlement.currency}, ledger is {ledger.currency}"
        )
    zero = Money.zero(ledger.currency)
    cash = cycle.entitlement + ledger.payable - cycle.refunds
    reserve = ledger.reserve_held
    drawn = shortfall = released = retained = unmet = zero
    if cash.is_negative():
        drawn = _min(reserve, -cash)
        reserve = reserve - drawn
        cash = cash + drawn
        if cash.is_negative():
            shortfall = -cash
            cash = zero
    target = cycle.reserve_target
    if target > reserve:
        retained = _min(target - reserve, cash)
        reserve = reserve + retained
        cash = cash - retained
        unmet = target - reserve
    else:
        released = reserve - target
        reserve = target
        cash = cash + released
    in_flight = receivable_in_flight or zero
    nettable = _max0(ledger.receivable - in_flight)
    offset = _min(nettable, cash) if net_receivables else zero
    cash = cash - offset
    settlement = CycleSettlement(
        seller=ledger.seller,
        cycle_id=cycle.cycle_id,
        currency=ledger.currency,
        entitlement=cycle.entitlement,
        refunds=cycle.refunds,
        payable_before=ledger.payable,
        reserve_before=ledger.reserve_held,
        reserve_target=target,
        reserve_after=reserve,
        released=released,
        retained=retained,
        drawn=drawn,
        unmet_reserve=_max0(unmet),
        shortfall=shortfall,
        receivable_offset=offset,
        payout=cash,
        ledger_version=ledger.version,
    )
    if not settlement.residual().is_zero():  # unreachable by construction; guards edits
        raise LedgerInvariantError(f"cycle identity off by {settlement.residual()}")
    new = ledger._bump(
        entitled=ledger.entitled + cycle.entitlement,
        refunds_paid=ledger.refunds_paid + cycle.refunds,
        reserve_held=reserve,
        payable=zero,
        payout_pending=ledger.payout_pending + cash,
        receivable=ledger.receivable - offset + shortfall,
    )
    return settlement, new


def platform_residual(
    ledgers: Iterable[SellerLedger], platform_balance: Money, *, platform_own_cash: Money
) -> Money:
    """Observed platform balance minus its own cash minus what the ledgers say it holds.

    Zero means every retained dollar is assigned to a seller. platform_own_cash is the cash the
    platform holds for itself (fees, capital); the guide's scenario has none.
    """
    expected = platform_own_cash
    for ledger in ledgers:
        if ledger.currency != platform_balance.currency:
            raise CurrencyMismatch("reconcile one currency at a time")
        expected = expected + ledger.platform_share()
    return platform_balance - expected
