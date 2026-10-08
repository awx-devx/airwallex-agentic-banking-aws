"""Kit 5's tool surface through the real ToolDispatcher (awx_agent.testing.KitToolHarness):
Cognito-shaped tokens, the per-kit reader, the propose path into Step Functions. The kit's
setup document and capital ledger are seeded into the platform store, as an operator does at
deploy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from awx_agent.testing import KitToolHarness
from awx_common import ApprovalBindingError, ToolKind
from awx_common.approvals import ApprovalDecision, Decision, approval_state
from awx_k5_platform_spend import TOOLS
from awx_spine_aws import codec
from k5_support import PROPOSE_INPUTS, READ_INPUTS, Platform, obligation, seed_by_operator


class Surface:
    def __init__(self) -> None:
        self.p = Platform(
            obligations=(
                obligation("t-alpha", "ob-a1", "500"),
                obligation("t-bravo", "ob-b1", "400"),
            )
        )
        self.h = KitToolHarness.for_kit(
            "k5",
            port=self.p.fake,
            tenants=dict(self.p.acct),
            clock=self.p.now,
        )
        # Empty harness stores, seeded only through the shipped operator commands.
        seed_by_operator(self.p, self.h.store("platform"))
        self.seeded_at = len(self.p.fake.requests)  # operator reads before this point

    def call(
        self, tool: str, args: dict[str, Any], tenant: str = "t-alpha", user: str = "user-requester"
    ):
        token = self.h.issuer.token(user, tenant_id=tenant, now=self.p.now().timestamp())
        return self.h.call(tool, args, token=token)


@pytest.fixture
def s() -> Surface:
    return Surface()


def test_every_tool_is_covered_here():
    assert {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE} == set(PROPOSE_INPUTS)
    assert {t.name for t in TOOLS if t.kind is ToolKind.READ} == set(READ_INPUTS)


@pytest.mark.parametrize("tool", sorted(READ_INPUTS))
def test_read_tools_run_through_the_reader_for_the_callers_tenant_only(s, tool):
    out = s.call(tool, READ_INPUTS[tool])
    assert "error" not in out, out
    assert "acct_" not in str(out["untrusted_data"])
    for _method, _path, obo in [r for r in s.p.fake.requests[s.seeded_at :] if r[0] == "GET"]:
        assert obo == s.p.acct["t-alpha"]  # no read ever names another tenant's account


@pytest.mark.parametrize("tool", sorted(PROPOSE_INPUTS))
def test_every_propose_tool_binds_its_approval_to_the_proposal_hash(s, tool):
    out = s.call(tool, PROPOSE_INPUTS[tool])
    assert "error" not in out, out
    if out["risk_tier"] == "BLOCKED":  # e.g. no card issued yet, nothing in flight
        assert out["reasons"]
        return
    execution = s.h.executions[-1]
    proposal = execution["proposal"]
    if not hasattr(proposal, "verify_hash"):
        proposal = codec.proposal_from(proposal)
    proposal.verify_hash()
    assert proposal.intent_hash == out["intent_hash"]
    assert proposal.proposal_id == out["proposal_id"]
    # The hash covers intent and computed: any change to what the approver saw breaks it.
    tampered = replace(proposal, intent={**proposal.intent, "rationale": "tampered"})
    with pytest.raises(ApprovalBindingError):
        tampered.verify_hash()
    if proposal.computed:
        key = sorted(proposal.computed)[0]
        changed = replace(proposal, computed={**proposal.computed, key: "tampered"})
        with pytest.raises(ApprovalBindingError):
            changed.verify_hash()
    if out["risk_tier"] in ("SINGLE_APPROVER", "DUAL_APPROVER"):
        assert out["status"] == "AWAITING_APPROVAL"
        wrong = ApprovalDecision(
            proposal.proposal_id, "0" * 64, "approver-1", Decision.APPROVE, proposal.created_at
        )
        with pytest.raises(ApprovalBindingError):  # a decision for another hash is refused
            approval_state(proposal, [wrong], now=proposal.created_at)
    else:  # AUTO only where DESIGN.md justifies it
        assert tool in ("propose_register_shortfall", "propose_reconcile_bridge")


def test_bridge_proposal_amount_comes_from_code_not_the_model(s):
    out = s.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"})
    assert out["summary"]["computed_by_code"]["amount"] == {"amount": "300.00", "currency": "USD"}
    assert out["risk_tier"] == "SINGLE_APPROVER"


OWNED_OR_COMPUTED = (
    {"amount": "1.00"},
    {"connected_account_id": "acct_x"},
    {"destination": "acct_x"},
    {"source": "acct_x"},
    {"request_id": "rid-1"},
    {"tenant_id": "t-bravo"},
    {"shortfall": "999"},
    {"fee": "1.00"},
    {"per_transaction_limit": "9999"},
)


@pytest.mark.parametrize("tool", sorted(PROPOSE_INPUTS))
@pytest.mark.parametrize("extra", OWNED_OR_COMPUTED, ids=lambda e: next(iter(e)))
def test_model_supplied_values_never_choose_amount_account_or_request_id(s, tool, extra):
    if set(extra) <= set(PROPOSE_INPUTS[tool]):
        pytest.skip("a legitimate input of this tool (e.g. the simulation's own amount)")
    out = s.call(tool, {**PROPOSE_INPUTS[tool], **extra})
    assert "error" in out, out
    assert out["error"]["code"] in ("INVALID_INPUT", "NOT_AUTHORISED")
    assert s.h.executions == []


PLATFORM_FIGURES = ("1000.00", "400.00", "600.00", "1000 ", "headroom", "ranked", "of 2")


def test_no_platform_or_other_tenant_figures_reach_a_tenant(s):
    """Policy reasons, computed values and read replies reach the tenant caller: none may carry
    the platform's capital (1000), headroom (400), reserve floor (600), the high-risk threshold,
    the rank among other tenants' requests, or bravo's figures."""
    s.call("propose_register_shortfall", {"obligation_ref": "ob-b1"}, tenant="t-bravo")
    bravo_short = "287.17"
    outputs = []
    for tool in sorted(READ_INPUTS):
        outputs.append(s.call(tool, READ_INPUTS[tool]))
    for tool in sorted(PROPOSE_INPUTS):
        outputs.append(s.call(tool, PROPOSE_INPUTS[tool]))
    big = s.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"})  # rank 2 of 2 today
    outputs.append(big)
    for out in outputs:
        assert "error" not in out, out
        text = str(out)
        for figure in (*PLATFORM_FIGURES, bravo_short, "t-bravo"):
            assert figure not in text, (figure, out.get("tool"), text[:400])


def test_tenant_isolation_on_the_propose_path(s):
    out = s.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-bravo")
    assert out["error"]["code"] == "TENANT_SCOPE"  # bravo cannot name alpha's obligation
    assert s.h.executions == []
    unknown = s.call("propose_collect_fee", {"period": "2026-10"}, tenant="t-unknown")
    assert "error" in unknown
    no_claim = s.h.call(
        "propose_collect_fee",
        {"period": "2026-10"},
        token=s.h.issuer.token("user-requester", now=s.p.now().timestamp()),
    )
    assert no_claim["error"]["code"] == "NOT_AUTHORISED"  # kits 5-8 need a tenant claim


def test_a_reproposal_after_the_execution_closed_reports_the_existing_proposal(s):
    first = s.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"})
    assert first["status"] == "AWAITING_APPROVAL", first
    (arn,) = s.h.sfn.executions
    s.h.sfn.status[arn] = "SUCCEEDED"
    s.h.outcomes[first["proposal_id"]] = "SUCCEEDED"
    again = s.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"})
    assert again["status"] == "EXISTING_PROPOSAL_SUCCEEDED"
    assert again["proposal_id"] == first["proposal_id"]
    assert again["existing_proposal"] is True
    assert len(s.h.sfn.executions) == 1


def test_the_tool_plane_never_writes_kit_state(s):
    before = {k: [(i.key, i.version) for i in v.query("")] for k, v in s.h.stores.items()}
    for tool, args in {**READ_INPUTS, **PROPOSE_INPUTS}.items():
        s.call(tool, args)
    after = {k: [(i.key, i.version) for i in v.query("")] for k, v in s.h.stores.items()}
    assert {k: v for k, v in after.items() if v} == {k: v for k, v in before.items() if v}
    assert all(r[0] == "GET" for r in s.p.fake.requests)  # nothing was sent to Airwallex
