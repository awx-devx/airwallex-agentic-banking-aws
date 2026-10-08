from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from awx_common import Money
from awx_k2_purchase.controls import Interval
from awx_k2_purchase.policy_config import POLICY
from awx_k2_purchase.terms import (
    Plan,
    add_months,
    annual_price_from_discount,
    cash_curve,
    choose_plan,
    controls_for,
    parse_flows,
    parse_terms,
    plan_charges,
    reconsideration,
    terms_from_intent,
    terms_to_intent,
)
from hypothesis import given
from hypothesis import strategies as st

SHA = "a" * 64


def raw_terms(**over):
    raw = {
        "purchase_ref": "crm-seats",
        "vendor": "Example CRM",
        "purchase_category": "SAAS_SUBSCRIPTION",
        "currency": "USD",
        "monthly_price": "1500.00",
        "annual_discount_percent": "18",
        "cancellation_notice_days": 30,
        "start_date": "2026-10-12",
        "renewal_date": "2027-10-12",
        "terms_document_sha256": SHA,
    }
    raw.update(over)
    return {k: v for k, v in raw.items() if v is not None}


def weekly_outflows(start: date, amount: str, weeks: int = 13):
    return [
        {
            "date": (start + timedelta(days=7 * w - 1)).isoformat(),
            "direction": "OUT",
            "amount": amount,
        }
        for w in range(1, weeks + 1)
    ]


# Opening 60,000 USD, reserve 25,000, 3,000 out every week, a 20,000 receipt in week 8.
GUIDE_FLOWS = [
    *weekly_outflows(date(2026, 10, 12), "3000.00"),
    {"date": "2026-12-07", "direction": "IN", "amount": "20000.00", "label": "receipt"},
]


# ---- parsing --------------------------------------------------------------------------------


def test_discount_becomes_exact_annual_price():
    t = parse_terms(raw_terms())
    assert t.annual_price == Money.of("14760.00", "USD")  # 12 x 1500 x 0.82


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"annual_price": "14000.00"}, "exactly one"),
        ({"annual_discount_percent": None}, "exactly one"),
        ({"currency": "XYZ"}, "not supported"),
        ({"monthly_price": 1500.0}, "float"),
        ({"monthly_price": "15.001"}, "decimal"),
        ({"purchase_ref": "Bad Ref"}, "purchase_ref"),
        ({"terms_document_sha256": "nope"}, "SHA-256"),
        ({"renewal_date": "2026-10-01"}, "after start_date"),
        ({"cancellation_notice_days": True}, "integer"),
        ({"purchase_category": "GAMBLING"}, "policy table"),
    ],
)
def test_invalid_terms(over, message):
    with pytest.raises((ValueError, TypeError), match=message):
        parse_terms(raw_terms(**over))


def test_intent_round_trip():
    t = parse_terms(raw_terms())
    flows = parse_flows(weekly_outflows(t.start_date, "3000.00", 2), "USD")
    intent = terms_to_intent(t, flows)
    assert "annual_discount_percent" not in intent
    t2, flows2 = terms_from_intent(intent)
    assert (t2, flows2) == (t, flows)


@given(
    monthly=st.integers(1, 10_000_000),
    pct=st.decimals(min_value=0, max_value=Decimal("99.99"), places=2),
)
def test_annual_price_rule(monthly, pct):
    m = Money.from_minor(monthly, "USD")
    annual = annual_price_from_discount(m, pct)
    exact = Decimal(monthly) / 100 * 12 * (100 - pct) / 100
    assert annual.amount == exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert annual <= m.scale(12, rounding=ROUND_HALF_UP)


# ---- dates ----------------------------------------------------------------------------------


@given(st.dates(min_value=date(2000, 1, 1), max_value=date(2090, 1, 1)), st.integers(0, 120))
def test_add_months_lands_in_the_right_month(d, k):
    out = add_months(d, k)
    assert (out.year * 12 + out.month) - (d.year * 12 + d.month) == k
    assert out.day <= d.day


@given(st.dates(min_value=date(2000, 1, 1), max_value=date(2090, 1, 1)))
def test_monthly_charges_never_share_a_calendar_month(start):
    t = parse_terms(raw_terms(start_date=start.isoformat(), renewal_date=None))
    charges = plan_charges(t, Plan.MONTHLY, start + timedelta(days=400))
    months = [(d.year, d.month) for d, _ in charges]
    assert len(months) == len(set(months))


# ---- cash curve and plan choice ---------------------------------------------------------------


def test_guide_scenario_annual_breaks_reserve_in_week_seven():
    """Guide kit 2: annual is 18% cheaper but drops below the reserve in week seven.

    Weekly outflows land on the last day of each week; week 1 is days 0-6.
    """
    t = parse_terms(raw_terms())
    flows = parse_flows(GUIDE_FLOWS, "USD")
    choice = choose_plan(t, Money.of("60000.00", "USD"), flows)
    assert choice.chosen is Plan.MONTHLY
    annual = choice.outcomes[Plan.ANNUAL]
    assert annual.first_breach_week == 7
    assert not annual.feasible
    assert choice.outcomes[Plan.MONTHLY].feasible
    assert choice.annual_saving == Money.of("3240.00", "USD")
    assert "week 7" in choice.reason


