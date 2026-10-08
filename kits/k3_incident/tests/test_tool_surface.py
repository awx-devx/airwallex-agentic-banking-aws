"""Kit 3's tool surface through the real ToolDispatcher (awx_agent.testing.KitToolHarness):
for every tool in TOOLS, the approval binds to the proposal hash, tenant isolation holds, model
input never chooses an amount, an account or a request_id, and a re-proposal after the
execution closed reports the existing proposal."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_common import ApprovalDecision, Decision, Money, transport
from awx_common.approvals import ApprovalBindingError
from awx_common.tenancy import TenantContext
from awx_k3_incident import SPECS, TOOLS
from awx_k3_incident import airwallex as awx
from awx_spine_aws import engine
from k3_fixtures import BANK_RETURN, Kit3

# Pinned once: a dropped or an added tool fails here and in synth.
EXPECTED_TOOLS = frozenset(
    {
        "read_transfer",
        "read_incident_assessment",
        "propose_replace_failed_transfer",
        "propose_close_incident",
        "propose_simulate_transfer_failure",
    }
)
READS = sorted(t.name for t in TOOLS if t.name.startswith("read_"))
PROPOSES = sorted(t.name for t in TOOLS if t.name.startswith("propose_"))


def fixed_now() -> datetime:
    """A per-harness frozen clock (so a retry's execution input is identical), set a moment
    ahead of the wall clock the TokenIssuer signs iat with, so no token is from the future."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=2)


def harness(kit: Kit3, **kw: Any) -> KitToolHarness:
    frozen = fixed_now()
    h = KitToolHarness.for_kit("k3", port=kit.fake, clock=lambda: frozen, **kw)
    h.frozen = frozen  # type: ignore[attr-defined]
    # The tool plane's kit store is the platform tenant's: share the scenario's state.
    h.stores[("k3", "platform")] = kit.store
    return h


def args_for(tool: str, kit: Kit3, oid: str) -> dict[str, Any]:
    return {
        "read_transfer": {"transfer_id": oid},
        "read_incident_assessment": {"transfer_id": oid},
        "propose_replace_failed_transfer": {"original_transfer_id": oid, "incident_ref": "INC-1"},
        "propose_close_incident": {"original_transfer_id": oid},
        "propose_simulate_transfer_failure": {"transfer_id": oid, "failure_type": BANK_RETURN},
    }[tool]


def scenario(tool: str) -> tuple[Kit3, str]:
    """A state in which `tool` has something to do."""
    kit = Kit3.new()
    if tool == "propose_simulate_transfer_failure":
        return kit, kit.staged_original()
    oid = kit.failed_original()
    if tool == "propose_close_incident":
        assert kit.replace(oid).state == "SUCCEEDED"
    return kit, oid


def test_every_tool_is_covered():
    assert {t.name for t in TOOLS} == EXPECTED_TOOLS
    assert set(READS) | set(PROPOSES) == EXPECTED_TOOLS
    assert {f"propose_{a}" for a in SPECS} == set(PROPOSES)


def test_the_send_tool_is_not_an_agent_tool():
    """An agent that reads supplier messages cannot pay anyone."""
    kit = Kit3.new()
    h = harness(kit)
    assert "propose_send_supplier_transfer" not in h.dispatcher.tools
    assert "send_supplier_transfer" not in SPECS
    out = h.call(
        "propose_send_supplier_transfer",
        {
            "payment_key": "inv-x",
            "beneficiary_id": kit.beneficiary_id,
            "amount": "95000.00",
            "currency": "USD",
            "transfer_method": "LOCAL",
        },
    )
    assert "error" in out, out
    assert h.sfn.calls == []
    assert h.reader.calls == []


# ---- approval bound to the proposal hash ---------------------------------------------------


@pytest.mark.parametrize("tool", PROPOSES)
def test_approval_binds_to_the_proposal_hash(tool):
    kit, oid = scenario(tool)
    h = harness(kit)
    out = h.call(tool, args_for(tool, kit, oid))
    assert out.get("status") == "AWAITING_APPROVAL" or out.get("risk_tier") == "AUTO", out
    (execution,) = h.executions
    spec = SPECS[tool.removeprefix("propose_")]
    run = engine.start(spec, execution, tenant=TenantContext("platform"))
    p = run.proposal
    assert p.intent_hash == out["intent_hash"]
    p.verify_hash()
    if p.risk_tier.value == "AUTO":  # close_incident: no approver (README justification)
        assert tool == "propose_close_incident"
        return
    good = ApprovalDecision(p.proposal_id, p.intent_hash, "approver", Decision.APPROVE, h.frozen)
    assert engine.binding(run, [good], now=h.frozen).approval == "APPROVED"
    wrong = replace(good, intent_hash="0" * 64)
    assert engine.binding(run, [wrong], now=h.frozen).state == "REJECTED"
    own = replace(good, approver=p.proposed_by)
    assert engine.binding(run, [own], now=h.frozen).state == "REJECTED"
    # A tampered computed (another amount) no longer matches its hash.
    tampered = dict(execution)
    tampered["proposal"] = replace(p, computed={**p.computed, "_tampered": True})
    with pytest.raises(ApprovalBindingError):
        engine.start(spec, tampered, tenant=TenantContext("platform"))


# ---- tenant isolation ---------------------------------------------------------------------


@pytest.mark.parametrize("tool", READS + PROPOSES)
def test_a_tenant_claim_never_moves_kit3_off_the_platform_account(tool):
    """Kit 3 runs on the platform's own account (kits 1-4) and serves platform callers only:
    another tenant's claim, with a row or none, is refused before anything is read. A platform
    caller's reads go to the platform reader with no x-on-behalf-of, and every proposal is the
    platform's."""
    kit, oid = scenario(tool)
    h = harness(kit, tenants={"tenant-x": "acct_X"})
    for claim in ("tenant-x", "k5:operator"):
        refused = h.call(tool, args_for(tool, kit, oid), tenant_id=claim)
        assert refused["error"]["code"] == "TENANT_SCOPE", (claim, refused)
    assert h.reader.calls == []
    assert h.executions == []
    out = h.call(tool, args_for(tool, kit, oid))
    assert "error" not in out, out
    assert h.reader.calls
    assert all(c["payload"]["tenant_id"] == "platform" for c in h.reader.calls)
    assert all(c["FunctionName"] != ADAPTER_FN for c in h.reader.calls)
    reads = [transport.loads(transport.dumps(c["payload"]["read"])) for c in h.reader.calls]
    assert all(r.get("on_behalf_of") is None for r in reads)
    for execution in h.executions:
        assert execution["tenant_id"] == "platform"


def test_a_connected_accounts_transfer_is_invisible_to_kit3():
    """A transfer that exists only in a connected account cannot be read or acted on through
    kit 3's tools, whatever tenant the caller claims."""
    from dataclasses import replace as dc_replace

    kit = Kit3.new()
    acct = kit.fake.add_connected_account("poc-i3-other", {"USD": Money.of("50", "USD")})
    ben_op = dc_replace(kit_beneficiary_op(kit), on_behalf_of=acct)
    ben = kit.fake.execute(ben_op).provider_id
    op = awx.create_transfer_op(
        business_key="other",
        request_id=awx.derive_request_id("k3", "other", "x"),
        intent_hash="c" * 64,
        beneficiary_id=str(ben),
        amount=Money.of("5.00", "USD"),
        transfer_method="LOCAL",
        reference="poc-i3-other-nosettle",
        metadata={"poc_label": "poc-i3-original"},
    )
    result = kit.fake.execute(dc_replace(op, on_behalf_of=acct))
    if result.provider_id is None:
        pytest.skip(f"the fake refused the connected-account transfer: {result.error_code}")
    tid = str(result.provider_id)
    h = harness(kit, tenants={"tenant-x": acct})
    for tool, args in (
        ("read_transfer", {"transfer_id": tid}),
        ("read_incident_assessment", {"transfer_id": tid}),
        ("propose_simulate_transfer_failure", {"transfer_id": tid, "failure_type": BANK_RETURN}),
    ):
        out = h.call(tool, args, tenant_id="tenant-x")
        assert "error" in out or out.get("risk_tier") == "BLOCKED", (tool, out)
        assert "untrusted_data" not in out
    assert all(e["proposal"]["risk_tier"] == "BLOCKED" for e in h.executions)


def kit_beneficiary_op(kit: Kit3):
    from awx_common import Operation

    rid = awx.derive_request_id("k3", "poc-i3-beneficiary-other", "create")
    return Operation(
        kit="k3",
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body={
            "request_id": rid,
            "nickname": "poc-i3-other",
            "transfer_methods": ["LOCAL"],
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "address": {"country_code": "US", "state": "US-NY"},
                "bank_details": {
                    "bank_account_category": "Checking",
                    "account_routing_type1": "aba",
                },
            },
        },
        request_id=rid,
        business_key="poc-i3-beneficiary-other",
        intent_hash="b" * 64,
    )


