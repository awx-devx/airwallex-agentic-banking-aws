"""Proposal origin, the agent approval floor (rule 11) and deterministic proposal ids."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from awx_common import ApprovalBindingError, Proposal, RiskTier, approval_state
from awx_common.approvals import Origin, derive_proposal_id, effective_tier

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("policy", "origin", "floor", "expected"),
    [
        (RiskTier.AUTO, Origin.AGENT, RiskTier.SINGLE, RiskTier.SINGLE),
        (RiskTier.AUTO, Origin.SCHEDULE, RiskTier.SINGLE, RiskTier.AUTO),
        (RiskTier.AUTO, Origin.OPERATOR, RiskTier.SINGLE, RiskTier.AUTO),
        (RiskTier.AUTO, Origin.AGENT, RiskTier.AUTO, RiskTier.AUTO),
        (RiskTier.SINGLE, Origin.AGENT, RiskTier.DUAL, RiskTier.DUAL),
        (RiskTier.DUAL, Origin.AGENT, RiskTier.SINGLE, RiskTier.DUAL),
        (RiskTier.BLOCKED, Origin.AGENT, RiskTier.SINGLE, RiskTier.BLOCKED),
    ],
)
def test_effective_tier(policy, origin, floor, expected):
    assert effective_tier(policy, origin, floor) is expected


def test_floor_cannot_be_blocked():
    with pytest.raises(ValueError, match="BLOCKED"):
        effective_tier(RiskTier.AUTO, Origin.AGENT, RiskTier.BLOCKED)


def test_origin_is_bound_into_the_hash():
    p = Proposal.create(
        kit="k1",
        action="fund_obligation",
        intent={"obligation": "inv-1"},
        risk_tier=RiskTier.SINGLE,
        reasons=["agent floor"],
        proposed_by="u1",
        now=NOW,
        ttl=timedelta(hours=1),
    )
    assert p.origin is Origin.AGENT
    with pytest.raises(ApprovalBindingError):
        approval_state(replace(p, origin=Origin.SCHEDULE), [], now=NOW)


def test_proposal_id_is_stable_within_a_window():
    kwargs = {
        "kit": "k1",
        "action": "fund_obligation",
        "intent": {"obligation": "inv-1"},
        "computed": {"amount": "100.00"},
        "tenant_id": "platform",
        "proposed_by": "u1",
        "ttl": timedelta(hours=1),
    }
    window_start = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
    first = derive_proposal_id(now=window_start + timedelta(minutes=1), **kwargs)
    assert first == derive_proposal_id(now=window_start + timedelta(minutes=59), **kwargs)
    assert first != derive_proposal_id(now=window_start + timedelta(minutes=61), **kwargs)
    assert first != derive_proposal_id(
        now=window_start, **{**kwargs, "intent": {"obligation": "inv-2"}}
    )
    with pytest.raises(ValueError, match="timezone"):
        derive_proposal_id(now=datetime(2026, 10, 6), **kwargs)  # noqa: DTZ001


def test_proposal_id_changes_when_computed_changes():
    # Kit 2: freeze, unfreeze, freeze again with the same reason inside one window. The
    # second freeze has the next change number in computed, so it must get its own ID
    # instead of reaching the first freeze's finished execution.
    kwargs = {
        "kit": "k2",
        "action": "freeze_card",
        "intent": {"card_id": "c1", "reason": "lost"},
        "tenant_id": "t1",
        "proposed_by": "u1",
        "now": datetime(2026, 10, 6, 6, 5, tzinfo=UTC),
        "ttl": timedelta(minutes=30),
    }
    first = derive_proposal_id(computed={"change_seq": 1}, **kwargs)
    assert first == derive_proposal_id(computed={"change_seq": 1}, **kwargs)
    assert first != derive_proposal_id(computed={"change_seq": 3}, **kwargs)
