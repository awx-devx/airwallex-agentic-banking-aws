from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from awx_common import (
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalState,
    Decision,
    Operation,
    OperationResult,
    OperationStatus,
    Proposal,
    ReadRequest,
    RiskTier,
    TenantContext,
    ToolKind,
    ToolSpec,
    approval_state,
    bind_operation,
    derive_request_id,
)
from awx_common.errors import InsufficientFunds, TenantScopeViolation

NOW = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
HASH = "a" * 64


def make_op(**overrides):
    rid = derive_request_id("k5", "payout:1")
    fields = {
        "kit": "k5",
        "name": "transfers.create",
        "method": "POST",
        "path": "/api/v1/transfers/create",
        "body": {"request_id": rid, "transfer_amount": Decimal("10.00")},
        "request_id": rid,
        "business_key": "payout:1",
        "intent_hash": HASH,
    }
    fields.update(overrides)
    return Operation(**fields)


def test_operation_validates_shape():
    assert make_op().body["transfer_amount"] == Decimal("10.00")
    with pytest.raises(ValueError, match="unknown kit"):
        make_op(kit="k9")
    with pytest.raises(ValueError, match="writes"):
        make_op(method="GET")
    with pytest.raises(ValueError, match="request_id"):
        make_op(body={"request_id": "other"})
    with pytest.raises(ValueError, match="/api/v1/"):
        ReadRequest(path="/balances/current")


def test_result_rebuilds_normalised_error():
    result = OperationResult(
        request_id="r",
        status=OperationStatus.FAILED,
        http_status=400,
        error_type="InsufficientFunds",
        error_code="insufficient_fund",
        error_message="Insufficient funds",
    )
    err = result.error()
    assert isinstance(err, InsufficientFunds)
    assert err.code == "insufficient_fund"
    assert OperationResult(request_id="r", status=OperationStatus.SUCCEEDED).error() is None


def test_tenant_binding_is_server_owned():
    tenant = TenantContext(tenant_id="t-1", connected_account_id="acct_1")
    assert bind_operation(make_op(), tenant).on_behalf_of == "acct_1"
    assert bind_operation(make_op(on_behalf_of="acct_1"), tenant).on_behalf_of == "acct_1"
    with pytest.raises(TenantScopeViolation):
        bind_operation(make_op(on_behalf_of="acct_2"), tenant)


def proposal(tier=RiskTier.SINGLE):
    return Proposal.create(
        kit="k1",
        action="fx.convert",
        intent={"sell": "1000.00 AUD"},
        risk_tier=tier,
        reasons=["above auto limit"],
        proposed_by="requester",
        now=NOW,
        ttl=timedelta(hours=1),
    )


def decide(p, approver, decision=Decision.APPROVE, at=NOW + timedelta(minutes=5), **kw):
    return ApprovalDecision(
        proposal_id=kw.get("proposal_id", p.proposal_id),
        intent_hash=kw.get("intent_hash", p.intent_hash),
        approver=approver,
        decision=decision,
        decided_at=at,
    )


def test_tiers():
    assert approval_state(proposal(RiskTier.AUTO), [], now=NOW) is ApprovalState.NOT_REQUIRED
    assert approval_state(proposal(RiskTier.BLOCKED), [], now=NOW) is ApprovalState.BLOCKED
    p = proposal()
    assert approval_state(p, [], now=NOW) is ApprovalState.PENDING
    assert approval_state(p, [decide(p, "approver")], now=NOW) is ApprovalState.APPROVED
    assert approval_state(p, [], now=NOW + timedelta(hours=2)) is ApprovalState.EXPIRED


def test_dual_needs_two_distinct_approvers_and_any_reject_wins():
    p = proposal(RiskTier.DUAL)
    one = decide(p, "a1")
    assert approval_state(p, [one, decide(p, "a1")], now=NOW) is ApprovalState.PENDING
    assert approval_state(p, [one, decide(p, "a2")], now=NOW) is ApprovalState.APPROVED
    rejected = [one, decide(p, "a2"), decide(p, "a3", Decision.REJECT)]
    assert approval_state(p, rejected, now=NOW) is ApprovalState.REJECTED


@pytest.mark.parametrize(
    "bad",
    [
        {"approver": "requester"},
        {"approver": "x", "intent_hash": "b" * 64},
        {"approver": "x", "proposal_id": "other"},
        {"approver": "x", "at": NOW + timedelta(hours=2)},
    ],
)
def test_binding_violations_raise(bad):
    p = proposal()
    bad = dict(bad)
    approver = bad.pop("approver")
    with pytest.raises(ApprovalBindingError):
        approval_state(p, [decide(p, approver, **bad)], now=NOW)


def test_tampered_intent_detected():
    p = proposal()
    tampered = Proposal(**{**p.__dict__, "intent": {"sell": "9000.00 AUD"}})
    with pytest.raises(ApprovalBindingError):
        approval_state(tampered, [], now=NOW)


def schema(**props):
    return {"type": "object", "additionalProperties": False, "properties": props}


def test_tool_spec_guards():
    ToolSpec("read_balances", ToolKind.READ, "Current balances", schema())
    with pytest.raises(ValueError, match="prefix"):
        ToolSpec("read_balances", ToolKind.PROPOSE, "x", schema())
    with pytest.raises(ValueError, match="additionalProperties"):
        ToolSpec("read_x", ToolKind.READ, "x", {"type": "object"})
    with pytest.raises(ValueError, match="server-owned"):
        ToolSpec(
            "propose_payout",
            ToolKind.PROPOSE,
            "x",
            schema(payout={"type": "object", "properties": {"on_behalf_of": {"type": "string"}}}),
        )
