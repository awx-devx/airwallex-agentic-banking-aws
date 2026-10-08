"""Kit 2's tool surface through the real ToolDispatcher (awx_agent.testing.KitToolHarness).

Every tool in TOOLS is called the way the Gateway calls it, with a signed token. Checked:
the approval binds to the proposal hash; tenant isolation (kit 2 runs as the platform, and a
caller's tenant claim never reaches a connected account); model-supplied values never choose
an amount, an account or a request_id; a re-proposal after the execution closed reports
EXISTING_PROPOSAL_*.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_common import Proposal, transport
from awx_common.approvals import ApprovalBindingError
from awx_k2_purchase import SPECS, TOOLS
from k2env import K2Env, guide_terms

FROZEN = [""]  # the world fixture's frozen card, for propose_unfreeze_card


@pytest.fixture
def world():
    env = K2Env()
    _, card_id = env.issue()
    test_card = env.limit_test_card()
    _, frozen = env.issue("frozen-ref")
    assert env.run("freeze_card", {"card_id": frozen}).state == "SUCCEEDED"
    FROZEN[0] = frozen
    # A fixed tool clock (so a retry's execution input is identical), set a little ahead of
    # the real time the harness signs tokens with, so no token's iat is in its future.
    fixed = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=120)
    h = KitToolHarness.for_kit(
        "k2", port=env.fake, clock=lambda: fixed, tenants={"tenant-a": "acct_A"}
    )
    return env, h, card_id, test_card


def _args(tool: str, card_id: str, test_card: str) -> dict:
    if tool in ("read_purchase_plan", "propose_issue_card"):
        return guide_terms(purchase_ref="tool-surface-ref")
    if tool == "propose_update_card_controls":
        return {**guide_terms(monthly_price="1200.00"), "card_id": card_id}
    if tool == "propose_run_limit_test":
        return {"card_id": test_card}
    if tool == "propose_unfreeze_card":
        return {"card_id": FROZEN[0]}
    return {"card_id": card_id}


@pytest.mark.parametrize("tool", [t.name for t in TOOLS])
def test_every_tool_runs_through_the_dispatcher_and_never_the_adapter(world, tool):
    env, h, card_id, test_card = world
    writes = len(env.posts(""))
    out = h.call(tool, _args(tool, card_id, test_card))
    assert "error" not in out, out
    if tool.startswith("read_"):
        assert "untrusted_data" in out
        assert h.sfn.calls == []
    else:
        assert out["proposal_id"]
        assert out["status"] != "BLOCKED_BY_POLICY", out["reasons"]
        assert len(h.executions) == 1
    assert {c["FunctionName"] for c in h.reader.calls} <= {"fake-reader"}
    assert ADAPTER_FN not in {c["FunctionName"] for c in h.reader.calls}
    assert len(env.posts("")) == writes  # tools read and propose; they never write


@pytest.mark.parametrize("tool", [t.name for t in TOOLS if t.name.startswith("propose_")])
def test_the_approval_is_bound_to_the_proposal_hash(world, tool):
    _, h, card_id, test_card = world
    out = h.call(tool, _args(tool, card_id, test_card))
    (execution,) = h.executions
    proposal = execution["proposal"]
    assert proposal["intent_hash"] == out["intent_hash"]
    assert proposal["proposal_id"] == out["proposal_id"]
    rebuilt = Proposal(**{**proposal, "risk_tier": _tier(proposal), "origin": _origin(proposal)})
    rebuilt.verify_hash()
    tampered = Proposal(
        **{
            **proposal,
            "risk_tier": _tier(proposal),
            "origin": _origin(proposal),
            "computed": {**proposal["computed"], "tampered": True},
        }
    )
    with pytest.raises(ApprovalBindingError):
        tampered.verify_hash()


def _tier(p):
    from awx_common import RiskTier

    return RiskTier(p["risk_tier"])


def _origin(p):
    from awx_common import Origin

    return Origin(p["origin"])


def test_a_callers_tenant_claim_never_reaches_a_connected_account(world):
    env, h, card_id, _ = world
    plain = h.call("propose_freeze_card", {"card_id": card_id})
    claimed = h.call("propose_freeze_card", {"card_id": card_id}, tenant_id="tenant-a")
    read = h.call("read_card_controls", {"card_id": card_id}, tenant_id="tenant-a")
    assert "untrusted_data" in read
    assert claimed["proposal_id"] == plain["proposal_id"]  # the same platform proposal
    assert {c["payload"]["tenant_id"] for c in h.reader.calls} == {"platform"}
    assert {e["tenant_id"] for e in h.executions} <= {None, "platform"}
    assert {obo for _, _, obo in env.fake.requests} == {None}


@pytest.mark.parametrize(
    "extra",
    [
        {"request_id": "x" * 36},
        {"account_id": "acct_A"},
        {"on_behalf_of": "acct_A"},
        {"amount": "9999.00"},
        {"limits": [{"interval": "ALL_TIME", "amount": "99999"}]},
        {"controls": {}},
        {"debit": "0.00"},
    ],
)
def test_model_values_never_choose_an_amount_account_or_request_id(world, extra):
    _, h, card_id, test_card = world
    for tool in ("propose_freeze_card", "propose_run_limit_test", "propose_issue_card"):
        out = h.call(tool, {**_args(tool, card_id, test_card), **extra})
        assert out["error"]["code"] == "INVALID_INPUT", (tool, out)
    assert h.sfn.calls == []


def test_amounts_in_a_proposal_are_computed_by_code(world):
    """The model gives the terms it read; the limits and the limit-test debit come from code."""
    _, h, _, test_card = world
    issued = h.call("propose_issue_card", guide_terms(purchase_ref="tool-surface-ref"))
    tested = h.call("propose_run_limit_test", {"card_id": test_card})
    computed = {e["proposal"]["proposal_id"]: e["proposal"]["computed"] for e in h.executions}
    assert computed[issued["proposal_id"]]["controls"]["limits"][2] == {
        "interval": "ALL_TIME",
        "amount": "4500.00",
    }
    assert computed[tested["proposal_id"]]["debit"] == {"amount": "1500.00", "currency": "USD"}


@pytest.mark.parametrize(
    ("status", "outcome", "expected"),
    [
        ("SUCCEEDED", None, "EXISTING_PROPOSAL_EXECUTION_SUCCEEDED"),
        ("FAILED", None, "EXISTING_PROPOSAL_EXECUTION_FAILED"),
        ("SUCCEEDED", "REJECTED", "EXISTING_PROPOSAL_REJECTED"),
    ],
)
def test_a_reproposal_after_the_execution_closed_reports_the_existing_one(
    world, status, outcome, expected
):
    _, h, card_id, _ = world
    first = h.call("propose_freeze_card", {"card_id": card_id})
    (arn,) = h.sfn.executions
    h.sfn.status[arn] = status
    if outcome:
        h.outcomes[first["proposal_id"]] = outcome
    again = h.call("propose_freeze_card", {"card_id": card_id})
    assert again["proposal_id"] == first["proposal_id"]
    assert again["existing_proposal"] is True
    assert again["status"] == expected
    assert len(h.sfn.executions) == 1


def test_every_propose_tool_maps_to_one_state_machine(world):
    _, h, _, _ = world
    assert sorted(h.dispatcher.specs) == sorted(SPECS)
    raw = transport.dumps({"ok": True})
    assert transport.loads(raw) == {"ok": True}
