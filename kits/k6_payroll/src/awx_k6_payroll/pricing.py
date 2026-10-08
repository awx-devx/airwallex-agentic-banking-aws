"""All payroll arithmetic. The model never computes any of these numbers.

Two layers:

- `price_employer` uses only the payroll itself: per payout currency, the payouts, the flat
  SWIFT fee per SWIFT payout and the total that must land in that wallet, plus the platform fee.
  These go into the child intent, so the approval hash covers them.
- `plan_funding` adds what only a read can tell: the employer's own funding balance and an
  indicative FX rate. It never sees another employer's balance; its only inputs are one
  employer's pricing and that employer's reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_UP, Decimal, InvalidOperation
from typing import Any

from awx_common import Money

from .model import SWIFT

# Fee per SWIFT payout, debited from the payout wallet with the payout. Used for SIZING only
# (how much to convert and the pre-send funds check); the postcondition takes each payout's
# real cost from the transfer it reads back (amount_payer_pays, or transfer_amount + fee_amount).
# Sized from the LARGEST fee evidence, applied once per payout:
# - the hackathon builder guide ("SWIFT payouts cost a flat fee"): EUR SWIFT of 100, 1,000,
#   2,000 and 4,000 each cost EUR 12.85;
# - a sandbox probe charged EUR 6.21 on 1.00 and 100.00 (FAKE-FIDELITY.md);
# - the live sandbox run: a 2,000.00 EUR SWIFT payout was refused balance_insufficient with
#   2,006.21 EUR available, so 6.21 does not cover that amount (the 400 body named no fee).
# max(12.85, 6.21) = 12.85. No other currency has evidence, so a SWIFT payout in any other
# currency is refused (fail closed for an unpriced corridor).
SWIFT_FEES: dict[str, Money] = {"EUR": Money.of("12.85", "EUR")}

# Platform fee per payroll run, charged to the employer in its funding currency:
# base + per contractor. AWS-authored assumption; the guide sets no amount.
PLATFORM_FEES: dict[str, tuple[Money, Money]] = {
    "USD": (Money.of("10.00", "USD"), Money.of("2.00", "USD")),
    "AUD": (Money.of("15.00", "AUD"), Money.of("3.00", "AUD")),
    "GBP": (Money.of("8.00", "GBP"), Money.of("1.60", "GBP")),
    "EUR": (Money.of("9.00", "EUR"), Money.of("1.80", "EUR")),
}

# Two approvers when one employer's payroll in one currency (payouts plus SWIFT fees, a
# read-free amount, so the approval hash stays stable) reaches this. Any other currency: DUAL.
DUAL_THRESHOLDS: dict[str, Money] = {
    "USD": Money.of("10000.00", "USD"),
    "AUD": Money.of("15000.00", "AUD"),
    "GBP": Money.of("8000.00", "GBP"),
    "EUR": Money.of("9000.00", "EUR"),
}

# Head-room on an indicative rate where no rate lock is approved yet (the read tool's plan):
# the most a conversion may sell is the indicative sell times (1 + FX_MARGIN).
FX_MARGIN = Decimal("0.02")


class RateUnusable(ValueError):
    """An FX rate or quote body this kit cannot interpret safely."""


def _sum(values: Sequence[Money], currency: str) -> Money:
    total = Money.zero(currency)
    for v in values:
        total = total + v
    return total


def price_employer(employer: Mapping[str, Any]) -> dict[str, Any]:
    """Pricing that depends only on the payroll lines. Deterministic and read-free."""
    funding = employer["funding_currency"]
    contractors = employer["contractors"]
    blockers: list[str] = []
    by_ccy: dict[str, list[Mapping[str, Any]]] = {}
    for c in contractors:
        by_ccy.setdefault(c["amount"].currency, []).append(c)
    needs: dict[str, dict[str, Any]] = {}
    for ccy in sorted(by_ccy):
        lines = by_ccy[ccy]
        swift = [c for c in lines if c["transfer_method"] == SWIFT]
        fee = SWIFT_FEES.get(ccy)
        if swift and fee is None:
            blockers.append(f"no evidenced SWIFT fee for {ccy}; refusing to price SWIFT payouts")
            fee = Money.zero(ccy)
        payouts = _sum([c["amount"] for c in lines], ccy)
        fees = _sum([fee] * len(swift), ccy)
        needs[ccy] = {
            "payouts": payouts,
            "swift_count": len(swift),
            "swift_fees": fees,
            "need": payouts + fees,
        }
    schedule = PLATFORM_FEES.get(funding)
    if schedule is None:
        blockers.append(f"no platform fee schedule for funding currency {funding}")
        platform_fee = Money.zero(funding)
    else:
        base, per = schedule
        platform_fee = base + _sum([per] * len(contractors), funding)
    return {"by_currency": needs, "platform_fee": platform_fee, "blockers": blockers}


def payout_cost(c: Mapping[str, Any]) -> Money:
    """What one payout takes out of its wallet: the amount plus any flat SWIFT fee."""
    amount: Money = c["amount"]
    if c["transfer_method"] == SWIFT:
        fee = SWIFT_FEES.get(amount.currency)
        if fee is None:
            raise RateUnusable(f"no evidenced SWIFT fee for {amount.currency}")
        return amount + fee
    return amount


def _decimal(value: Any, what: str) -> Decimal:
    if isinstance(value, float | bool):
        raise RateUnusable(f"{what} arrived as {type(value).__name__}; amounts must be exact")
    try:
        d = Decimal(value) if isinstance(value, str | int) else value
    except InvalidOperation as exc:
        raise RateUnusable(f"{what} is not a number") from exc
    if not isinstance(d, Decimal) or not d.is_finite():
        raise RateUnusable(f"{what} is not a finite number")
    return d


def sell_for(buy: Money, sell_currency: str, body: Mapping[str, Any], *, rate_key: str) -> Money:
    """Sell-currency cost of `buy`, from a rate or quote body. Rounds up, never down.

    Airwallex quotes a pair such as USDEUR, read as 1 unit of the first currency = rate units
    of the second. Any other pair shape is refused rather than guessed.
    """
    rate = _decimal(body.get(rate_key), rate_key)
    if rate <= 0:
        raise RateUnusable(f"{rate_key} must be positive")
    pair = body.get("currency_pair")
    if pair == f"{sell_currency}{buy.currency}":
        raw = buy.amount / rate
    elif pair == f"{buy.currency}{sell_currency}":
        raw = buy.amount * rate
    else:
        raise RateUnusable(f"currency_pair {pair!r} does not match {sell_currency}/{buy.currency}")
    return Money.of(raw, sell_currency, rounding=ROUND_UP)


def max_sell_for(buy: Money, sell_currency: str, rate_body: Mapping[str, Any]) -> Money:
    """Rule M3: the most a conversion may sell, indicative rate times (1 + FX_MARGIN)."""
    return with_margin(sell_for(buy, sell_currency, rate_body, rate_key="rate"))


def with_margin(amount: Money) -> Money:
    return amount.scale(Decimal(1) + FX_MARGIN, rounding=ROUND_UP)


def available_balance(balances: Any, currency: str) -> Money:
    """Available balance in one currency from one employer's GET /balances/current."""
    items = balances.get("items") if isinstance(balances, Mapping) else balances
    if not isinstance(items, list):
        raise RateUnusable("balances read is not a list")
    rows = [b for b in items if isinstance(b, Mapping) and b.get("currency") == currency]
    if not rows:
        return Money.zero(currency)
    if len(rows) > 1:
        raise RateUnusable(f"balances list {currency} twice")
    return Money.of(_decimal(rows[0].get("available_amount"), "available_amount"), currency)


