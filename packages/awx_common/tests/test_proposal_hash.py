"""The proposal hash binds the computed amounts and the risk tier."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from awx_common import ApprovalBindingError, Money, Proposal, RiskTier, approval_state

NOW = datetime(2026, 10, 6, 5, 0, tzinfo=UTC)


def make_proposal(**overrides):
    fields = {
        "kit": "k7",
        "action": "collect_repayment",
        "intent": {"week": "2026-W41"},
        "computed": {"collect": Money.of("300.00", "AUD")},
        "risk_tier": RiskTier.SINGLE,
        "reasons": ["weekly collection"],
        "proposed_by": "requester",
        "now": NOW,
        "ttl": timedelta(hours=1),
    }
    fields.update(overrides)
    return Proposal.create(**fields)


def test_hash_binds_computed_amounts_and_tier():
    p = make_proposal()
    assert p.intent_hash != make_proposal(computed={"collect": Money.of("301", "AUD")}).intent_hash
    assert p.intent_hash != make_proposal(risk_tier=RiskTier.DUAL).intent_hash
    with pytest.raises(ApprovalBindingError):
        approval_state(replace(p, computed={"collect": Money.of("900", "AUD")}), [], now=NOW)
    with pytest.raises(ApprovalBindingError):
        approval_state(replace(p, risk_tier=RiskTier.AUTO), [], now=NOW)
