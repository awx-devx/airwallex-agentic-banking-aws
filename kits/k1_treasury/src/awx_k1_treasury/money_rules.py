"""Every money rule of kit 1. Pure functions over awx_common.Money; no float anywhere.

Rounding always runs against the treasury: an estimated sell amount rounds up, so a plan that
clears the reserve floor on paper still clears it at the quoted rate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from awx_common import Money

from .book import TransferMethod

# SWIFT payout fees per currency, taken from the same wallet; LOCAL payouts are free. This is
# configuration, not fact: the sandbox charged EUR 6.21 on EUR SWIFT of 1.00 and 100.00
# (awx_spine FAKE-FIDELITY.md, [tr-swift-100], fee_paid_by PAYER, SHARED), while the guide says
# a flat EUR 12.85. The sandbox value is the default. No fee is verified for any other currency, so
# a SWIFT payout elsewhere escalates instead of guessing.
SANDBOX_SWIFT_FEES: Mapping[str, Money] = {"EUR": Money.of("6.21", "EUR")}
GUIDE_SWIFT_FEES: Mapping[str, Money] = {"EUR": Money.of("12.85", "EUR")}  # reference only


class K1Error(Exception):
    """A kit 1 rule refused the plan. Raised before any irreversible leg where possible."""


class UnknownFee(K1Error):
    pass


class ReserveBreach(K1Error):
    pass


class BadProviderData(K1Error):
    """A provider response is missing a field or carries a float amount."""


def payout_fee(
    method: TransferMethod, currency: str, fees: Mapping[str, Money] = SANDBOX_SWIFT_FEES
) -> Money:
    if method is TransferMethod.LOCAL:
        return Money.zero(currency)
    fee = fees.get(currency)
    if fee is None:
        raise UnknownFee(f"no verified SWIFT fee for {currency}")
    if fee.currency != currency or fee.is_negative():
        raise ValueError(f"SWIFT fee for {currency} must be a non-negative {currency} amount")
    return fee


def payout_need(
    amount: Money, method: TransferMethod, fees: Mapping[str, Money] = SANDBOX_SWIFT_FEES
) -> Money:
    """What the wallet loses for one payout: the amount plus its flat fee."""
    return amount + payout_fee(method, amount.currency, fees)


def min_conversion(need: Money, balance: Money, floor: Money) -> Money:
    """The least to buy so that paying `need` leaves the wallet at or above its floor.

    A wallet already below its floor is refused: topping it up is a policy decision, not a
    side effect of paying an invoice.
    """
    headroom = balance - floor
    if headroom.is_negative():
        raise ReserveBreach(f"{balance.currency} wallet is already below its reserve floor")
    short = need - headroom
    return short if short.amount > 0 else Money.zero(need.currency)


def to_decimal(value: Any) -> Decimal:
    """Exact decimal from a provider field. A float means precision was already lost upstream."""
    if isinstance(value, bool) or isinstance(value, float):
        raise BadProviderData("amount arrived as a float or bool; refusing to guess its value")
    if isinstance(value, Decimal | int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError as exc:
            raise BadProviderData(f"not a decimal: {value!r}") from exc
    raise BadProviderData(f"unexpected amount type {type(value).__name__}")


def to_money(value: Any, currency: str) -> Money:
    try:
        return Money.of(to_decimal(value), currency)
    except ValueError as exc:
        raise BadProviderData(str(exc)) from exc


@dataclass(frozen=True)
class IndicativeRate:
    """GET /fx/rates/current. Indicative only: it carries no quote_id and books nothing.

    Sandbox-verified (FAKE-FIDELITY.md, [fx-rate-eurusd], [fx-rate-usdeur]): `currency_pair` is
    fixed per pair whichever way you trade (EURUSD both ways), and `rate` is units of the
    second currency per one unit of the first. So when the pair is BUY+SELL one unit of the buy
    currency costs `rate` of the sell currency, and when it is SELL+BUY the rate is inverted.
    Any other pair is refused rather than guessed.
    """

    buy_currency: str
    sell_currency: str
    currency_pair: str
    rate: Decimal

    def __post_init__(self) -> None:
        if not self.rate > 0:
            raise BadProviderData("rate must be positive")
        if self.currency_pair not in (
            self.buy_currency + self.sell_currency,
            self.sell_currency + self.buy_currency,
        ):
            raise BadProviderData(f"currency_pair {self.currency_pair} does not match the request")

    def sell_per_buy(self) -> Decimal:
        if self.currency_pair == self.buy_currency + self.sell_currency:
            return self.rate
        return Decimal(1) / self.rate

    def max_sell(self, buy: Money, tolerance: Decimal) -> Money:
        """Upper bound on what buying `buy` may cost, rounded up to the minor unit."""
        if buy.currency != self.buy_currency:
            raise ValueError("buy amount is not in this rate's buy currency")
        if tolerance < 0:
            raise ValueError("tolerance must be non-negative")
        raw = buy.amount * self.sell_per_buy() * (Decimal(1) + tolerance)
        return Money.of(raw, self.sell_currency, rounding=ROUND_CEILING)


def parse_rate(body: Mapping[str, Any], *, buy_currency: str, sell_currency: str) -> IndicativeRate:
    try:
        if body["buy_currency"] != buy_currency or body["sell_currency"] != sell_currency:
            raise BadProviderData("rate response is for a different currency pair")
        return IndicativeRate(
            buy_currency, sell_currency, str(body["currency_pair"]), to_decimal(body["rate"])
        )
    except KeyError as exc:
        raise BadProviderData(f"rate response lacks {exc.args[0]}") from exc


def parse_balances(body: Any, currencies: Iterable[str]) -> dict[str, Money]:
    """GET /balances/current returns a top-level JSON array of per-currency rows.

    Uses available_amount. A currency with no row has a zero balance.
    """
    rows = body.get("items", body) if isinstance(body, Mapping) else body
    if not isinstance(rows, list):
        raise BadProviderData("balances response is not a list")
    wanted = set(currencies)
    out = {c: Money.zero(c) for c in wanted}
    seen: set[str] = set()
    for row in rows:
        cur = row.get("currency") if isinstance(row, Mapping) else None
        if cur not in wanted:
            continue
        if cur in seen:
            raise BadProviderData(f"two balance rows for {cur}")
        seen.add(cur)
        if "available_amount" not in row:
            raise BadProviderData(f"balance row for {cur} lacks available_amount")
        out[cur] = to_money(row["available_amount"], cur)
    return out


@dataclass(frozen=True)
class Quote:
    quote_id: str
    buy: Money
    sell: Money
    valid_to: datetime

    def remaining(self, now: datetime) -> timedelta:
        return self.valid_to - now

    def usable(self, now: datetime, margin: timedelta) -> bool:
        """A quote is usable only with `margin` left, so the booking lands before expiry."""
        return self.remaining(now) >= margin


QUOTE_EXPIRY_FIELDS = ("valid_to_at", "valid_to")


def parse_quote(body: Mapping[str, Any]) -> Quote:
    """POST /fx/quotes/create response (HTTP 200).

    Sandbox-verified fields (FAKE-FIDELITY.md, [quote1], [quote-MIN_1]): quote_id, buy_amount,
    sell_amount, valid_to_at, usage "MULTI_USE". A missing expiry still makes the quote
    unusable rather than open-ended.
    """
    try:
        quote_id = str(body["quote_id"])
        buy_cur = str(body["buy_currency"])
        sell_cur = str(body["sell_currency"])
        buy = to_money(body["buy_amount"], buy_cur)
        sell = to_money(body["sell_amount"], sell_cur)
    except KeyError as exc:
        raise BadProviderData(f"quote response lacks {exc.args[0]}") from exc
    raw_expiry = next((body[k] for k in QUOTE_EXPIRY_FIELDS if body.get(k)), None)
    if raw_expiry is None:
        raise BadProviderData("quote response has no expiry; treating it as unusable")
    if isinstance(raw_expiry, datetime):
        valid_to = raw_expiry
    else:
        try:
            valid_to = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
        except ValueError as exc:
            raise BadProviderData("quote expiry is not an ISO timestamp") from exc
    if valid_to.tzinfo is None:
        raise BadProviderData("quote expiry has no timezone")
    return Quote(quote_id, buy, sell, valid_to)


@dataclass(frozen=True)
class ConversionLeg:
    buy: Money
    sell: Money  # the most this leg may take from the funding wallet

    @property
    def sell_currency(self) -> str:
        return self.sell.currency


@dataclass(frozen=True)
class PayoutLeg:
    obligation_id: str
    beneficiary_id: str
    amount: Money
    fee: Money
    method: TransferMethod

    @property
    def need(self) -> Money:
        return self.amount + self.fee


def project(
    balances: Mapping[str, Money],
    conversions: Iterable[ConversionLeg],
    payouts: Iterable[PayoutLeg],
) -> dict[str, Money]:
    """Balances after every leg of the plan, fees included."""
    out = dict(balances)

    def add(m: Money) -> None:
        out[m.currency] = out.get(m.currency, Money.zero(m.currency)) + m

    for c in conversions:
        add(c.buy)
        add(-c.sell)
    for p in payouts:
        add(-p.need)
    return out


def execution_states(
    balances: Mapping[str, Money],
    conversions: Iterable[ConversionLeg],
    payouts: Iterable[PayoutLeg],
) -> list[dict[str, Money]]:
    """Balances after each leg in execution order: every conversion, then every payout."""
    state = dict(balances)
    states = [dict(state)]
    for c in conversions:
        state = project(state, [c], [])
        states.append(state)
    for p in payouts:
        state = project(state, [], [p])
        states.append(state)
    return states


def reserve_breaches(
    projected: Mapping[str, Money], floors: Mapping[str, Money]
) -> tuple[str, ...]:
    """Currencies that end below their floor. A currency without a floor is a breach."""
    out = []
    for cur, bal in sorted(projected.items()):
        floor = floors.get(cur)
        if floor is None or bal < floor:
            out.append(cur)
    return tuple(out)


def touched_currencies(
    conversions: Iterable[ConversionLeg], payouts: Iterable[PayoutLeg]
) -> set[str]:
    out: set[str] = set()
    for c in conversions:
        out |= {c.buy.currency, c.sell_currency}
    for p in payouts:
        out.add(p.amount.currency)
    return out


def whole_plan_breaches(
    balances: Mapping[str, Money],
    conversions: Iterable[ConversionLeg],
    payouts: Iterable[PayoutLeg],
    floors: Mapping[str, Money],
) -> tuple[str, ...]:
    """The whole-plan reserve check: every wallet the plan touches must stay at or above its
    floor after EVERY leg in execution order (all conversions, then all payouts), fees
    included. A wallet the plan does not touch is not the plan's to judge."""
    conversions, payouts = list(conversions), list(payouts)
    touched = touched_currencies(conversions, payouts)
    out: set[str] = set()
    for state in execution_states(balances, conversions, payouts)[1:]:
        out |= set(reserve_breaches({c: state[c] for c in touched}, floors))
    return tuple(sorted(out))
