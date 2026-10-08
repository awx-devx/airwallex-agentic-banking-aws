"""Proposals and approval decisions.

A Proposal's intent_hash covers the kit, the action, the intent, the amounts the policy code
computed and the risk tier, so an approval is bound to exactly what the approver was shown.
The workflow re-runs policy before executing and fails closed if that hash changes. A decision
only counts when it names the same proposal and hash, comes from someone other than the
proposer and lands before expiry. Any mismatch is tampering and raises ApprovalBindingError.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .intent import intent_hash


class RiskTier(StrEnum):
    AUTO = "AUTO"
    SINGLE = "SINGLE_APPROVER"
    DUAL = "DUAL_APPROVER"
    BLOCKED = "BLOCKED"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ApprovalState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class ApprovalBindingError(Exception):
    """A decision does not bind to this proposal."""


class Origin(StrEnum):
    """Who started a proposal. Always set server-side, never from model output."""

    AGENT = "AGENT"  # a propose_* tool call made by the model
    SCHEDULE = "SCHEDULE"  # a scheduled system trigger
    OPERATOR = "OPERATOR"  # a signed-in person using the CLI directly, no model involved


_TIER_ORDER = {RiskTier.AUTO: 0, RiskTier.SINGLE: 1, RiskTier.DUAL: 2, RiskTier.BLOCKED: 3}


def effective_tier(policy_tier: RiskTier, origin: Origin, agent_min_tier: RiskTier) -> RiskTier:
    """The tier a proposal really gets. A model-started proposal never falls below the floor.

    Text the model read (an email, an evidence file, a tool result) can steer which action it
    proposes. With the default floor of SINGLE, nothing the model starts executes without a
    human approval. A kit that sets its floor to AUTO for an action must justify it.
    """
    if agent_min_tier is RiskTier.BLOCKED:
        raise ValueError("agent_min_tier cannot be BLOCKED")
    if origin is Origin.AGENT and _TIER_ORDER[policy_tier] < _TIER_ORDER[agent_min_tier]:
        return agent_min_tier
    return policy_tier


PROPOSAL_ID_NAMESPACE = uuid.UUID("9b0f4e2a-6c1d-4a8e-b7f3-2d5c8e1a9f60")


def derive_proposal_id(
    *,
    kit: str,
    action: str,
    intent: Mapping[str, Any],
    computed: Mapping[str, Any],
    tenant_id: str,
    proposed_by: str,
    now: datetime,
    ttl: timedelta,
) -> str:
    """Deterministic proposal ID, used as the Step Functions execution name.

    A model retry of the same proposal (same intent and same computed values) inside one
    approval window gets the same ID, so it reaches the existing execution and cannot start a
    second run. The ID covers computed, so a new business action whose computed values differ
    (for example the next card change number) always gets a new ID and is never swallowed by
    an earlier execution with the same intent.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    seconds = int(ttl.total_seconds())
    if seconds <= 0:
        raise ValueError("ttl must be positive")
    window = int(now.timestamp()) // seconds
    key = intent_hash(
        {
            "kit": kit,
            "action": action,
            "intent": intent,
            "computed": computed,
            "tenant_id": tenant_id,
            "proposed_by": proposed_by,
            "window": window,
        }
    )
    return str(uuid.uuid5(PROPOSAL_ID_NAMESPACE, key))


def proposal_hash(
    kit: str,
    action: str,
    intent: Mapping[str, Any],
    computed: Mapping[str, Any] | None = None,
    risk_tier: RiskTier | None = None,
    origin: Origin | None = None,
) -> str:
    """Use Proposal.create and Proposal.verify_hash rather than calling this directly."""
    return intent_hash(
        {
            "kit": kit,
            "action": action,
            "intent": intent,
            "computed": computed or {},
            "risk_tier": risk_tier.value if risk_tier is not None else None,
            "origin": origin.value if origin is not None else None,
        }
    )


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    kit: str
    action: str
    intent: Mapping[str, Any]
    computed: Mapping[str, Any]
    intent_hash: str
    risk_tier: RiskTier
    reasons: tuple[str, ...]
    proposed_by: str
    created_at: datetime
    expires_at: datetime
    origin: Origin = Origin.AGENT

    @classmethod
    def create(
        cls,
        *,
        kit: str,
        action: str,
        intent: Mapping[str, Any],
        risk_tier: RiskTier,
        reasons: Sequence[str],
        proposed_by: str,
        now: datetime,
        ttl: timedelta,
        computed: Mapping[str, Any] | None = None,
        proposal_id: str | None = None,
        origin: Origin = Origin.AGENT,
    ) -> Proposal:
        """risk_tier must already be the effective tier (see effective_tier)."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        computed = dict(computed or {})
        return cls(
            proposal_id=proposal_id or str(uuid.uuid4()),
            kit=kit,
            action=action,
            intent=intent,
            computed=computed,
            intent_hash=proposal_hash(kit, action, intent, computed, risk_tier, origin),
            risk_tier=risk_tier,
            reasons=tuple(reasons),
            proposed_by=proposed_by,
            created_at=now,
            expires_at=now + ttl,
            origin=origin,
        )

    def verify_hash(self) -> None:
        expected = proposal_hash(
            self.kit, self.action, self.intent, self.computed, self.risk_tier, self.origin
        )
        if expected != self.intent_hash:
            raise ApprovalBindingError("proposal no longer matches its hash")


@dataclass(frozen=True)
class ApprovalDecision:
    proposal_id: str
    intent_hash: str
    approver: str
    decision: Decision
    decided_at: datetime


def approval_state(
    proposal: Proposal, decisions: Sequence[ApprovalDecision], *, now: datetime
) -> ApprovalState:
    proposal.verify_hash()
    if proposal.risk_tier is RiskTier.BLOCKED:
        return ApprovalState.BLOCKED
    if proposal.risk_tier is RiskTier.AUTO:
        return ApprovalState.NOT_REQUIRED
    for d in decisions:
        if d.proposal_id != proposal.proposal_id or d.intent_hash != proposal.intent_hash:
            raise ApprovalBindingError("decision is for a different proposal or intent")
        if d.approver == proposal.proposed_by:
            raise ApprovalBindingError("the proposer cannot approve their own proposal")
        if not proposal.created_at <= d.decided_at <= proposal.expires_at:
            raise ApprovalBindingError("decision falls outside the proposal's validity window")
    if any(d.decision is Decision.REJECT for d in decisions):
        return ApprovalState.REJECTED
    approvers = {d.approver for d in decisions if d.decision is Decision.APPROVE}
    needed = 2 if proposal.risk_tier is RiskTier.DUAL else 1
    if len(approvers) >= needed:
        return ApprovalState.APPROVED
    if now > proposal.expires_at:
        return ApprovalState.EXPIRED
    return ApprovalState.PENDING
