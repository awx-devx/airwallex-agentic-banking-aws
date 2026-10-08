"""Each proposal records an approver_tenant from its spec; the approval API lets only that
tenant decide it, and the proposal's own tenant may still read it."""

from __future__ import annotations

import dataclasses
import importlib
import json

import pytest
from awx_common import RiskTier
from awx_common.tenancy import CONNECTED_ACCOUNT_KITS
from awx_common.transport import decode
from awx_common.workflow import ApproverScope, approver_tenant
from awx_spine_aws import engine
from awx_spine_aws.handlers import approval
from awx_spine_aws.repos import DecisionRepo, ProposalRepo

import toy
from aws_harness import start, step, use_spec
from test_handlers import http, pid


def _waiting(testkit, scope):
    use_spec(testkit, dataclasses.replace(toy.toy_spec(RiskTier.SINGLE), approver=scope))
    run = start()  # the platform tenant
    step(engine.Phase.REQUEST_APPROVAL, run, token="tok-1", execution_arn=f"arn:exec:{pid(run)}")
    return run


def _approve(run, **kw):
    body = {"decision": "APPROVE", "intent_hash": run["proposal"]["intent_hash"]}
    return approval.handler(http("POST", pid(run), body=body, **kw), None)


def _ids(resp):
    return [v["proposal"]["proposal_id"] for v in json.loads(resp["body"])["proposals"]]


def test_an_operator_scoped_proposal_records_and_is_decided_by_the_kits_operator(aws, testkit):
    run = _waiting(testkit, ApproverScope.KIT_OPERATOR)
    stored = ProposalRepo("proposals", client=aws.db).get(pid(run))
    assert (stored.tenant_id, stored.approver_tenant) == ("platform", "k3:operator")
    # the requesting tenant reads its status but cannot approve (403), and stores nothing
    own = approval.handler(http("GET", pid(run)), None)
    assert own["statusCode"] == 200
    assert json.loads(own["body"])["approver_tenant"] == "k3:operator"
    assert _approve(run)["statusCode"] == 403
    assert DecisionRepo("decisions", client=aws.db).list(pid(run)) == []
    assert aws.sfn.sent == []
    # a third tenant still sees nothing
    assert _approve(run, tenant="tenant-a")["statusCode"] == 404
    # the operator lists it and approves it
    assert _ids(approval.handler(http("GET", None, tenant="k3:operator"), None)) == [pid(run)]
    assert _approve(run, tenant="k3:operator")["statusCode"] == 200
    assert aws.sfn.sent == [("tok-1", json.dumps({"approval": "APPROVED"}))]


def test_an_own_tenant_proposal_is_unchanged(aws, testkit):
    run = _waiting(testkit, ApproverScope.OWN_TENANT)
    stored = ProposalRepo("proposals", client=aws.db).get(pid(run))
    assert stored.approver_tenant == "platform"
    assert _approve(run, tenant="k3:operator")["statusCode"] == 404
    assert _approve(run)["statusCode"] == 200


def test_the_requester_still_cannot_approve_an_operator_scoped_proposal(aws, testkit):
    run = _waiting(testkit, ApproverScope.KIT_OPERATOR)
    assert _approve(run, tenant="k3:operator", sub="requester-sub")["statusCode"] == 409


def test_a_row_without_an_approver_tenant_decides_in_its_own_tenant(aws, testkit):
    run = _waiting(testkit, ApproverScope.OWN_TENANT)
    aws.db.update_item(
        TableName="proposals",
        Key={"proposal_id": {"S": pid(run)}},
        UpdateExpression="REMOVE approver_tenant",
    )
    assert ProposalRepo("proposals", client=aws.db).get(pid(run)).approver_tenant == "platform"
    assert _approve(run)["statusCode"] == 200


# ---- the kits' declarations (closed set, fail closed) -------------------------------------

_KITS = {"k5": "awx_k5_platform_spend", "k6": "awx_k6_payroll", "k7": "awx_k7_lending",
         "k8": "awx_k8_settlement"}  # fmt: skip
OPERATOR_ACTIONS = {
    "k5": {"bridge_shortfall"},
    "k6": set(),  # run_payroll already runs as k6:operator; employer payrolls are children
    "k7": {"disburse_advance"},
    "k8": {"settle_seller_cycle", "recalculate_seller_reserve", "resolve_in_flight_money"},
}


@pytest.mark.parametrize("kit", sorted(CONNECTED_ACCOUNT_KITS))
def test_every_kit_5_to_8_root_spec_declares_its_approver(kit):
    specs = importlib.import_module(_KITS[kit]).SPECS
    assert all(s.approver is not None for s in specs.values()), kit
    operator = {a for a, s in specs.items() if s.approver is ApproverScope.KIT_OPERATOR}
    assert operator == OPERATOR_ACTIONS[kit]


