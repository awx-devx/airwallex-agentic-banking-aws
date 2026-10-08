"""The approval matrix, cell by cell (policy.py docstring)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from awx_common import RiskTier
from awx_k4_dispute import SPECS
from hypothesis import given, settings
from hypothesis import strategies as st
from k4_harness import NOW, World, challenge_input, fact, make_ctx, money


def decide(action, raw, port):
    spec = SPECS[action]
    return spec.policy(spec.validate(raw), make_ctx(port, spec, NOW))


def accept(dispute_id, amount, reason="INSUFFICIENT_EVIDENCE"):
    return {
        "dispute_id": dispute_id,
        "disputed_amount": money(amount),
        "reason": reason,
        "rationale": "r",
    }


@pytest.fixture
def p():
    port = World()
    port.add_dispute("d_small", amount="15.00", reason={"code": "13.1"})
    port.add_dispute("d_mid", amount="900.00", reason={"code": "10.4"})
    port.add_dispute("d_big", amount="1000.00", reason={"code": "10.4"})
    return port


@pytest.mark.parametrize(
    ("dispute", "amount", "reason", "tier"),
    [
        ("d_small", "15.00", "LOW_VALUE", RiskTier.SINGLE),
        ("d_mid", "900.00", "INSUFFICIENT_EVIDENCE", RiskTier.SINGLE),
        ("d_big", "1000.00", "INSUFFICIENT_EVIDENCE", RiskTier.DUAL),
        ("d_mid", "900.00", "LOW_VALUE", RiskTier.BLOCKED),
        ("d_mid", "900.00", "AFTER_ISSUER_REJECTION", RiskTier.BLOCKED),  # still at RFI
        ("d_mid", "899.99", "INSUFFICIENT_EVIDENCE", RiskTier.BLOCKED),  # amount drift
    ],
)
def test_accept_matrix(p, dispute, amount, reason, tier):
    assert decide("accept_dispute", accept(dispute, amount, reason), p).risk_tier is tier


def test_accept_is_never_auto(p):
    for d, a in (("d_small", "15.00"), ("d_mid", "900.00"), ("d_big", "1000.00")):
        assert decide("accept_dispute", accept(d, a), p).risk_tier is not RiskTier.AUTO


def test_accept_blocked_when_not_respondable_or_late(p):
    p.disputes["d_mid"]["status"] = "CHALLENGED"
    assert decide("accept_dispute", accept("d_mid", "900.00"), p).risk_tier is RiskTier.BLOCKED
    p.disputes["d_small"]["due_at"] = (NOW + timedelta(minutes=30)).isoformat()
    assert decide("accept_dispute", accept("d_small", "15.00", "LOW_VALUE"), p).risk_tier is (
        RiskTier.BLOCKED
    )


def test_accept_blocked_for_unconfigured_currency(p):
    p.add_dispute("d_eur", amount="10.00", currency="EUR")
    raw = {
        "dispute_id": "d_eur",
        "disputed_amount": money("10.00", "EUR"),
        "reason": "LOW_VALUE",
        "rationale": "r",
    }
    assert decide("accept_dispute", raw, p).risk_tier is RiskTier.BLOCKED


def test_challenge_matrix(p):
    assert decide("challenge_dispute", challenge_input("d_mid"), p).risk_tier is RiskTier.SINGLE
    assert decide("challenge_dispute", challenge_input("d_big", "1000.00"), p).risk_tier is (
        RiskTier.DUAL
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw, port: raw.update(reason="CREDIT_ALREADY_PROCESSED"),  # wrong family
        lambda raw, port: raw.update(evidence_ids=raw["evidence_ids"][:1]),  # below minimum files
        lambda raw, port: raw.update(facts=[]),  # nothing verified
        lambda raw, port: port.disputes["d_mid"].update(challenges=[{"id": "c"}]),
        lambda raw, port: port.disputes["d_mid"].update(
            due_at=(NOW + timedelta(hours=23)).isoformat()
        ),  # inside the 24h margin
        lambda raw, port: raw.update(
            facts=[fact("IP_MATCH_PRIOR", "ev_order_history", "IP address 203.0.113.24")]
        ),  # weak band
    ],
)
def test_challenge_blocked(p, mutate):
    raw = challenge_input("d_mid")
    mutate(raw, p)
    assert decide("challenge_dispute", raw, p).risk_tier is RiskTier.BLOCKED


def test_challenge_against_refer_economics_needs_two(p):
    # Moderate evidence on a mid-size amount sits in the indifference band -> REFER -> DUAL.
    p.add_dispute("d_edge", amount="96.00", reason={"code": "10.4"})
    facts = [f for f in challenge_input("d_edge")["facts"] if f["kind"] != "SIGNED_DELIVERY"]
    raw = challenge_input("d_edge", "96.00", facts=facts)
    d = decide("challenge_dispute", raw, p)
    assert d.computed["recommendation"] == "REFER"
    assert d.risk_tier is RiskTier.DUAL


def test_refer_is_auto_and_close_needs_terminal(p):
    assert decide("refer_dispute", {"dispute_id": "d_mid", "summary": "s"}, p).risk_tier is (
        RiskTier.AUTO
    )
    close = {"dispute_id": "d_mid", "close_reason": "LOST_ISSUER_REJECTED_EVIDENCE"}
    assert decide("close_dispute", close, p).risk_tier is RiskTier.BLOCKED
    p.resolve("d_mid", "LOST")
    assert decide("close_dispute", close, p).risk_tier is RiskTier.AUTO


@settings(max_examples=60)
@given(
    minor=st.integers(min_value=1, max_value=50_000_000),
    accept_reason=st.sampled_from(
        ["LOW_VALUE", "INSUFFICIENT_EVIDENCE", "MERCHANT_ERROR", "AFTER_ISSUER_REJECTION"]
    ),
    signed=st.booleans(),
)
def test_irreversible_actions_are_never_auto(minor, accept_reason, signed):
    from awx_common import Money

    amount = format(Money.from_minor(minor, "AUD").amount, "f")
    port = World()
    port.add_dispute("d", amount=amount, reason={"code": "10.4"}, stage="CHARGEBACK")
    a = decide("accept_dispute", accept("d", amount, accept_reason), port)
    facts = [f for f in challenge_input("d")["facts"] if signed or f["kind"] != "SIGNED_DELIVERY"]
    c = decide("challenge_dispute", challenge_input("d", amount, facts=facts), port)
    assert a.risk_tier is not RiskTier.AUTO
    assert c.risk_tier is not RiskTier.AUTO
    big = Money.of(amount, "AUD") >= Money.of("1000.00", "AUD")
    if big:
        assert a.risk_tier in (RiskTier.DUAL, RiskTier.BLOCKED)
        assert c.risk_tier in (RiskTier.DUAL, RiskTier.BLOCKED)