def test_both_feasible_picks_annual_and_neither_blocks():
    t = parse_terms(raw_terms())
    assert choose_plan(t, Money.of("500000.00", "USD"), ()).chosen is Plan.ANNUAL
    assert choose_plan(t, Money.of("1000.00", "USD"), ()).chosen is None


def test_no_saving_ties_to_monthly():
    t = parse_terms(raw_terms(annual_discount_percent=None, annual_price="18000.00"))
    assert choose_plan(t, Money.of("500000.00", "USD"), ()).chosen is Plan.MONTHLY


@given(
    opening=st.integers(0, 50_000_000),
    outs=st.lists(st.tuples(st.integers(0, 120), st.integers(1, 2_000_000)), max_size=12),
    plan=st.sampled_from(list(Plan)),
)
def test_cash_curve_properties(opening, outs, plan):
    t = parse_terms(raw_terms())
    flows = parse_flows(
        [
            {
                "date": (t.start_date + timedelta(days=d)).isoformat(),
                "direction": "OUT",
                "amount": str(Money.from_minor(a, "USD").amount),
            }
            for d, a in outs
        ],
        "USD",
    )
    o = cash_curve(t, plan, Money.from_minor(opening, "USD"), flows)
    assert o.min_balance <= Money.from_minor(opening, "USD")
    assert o.feasible == (o.min_balance >= POLICY.floor("USD"))
    in_horizon = [f.amount for f in flows if (f.on - t.start_date).days < POLICY.horizon_days]
    end = Money.from_minor(opening, "USD")
    for amount in in_horizon:
        end = end + amount
    for _, charge in o.charges:
        end = end - charge
    assert o.weekly_closing[-1][1] == end


# ---- reconsideration and controls -----------------------------------------------------------


def test_reconsideration_is_periodic_or_before_cancellation_deadline():
    t = parse_terms(raw_terms())
    r = reconsideration(t, Plan.MONTHLY)
    assert r.on == t.start_date + timedelta(days=POLICY.review_interval_days)
    d = r.to_dict()
    assert d["schedule_expression"] == f"at({r.on.isoformat()}T00:00:00)"
    assert d["schedule_expression_timezone"] == "UTC"
    soon = parse_terms(raw_terms(renewal_date="2026-12-31", cancellation_notice_days=30))
    r2 = reconsideration(soon, Plan.ANNUAL)
    assert r2.on == date(2026, 12, 31) - timedelta(days=30 + POLICY.review_lead_days)
    assert r2.reason == "before the cancellation deadline"


def test_reconsideration_never_today_or_earlier():
    t = parse_terms(raw_terms(renewal_date="2026-10-20", cancellation_notice_days=30))
    assert reconsideration(t, Plan.MONTHLY).on == t.start_date + timedelta(days=1)


@given(
    monthly=st.integers(100, 1_000_000),
    start=st.dates(min_value=date(2026, 1, 1), max_value=date(2030, 1, 1)),
)
def test_monthly_controls_cover_exactly_the_charges_until_review(monthly, start):
    m = Money.from_minor(monthly, "USD")
    t = parse_terms(
        raw_terms(monthly_price=str(m.amount), start_date=start.isoformat(), renewal_date=None)
    )
    r = reconsideration(t, Plan.MONTHLY)
    c = controls_for(t, Plan.MONTHLY, r)
    n = len(plan_charges(t, Plan.MONTHLY, r.on))
    assert c.limit(Interval.PER_TRANSACTION) == m
    assert c.limit(Interval.MONTHLY) == m
    assert c.limit(Interval.ALL_TIME) == m.scale(n, rounding=ROUND_HALF_UP)
    assert c.allowed_currencies == ("USD",)
    assert not c.single_use


def test_annual_controls_are_single_use_at_the_annual_price():
    t = parse_terms(raw_terms())
    c = controls_for(t, Plan.ANNUAL, reconsideration(t, Plan.ANNUAL))
    assert c.single_use
    assert {lim.amount for lim in c.limits} == {t.annual_price}


def test_update_adds_lifetime_spend_to_all_time():
    t = parse_terms(raw_terms())
    r = reconsideration(t, Plan.MONTHLY)
    base = controls_for(t, Plan.MONTHLY, r)
    more = controls_for(t, Plan.MONTHLY, r, already_spent_all_time=Money.of("1500.00", "USD"))
    assert more.limit(Interval.ALL_TIME) == base.limit(Interval.ALL_TIME) + Money.of(
        "1500.00", "USD"
    )
