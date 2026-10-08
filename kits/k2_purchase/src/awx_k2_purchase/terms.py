"""Purchase terms, the cash curve, plan selection, reconsideration and the control set.

The model extracts the terms (prices, discount, notice period, dates) and the forecast flows
from documents. Everything in this module is code: the cash curve, which plan wins, the
reconsideration date and every card limit.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from awx_common import Money

from .amounts import parse_money
from .controls import CardControls, Interval, SpendLimit, merchant_categories_for
from .policy_config import POLICY, PurchasePolicy

_REF = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class Plan(StrEnum):
    MONTHLY = "MONTHLY"
    ANNUAL = "ANNUAL"


class Direction(StrEnum):
    IN = "IN"
    OUT = "OUT"


@dataclass(frozen=True)
class Flow:
    on: date
    amount: Money  # signed: negative is an outflow
    label: str


@dataclass(frozen=True)
class PurchaseTerms:
    purchase_ref: str
    vendor: str
    category: str
    currency: str
    monthly_price: Money
    annual_price: Money
    cancellation_notice_days: int
    start_date: date
    renewal_date: date | None
    terms_document_sha256: str


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def annual_price_from_discount(monthly: Money, discount_percent: Decimal) -> Money:
    """12 x monthly less the discount, rounded half-up to the minor unit."""
    if not Decimal(0) <= discount_percent < Decimal(100):
        raise ValueError("discount percent must be in [0, 100)")
    factor = Decimal(12) * (Decimal(100) - discount_percent) / Decimal(100)
    return monthly.scale(factor, rounding=ROUND_HALF_UP)


# ---- validation of tool input ---------------------------------------------------------------


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


def parse_terms(raw: Mapping[str, Any], policy: PurchasePolicy = POLICY) -> PurchaseTerms:
    ref = raw.get("purchase_ref")
    if not isinstance(ref, str) or not _REF.match(ref):
        raise ValueError("purchase_ref must be 3-40 chars of a-z, 0-9 and '-'")
    currency = raw.get("currency")
    if currency not in policy.currencies:
        raise ValueError(f"currency {currency!r} is not supported by the purchase policy")
    category = raw.get("purchase_category")
    if category not in policy.mcc_table:
        raise ValueError(f"purchase_category {category!r} is not in the policy table")
    monthly = parse_money(raw.get("monthly_price"), currency, field="monthly_price")
    if monthly.is_zero():
        raise ValueError("monthly_price must be positive")
    has_annual = raw.get("annual_price") is not None
    has_pct = raw.get("annual_discount_percent") is not None
    if has_annual == has_pct:
        raise ValueError("give exactly one of annual_price or annual_discount_percent")
    if has_annual:
        annual = parse_money(raw["annual_price"], currency, field="annual_price")
    else:
        pct = raw["annual_discount_percent"]
        if not isinstance(pct, str) or not re.match(r"^[0-9]{1,2}(\.[0-9]{1,2})?$", pct):
            raise ValueError('annual_discount_percent must be a decimal string like "18"')
        annual = annual_price_from_discount(monthly, Decimal(pct))
    if annual.is_zero():
        raise ValueError("annual price must be positive")
    notice = raw.get("cancellation_notice_days")
    if isinstance(notice, bool) or not isinstance(notice, int) or not 0 <= notice <= 365:
        raise ValueError("cancellation_notice_days must be an integer 0-365")
    start = _date(raw.get("start_date"), "start_date")
    renewal = raw.get("renewal_date")
    renewal_d = _date(renewal, "renewal_date") if renewal is not None else None
    if renewal_d is not None and renewal_d <= start:
        raise ValueError("renewal_date must be after start_date")
    sha = raw.get("terms_document_sha256")
    if not isinstance(sha, str) or not _SHA.match(sha):
        raise ValueError("terms_document_sha256 must be a lower-case SHA-256 hex digest")
    vendor = raw.get("vendor")
    if not isinstance(vendor, str) or not 1 <= len(vendor.strip()) <= 80:
        raise ValueError("vendor must be 1-80 characters")
    return PurchaseTerms(
        purchase_ref=ref,
        vendor=vendor.strip(),
        category=category,
        currency=currency,
        monthly_price=monthly,
        annual_price=annual,
        cancellation_notice_days=notice,
        start_date=start,
        renewal_date=renewal_d,
        terms_document_sha256=sha,
    )


def parse_flows(raw: Sequence[Mapping[str, Any]] | None, currency: str) -> tuple[Flow, ...]:
    out: list[Flow] = []
    for i, f in enumerate(raw or ()):
        direction = Direction(f.get("direction"))
        amount = parse_money(f.get("amount"), currency, field=f"forecast_flows[{i}].amount")
        label = f.get("label", "")
        if not isinstance(label, str) or len(label) > 80:
            raise ValueError(f"forecast_flows[{i}].label must be a string up to 80 chars")
        signed = amount if direction is Direction.IN else -amount
        out.append(Flow(_date(f.get("date"), f"forecast_flows[{i}].date"), signed, label))
    return tuple(sorted(out, key=lambda x: (x.on, x.amount.amount, x.label)))


def terms_to_intent(t: PurchaseTerms, flows: Sequence[Flow]) -> dict[str, Any]:
    """Canonical intent fields: everything the approver sees and the hash binds to."""
    return {
        "purchase_ref": t.purchase_ref,
        "vendor": t.vendor,
        "purchase_category": t.category,
        "currency": t.currency,
        "monthly_price": format(t.monthly_price.amount, "f"),
        "annual_price": format(t.annual_price.amount, "f"),
        "cancellation_notice_days": t.cancellation_notice_days,
        "start_date": t.start_date.isoformat(),
        "renewal_date": t.renewal_date.isoformat() if t.renewal_date else None,
        "terms_document_sha256": t.terms_document_sha256,
        "forecast_flows": [
            {
                "date": f.on.isoformat(),
                "direction": "OUT" if f.amount.is_negative() else "IN",
                "amount": format(abs(f.amount).amount, "f"),
                "label": f.label,
            }
            for f in flows
        ],
    }


def terms_from_intent(intent: Mapping[str, Any]) -> tuple[PurchaseTerms, tuple[Flow, ...]]:
    raw = dict(intent)
    raw.pop("annual_discount_percent", None)
    terms = parse_terms(raw)
    return terms, parse_flows(intent.get("forecast_flows"), terms.currency)


# ---- cash curve and plan choice -------------------------------------------------------------


@dataclass(frozen=True)
class PlanOutcome:
    plan: Plan
    charges: tuple[tuple[date, Money], ...]
    cost_over_term: Money  # 12 months of the plan
    min_balance: Money
    min_balance_on: date
    first_breach_on: date | None
    first_breach_week: int | None
    weekly_closing: tuple[tuple[int, Money], ...]

    @property
    def feasible(self) -> bool:
        return self.first_breach_on is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.value,
            "feasible": self.feasible,
            "cost_over_term": self.cost_over_term.to_dict(),
            "min_balance": self.min_balance.to_dict(),
            "min_balance_on": self.min_balance_on.isoformat(),
            "first_breach_on": self.first_breach_on.isoformat() if self.first_breach_on else None,
            "first_breach_week": self.first_breach_week,
            "charges": [{"date": d.isoformat(), **m.to_dict()} for d, m in self.charges],
            "weekly_closing": [{"week": w, **m.to_dict()} for w, m in self.weekly_closing],
        }


def plan_charges(t: PurchaseTerms, plan: Plan, until: date) -> tuple[tuple[date, Money], ...]:
    """Charge dates on or before `until`. Monthly bills on the start day each month."""
    if plan is Plan.ANNUAL:
        return ((t.start_date, t.annual_price),) if t.start_date <= until else ()
    out = []
    k = 0
    while (d := add_months(t.start_date, k)) <= until:
        out.append((d, t.monthly_price))
        k += 1
    return tuple(out)


def cash_curve(
    t: PurchaseTerms,
    plan: Plan,
    opening: Money,
    flows: Sequence[Flow],
    policy: PurchasePolicy = POLICY,
) -> PlanOutcome:
    """Run the balance forward day by day over the horizon and find the lowest point.

    Same-day order: inflows, then outflows and charges, so a breach is never hidden by an
    inflow later that day being counted first. That is the conservative reading.
    """
    floor = policy.floor(t.currency)
    end = t.start_date + timedelta(days=policy.horizon_days - 1)
    charges = plan_charges(t, plan, end)
    events: list[tuple[date, int, Money]] = []
    for f in flows:
        if t.start_date <= f.on <= end:
            events.append((f.on, 0 if not f.amount.is_negative() else 1, f.amount))
    events.extend((d, 1, -m) for d, m in charges)
    events.sort(key=lambda e: (e[0], e[1], e[2].amount))
    balance = opening
    low, low_on = opening, t.start_date
    breach_on: date | None = None if opening >= floor else t.start_date
    weekly: dict[int, Money] = {}
    idx = 0
    day = t.start_date
    while day <= end:
        while idx < len(events) and events[idx][0] == day:
            balance = balance + events[idx][2]
            if balance < low:
                low, low_on = balance, day
            if breach_on is None and balance < floor:
                breach_on = day
            idx += 1
        weekly[(day - t.start_date).days // 7 + 1] = balance
        day += timedelta(days=1)
    term_cost = (
        t.annual_price if plan is Plan.ANNUAL else t.monthly_price.scale(12, rounding=ROUND_HALF_UP)
    )
    return PlanOutcome(
        plan=plan,
        charges=charges,
        cost_over_term=term_cost,
        min_balance=low,
        min_balance_on=low_on,
        first_breach_on=breach_on,
        first_breach_week=(breach_on - t.start_date).days // 7 + 1 if breach_on else None,
        weekly_closing=tuple(sorted(weekly.items())),
    )


@dataclass(frozen=True)
class PlanChoice:
    chosen: Plan | None
    reason: str
    outcomes: Mapping[Plan, PlanOutcome]
    annual_saving: Money


def choose_plan(
    t: PurchaseTerms, opening: Money, flows: Sequence[Flow], policy: PurchasePolicy = POLICY
) -> PlanChoice:
    """Cheapest plan that keeps cash above the reserve floor; a tie goes to MONTHLY.

    MONTHLY keeps the option to cancel, so it wins whenever saving nothing or breaking the
    reserve is the alternative.
    """
    outcomes = {p: cash_curve(t, p, opening, flows, policy) for p in Plan}
    saving = outcomes[Plan.MONTHLY].cost_over_term - outcomes[Plan.ANNUAL].cost_over_term
    feasible = [p for p in (Plan.MONTHLY, Plan.ANNUAL) if outcomes[p].feasible]
    if not feasible:
        return PlanChoice(None, "both plans break the reserve floor", outcomes, saving)
    if feasible == [Plan.MONTHLY]:
        week = outcomes[Plan.ANNUAL].first_breach_week
        return PlanChoice(
            Plan.MONTHLY,
            f"annual saves {saving} but breaks the reserve floor in week {week}",
            outcomes,
            saving,
        )
    if feasible == [Plan.ANNUAL]:
        return PlanChoice(Plan.ANNUAL, "monthly breaks the reserve floor", outcomes, saving)
    if saving.amount > 0:
        return PlanChoice(
            Plan.ANNUAL, f"both keep the reserve; annual saves {saving}", outcomes, saving
        )
    return PlanChoice(Plan.MONTHLY, "both keep the reserve; annual saves nothing", outcomes, saving)


# ---- reconsideration ------------------------------------------------------------------------


@dataclass(frozen=True)
class Reconsideration:
    on: date
    reason: str

    @property
    def at(self) -> datetime:
        return datetime.combine(self.on, time(0, 0), tzinfo=UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.on.isoformat(),
            "reason": self.reason,
            # EventBridge Scheduler one-time expression, evaluated in UTC.
            "schedule_expression": f"at({self.at.strftime('%Y-%m-%dT%H:%M:%S')})",
            "schedule_expression_timezone": "UTC",
        }


def reconsideration(
    t: PurchaseTerms, plan: Plan, policy: PurchasePolicy = POLICY
) -> Reconsideration:
    """When to look at the decision again: the policy review interval, or earlier to leave
    review_lead_days before the cancellation deadline of the renewal."""
    candidates = [(t.start_date + timedelta(days=policy.review_interval_days), "periodic review")]
    if t.renewal_date is not None:
        deadline = t.renewal_date - timedelta(days=t.cancellation_notice_days)
        candidates.append(
            (deadline - timedelta(days=policy.review_lead_days), "before the cancellation deadline")
        )
    on, reason = min(candidates)
    if plan is Plan.ANNUAL and t.renewal_date is None:
        reason = "periodic review (annual plan with no renewal date in the terms)"
    floor_day = t.start_date + timedelta(days=1)
    if on < floor_day:
        on, reason = floor_day, "the cancellation deadline is already close; review tomorrow"
    return Reconsideration(on, reason)


# ---- the control set ------------------------------------------------------------------------


def controls_for(
    t: PurchaseTerms,
    plan: Plan,
    review: Reconsideration,
    policy: PurchasePolicy = POLICY,
    *,
    already_spent_all_time: Money | None = None,
) -> CardControls:
    """The least authority that lets the chosen plan pay until the next reconsideration.

    MONTHLY: per transaction = MONTHLY = one month's price; ALL_TIME = the charges due up to the
    reconsideration date (plus anything already spent on this card, for an update). ANNUAL: one
    single-use charge of the annual price, every limit equal to it.
    """
    mccs = merchant_categories_for(t.category, policy.mcc_table)
    spent = already_spent_all_time or Money.zero(t.currency)
    if plan is Plan.ANNUAL:
        price = t.annual_price
        return CardControls(
            currency=t.currency,
            allowed_merchant_categories=mccs,
            limits=(
                SpendLimit(Interval.PER_TRANSACTION, price),
                SpendLimit(Interval.MONTHLY, price),
                SpendLimit(Interval.ALL_TIME, spent + price),
            ),
            single_use=spent.is_zero(),
        )
    n = len(plan_charges(t, Plan.MONTHLY, review.on))
    total = t.monthly_price.scale(max(n, 1), rounding=ROUND_HALF_UP)
    return CardControls(
        currency=t.currency,
        allowed_merchant_categories=mccs,
        limits=(
            SpendLimit(Interval.PER_TRANSACTION, t.monthly_price),
            SpendLimit(Interval.MONTHLY, t.monthly_price),
            SpendLimit(Interval.ALL_TIME, spent + total),
        ),
    )
