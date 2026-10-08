"""Economics: unit tests on the guide's cases and property tests on every money rule."""

from __future__ import annotations

from decimal import Decimal

import pytest
from awx_common import Money
from awx_common.money import minor_units
from awx_k4_dispute.config import BAND_ORDER, DEFAULT_CONFIG, DisputeConfig, StrengthBand, _terms
from awx_k4_dispute.economics import Recommendation, UnsupportedCurrency, compute_economics
from hypothesis import given
from hypothesis import strategies as st

AUD = "AUD"
bands = st.sampled_from(list(StrengthBand))
amounts = st.integers(min_value=1, max_value=10_000_000).map(lambda m: Money.from_minor(m, AUD))
JPY_CONFIG = DisputeConfig(
    terms={**DEFAULT_CONFIG.terms, "JPY": _terms("JPY", "2500", "2000", "200", "2000", "100000")}
)


def test_strong_fraud_case_contests():
    e = compute_economics(Money.of("900.00", AUD), StrengthBand.STRONG, DEFAULT_CONFIG)
    assert e.cost_accept == Money.of("902.00", AUD)
    assert e.cost_contest_best == Money.of("45.00", AUD)
    assert e.cost_contest_worst == Money.of("945.00", AUD)
    assert e.cost_contest_expected == Money.of("270.00", AUD)
    assert e.expected_saving == Money.of("632.00", AUD)
    assert e.recommendation is Recommendation.CONTEST


def test_small_case_accepts_because_fee_exceeds_amount():
    e = compute_economics(Money.of("15.00", AUD), StrengthBand.STRONG, DEFAULT_CONFIG)
    assert not e.contest_can_beat_accept
    assert e.recommendation is Recommendation.ACCEPT


def test_merchant_fault_refers():
    e = compute_economics(
        Money.of("180.00", AUD), StrengthBand.MODERATE, DEFAULT_CONFIG, merchant_fault=True
    )
    assert e.recommendation is Recommendation.REFER


def test_indifference_band_refers():
    # MODERATE p=0.45: saving = A + 2 - (45 + 0.55A) = 0.45A - 43; zero near A=95.56
    e = compute_economics(Money.of("100.00", AUD), StrengthBand.MODERATE, DEFAULT_CONFIG)
    assert abs(e.expected_saving) < DEFAULT_CONFIG.terms[AUD].min_saving_to_contest
    assert e.recommendation is Recommendation.REFER


def test_fee_on_accept_adds_fee_to_accept_cost():
    cfg = DisputeConfig(fee_on_accept=True)
    e = compute_economics(Money.of("15.00", AUD), StrengthBand.NONE, cfg)
    assert e.cost_accept == Money.of("42.00", AUD)


def test_unsupported_currency_and_bad_amounts():
    with pytest.raises(UnsupportedCurrency):
        compute_economics(Money.of("10.00", "EUR"), StrengthBand.STRONG, DEFAULT_CONFIG)
    with pytest.raises(ValueError, match="positive"):
        compute_economics(Money.zero(AUD), StrengthBand.STRONG, DEFAULT_CONFIG)


def test_config_rejects_falling_probabilities():
    with pytest.raises(ValueError, match="fall"):
        DisputeConfig(
            win_probability={
                StrengthBand.NONE: Decimal("0"),
                StrengthBand.WEAK: Decimal("0.5"),
                StrengthBand.MODERATE: Decimal("0.4"),
                StrengthBand.STRONG: Decimal("0.9"),
            }
        )


@given(amount=amounts, band=bands)
def test_expected_cost_lies_between_best_and_worst(amount, band):
    e = compute_economics(amount, band, DEFAULT_CONFIG)
    assert e.cost_contest_best <= e.cost_contest_expected <= e.cost_contest_worst
    assert e.expected_saving == e.cost_accept - e.cost_contest_expected
    assert e.cost_contest_worst == e.cost_contest_best + amount


@given(amount=amounts, band=bands)
def test_every_amount_is_exact_at_minor_units(amount, band):
    e = compute_economics(amount, band, DEFAULT_CONFIG)
    for m in (e.cost_accept, e.cost_contest_best, e.cost_contest_expected, e.expected_saving):
        assert m.currency == AUD
        assert m.amount.as_tuple().exponent == -minor_units(AUD)


@given(amount=amounts)
def test_stronger_evidence_never_raises_expected_cost(amount):
    costs = [
        compute_economics(amount, b, DEFAULT_CONFIG).cost_contest_expected
        for b in sorted(StrengthBand, key=BAND_ORDER.__getitem__)
    ]
    assert costs == sorted(costs, reverse=True)


@given(amount=amounts)
def test_contest_recommendation_is_monotonic_in_band(amount):
    recs = [
        compute_economics(amount, b, DEFAULT_CONFIG).recommendation
        for b in sorted(StrengthBand, key=BAND_ORDER.__getitem__)
    ]
    first = next((i for i, r in enumerate(recs) if r is Recommendation.CONTEST), None)
    if first is not None:
        assert all(r is Recommendation.CONTEST for r in recs[first:])


@given(amount=amounts, band=bands, fee_on_accept=st.booleans())
def test_never_contest_when_fee_and_handling_meet_accept_cost(amount, band, fee_on_accept):
    cfg = DisputeConfig(fee_on_accept=fee_on_accept)
    e = compute_economics(amount, band, cfg)
    if e.cost_contest_best >= e.cost_accept:
        assert e.recommendation is Recommendation.ACCEPT
    if e.recommendation is Recommendation.CONTEST:
        assert e.expected_saving >= cfg.terms[AUD].min_saving_to_contest
        assert BAND_ORDER[band] >= BAND_ORDER[cfg.min_band_to_contest]


@given(amount=amounts, band=bands, fault=st.booleans(), contra=st.booleans())
def test_fault_or_contradiction_never_contests(amount, band, fault, contra):
    e = compute_economics(amount, band, DEFAULT_CONFIG, merchant_fault=fault, contradictory=contra)
    if fault or contra:
        assert e.recommendation is not Recommendation.CONTEST


@given(minor=st.integers(min_value=1, max_value=10_000_000), band=bands)
def test_zero_decimal_currency_stays_exact(minor, band):
    e = compute_economics(Money.from_minor(minor, "JPY"), band, JPY_CONFIG)
    assert e.cost_contest_expected.amount.as_tuple().exponent == 0
    assert e.cost_contest_best <= e.cost_contest_expected <= e.cost_contest_worst