def test_k7_disburse_goes_to_the_operator_and_collect_stays_with_the_borrower():
    specs = importlib.import_module("awx_k7_lending").SPECS
    borrower = "k7:borrower:loan-a"
    assert approver_tenant(specs["disburse_advance"], borrower) == "k7:operator"
    assert approver_tenant(specs["collect_weekly_repayment"], borrower) == borrower


def test_k8_a_seller_payout_goes_to_the_operator():
    specs = importlib.import_module("awx_k8_settlement").SPECS
    assert approver_tenant(specs["settle_seller_cycle"], "cara") == "k8:operator"
    assert approver_tenant(specs["recover_shortfall"], "cara") == "cara"


def test_an_undeclared_kit_5_to_8_root_fails_closed_and_a_child_does_not():
    spec = dataclasses.replace(
        importlib.import_module("awx_k7_lending").SPECS["disburse_advance"], approver=None
    )
    with pytest.raises(ValueError, match="declares no approver scope"):
        approver_tenant(spec, "k7:borrower:loan-a")
    assert approver_tenant(spec, "k7:borrower:loan-a", is_child=True) == "k7:borrower:loan-a"
    platform = dataclasses.replace(toy.toy_spec(), approver=None)  # kits 1-4: own tenant
    assert approver_tenant(platform, "platform") == "platform"


# ---- kit 5-8 rows without an approver fail closed; START stores nothing undeclared --------


def test_a_legacy_kit_5_to_8_row_cannot_be_decided_but_can_be_read(aws, testkit):
    """A k7 disburse row with no approver_tenant is undecidable, not decided in its own
    tenant."""
    run = _waiting(testkit, ApproverScope.OWN_TENANT)
    item = aws.db.get_item(TableName="proposals", Key={"proposal_id": {"S": pid(run)}})["Item"]
    doc = json.loads(item["doc"]["S"])
    doc["proposal"]["kit"], doc["proposal"]["action"] = "k7", "disburse_advance"
    item["doc"] = {"S": json.dumps(doc)}
    item["kit"], item["action"] = {"S": "k7"}, {"S": "disburse_advance"}
    item.pop("approver_tenant")  # a row with no approver_tenant attribute
    aws.db.put_item(TableName="proposals", Item=item)
    stored = ProposalRepo("proposals", client=aws.db).get(pid(run))
    assert stored.proposal.kit == "k7"
    assert stored.approver_tenant is None
    resp = _approve(run)  # the borrower's own approver
    assert resp["statusCode"] == 409
    assert "re-propose" in json.loads(resp["body"])["error"]
    assert DecisionRepo("decisions", client=aws.db).list(pid(run)) == []
    assert aws.sfn.sent == []
    assert _approve(run, tenant="k7:operator")["statusCode"] == 404  # never its approver either
    assert approval.handler(http("GET", pid(run)), None)["statusCode"] == 200  # own tenant reads


def test_put_requires_an_approver_tenant(aws, testkit):
    run = _waiting(testkit, ApproverScope.OWN_TENANT)
    repo = ProposalRepo("proposals", client=aws.db)
    with pytest.raises(TypeError):
        repo.put(engine.from_payload(decode(run)))  # keyword is required
    with pytest.raises(ValueError, match="required"):
        repo.put(engine.from_payload(decode(run)), approver_tenant="")


def test_an_undeclared_kit_5_to_8_root_stores_no_proposal_at_start(aws, testkit, monkeypatch):
    import awx_common.tenancy as tenancy

    monkeypatch.setattr(
        tenancy, "CONNECTED_ACCOUNT_KITS", frozenset({"k3", "k5", "k6", "k7", "k8"})
    )
    use_spec(testkit, dataclasses.replace(toy.toy_spec(RiskTier.SINGLE), approver=None))
    with pytest.raises(ValueError, match="declares no approver scope"):
        start("tenant-a")
    assert aws.db.scan(TableName="proposals")["Items"] == []


def test_a_redelivered_start_with_another_approver_tenant_raises(aws, testkit):
    run = _waiting(testkit, ApproverScope.OWN_TENANT)
    repo = ProposalRepo("proposals", client=aws.db)
    again = engine.from_payload(decode(run))
    assert repo.put(again, approver_tenant="platform").proposal_id == pid(run)  # same: returns it
    with pytest.raises(ValueError, match="already exists"):
        repo.put(again, approver_tenant="k3:operator")