@pytest.mark.parametrize("tool", READS + PROPOSES)
@pytest.mark.parametrize("field", ["on_behalf_of", "tenant_id", "account_id", "request_id"])
def test_server_owned_fields_are_refused(tool, field):
    kit, oid = scenario(tool)
    h = harness(kit)
    out = h.call(tool, {**args_for(tool, kit, oid), field: "acct_attacker"})
    assert out["error"]["code"] == "INVALID_INPUT", out
    assert h.sfn.calls == []


# ---- model input never chooses an amount, an account or a request_id ----------------------


@pytest.mark.parametrize("tool", PROPOSES)
@pytest.mark.parametrize("field", ["amount", "transfer_amount", "beneficiary_id", "attempt"])
def test_model_cannot_add_an_amount_beneficiary_or_attempt(tool, field):
    kit, oid = scenario(tool)
    allowed = set(args_for(tool, kit, oid))
    assert field not in allowed
    h = harness(kit)
    out = h.call(tool, {**args_for(tool, kit, oid), field: "999.00"})
    assert out["error"]["code"] == "INVALID_INPUT", out
    assert h.sfn.calls == []


def test_replacement_amount_beneficiary_and_request_id_come_from_the_original():
    kit, oid = scenario("propose_replace_failed_transfer")
    h = harness(kit)
    out = h.call(
        "propose_replace_failed_transfer", args_for("propose_replace_failed_transfer", kit, oid)
    )
    (execution,) = h.executions
    computed = execution["proposal"]["computed"]
    original = kit.transfer(oid)
    assert computed["amount"] == Money.of(Decimal(str(original["transfer_amount"])), "USD")
    assert computed["beneficiary_id"] == original["beneficiary_id"]
    assert computed["replacement_request_id"] == awx.replacement_request_id(oid, 1)
    assert out["summary"]["intent"] == {"original_transfer_id": oid, "incident_ref": "INC-1"}