def plan_funding(
    pricing: Mapping[str, Any],
    funding_currency: str,
    *,
    available: Money,
    rates: Mapping[str, Mapping[str, Any]] | None = None,
    max_sell: Mapping[str, Money] | None = None,
    done_conversions: frozenset[str] = frozenset(),
    done_funding_payouts: Sequence[Money] = (),
    fee_done: bool = False,
) -> dict[str, Any]:
    """Size every conversion and decide whether this employer alone can fund its payroll.

    `max_sell` (buy currency -> approved maximum sell, rule M3) replaces the indicative
    estimate when the parent's approval already fixed it; `rates` are used otherwise (the read
    tool). `done_*` exclude work an earlier run of the same pay period already submitted, so a
    resume is funded on what is left. Only this employer's balance is an input.
    """
    if available.currency != funding_currency:
        raise ValueError("available balance must be in the funding currency")
    conversions: list[dict[str, Any]] = []
    required = Money.zero(funding_currency)
    for ccy, need in sorted(pricing["by_currency"].items()):
        if ccy == funding_currency:
            direct = need["need"]
            for paid in done_funding_payouts:
                direct = direct - paid
            required = required + direct
            continue
        if max_sell is not None:
            if ccy not in max_sell:
                raise RateUnusable(f"no approved maximum sell for {ccy}")
            cap = max_sell[ccy]
            if cap.currency != funding_currency:
                raise RateUnusable("approved maximum sell is not in the funding currency")
            estimate = None
        else:
            if rates is None:
                raise RateUnusable("need rates or an approved maximum sell")
            estimate = sell_for(need["need"], funding_currency, rates[ccy], rate_key="rate")
            cap = with_margin(estimate)
        conversions.append(
            {
                "buy_currency": ccy,
                "buy_amount": need["need"],
                "sell_currency": funding_currency,
                "sell_estimate": estimate,
                "sell_cap": cap,
            }
        )
        if ccy not in done_conversions:
            required = required + cap
    if not fee_done:
        required = required + pricing["platform_fee"]
    shortfall = required - available
    return {
        "conversions": conversions,
        "required": required,
        "available": available,
        "shortfall": shortfall if shortfall.amount > 0 else Money.zero(funding_currency),
    }