# ---- re-proposal after the execution closed -----------------------------------------------


@pytest.mark.parametrize("tool", PROPOSES)
@pytest.mark.parametrize(
    ("status", "outcome", "expected"),
    [
        ("SUCCEEDED", None, "EXISTING_PROPOSAL_EXECUTION_SUCCEEDED"),
        ("FAILED", None, "EXISTING_PROPOSAL_EXECUTION_FAILED"),
        ("SUCCEEDED", "REJECTED", "EXISTING_PROPOSAL_REJECTED"),
    ],
)
def test_a_reproposal_after_the_execution_closed_reports_the_existing_one(
    tool, status, outcome, expected
):
    kit, oid = scenario(tool)
    h = harness(kit)
    args = args_for(tool, kit, oid)
    first = h.call(tool, args)
    (arn,) = h.sfn.executions
    h.sfn.status[arn] = status
    if outcome:
        h.outcomes[first["proposal_id"]] = outcome
    again = h.call(tool, args)
    assert again["proposal_id"] == first["proposal_id"]
    assert again["existing_proposal"] is True
    assert again["status"] == expected
    assert len(h.sfn.executions) == 1


def test_a_new_incident_is_a_new_proposal_but_cannot_pay_a_second_time():
    """A different incident_ref is a new proposal (a new execution); kit 3's lock and the
    replacement request_id still allow only one replacement, so policy BLOCKS it."""
    kit, oid = scenario("propose_replace_failed_transfer")
    assert kit.replace(oid).state == "SUCCEEDED"
    h = harness(kit)
    out = h.call(
        "propose_replace_failed_transfer", {"original_transfer_id": oid, "incident_ref": "INC-2"}
    )
    assert out["risk_tier"] == "BLOCKED", out
    assert out["existing_proposal"] is False
    assert len(kit.replacements_of(oid)) == 1
