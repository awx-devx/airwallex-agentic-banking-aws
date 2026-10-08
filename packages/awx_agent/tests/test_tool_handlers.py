from datetime import timedelta
from types import SimpleNamespace

import pytest
from awx_agent.handlers import tools as tools_mod
from awx_agent_stubs import (
    NOW,
    READER_FN,
    SM_ARN,
    StubLambda,
    caller,
    dispatcher,
    event,
)
from awx_common import transport
from awx_common.approvals import Origin, RiskTier, derive_proposal_id
from awx_common.store import InMemoryKitStore
from jwt_helpers import bad_signature_token

PAY = {"amount": "250.00", "currency": "AUD", "beneficiary_name": "Acme Pty"}
SMALL = {**PAY, "amount": "20.00"}


def _payloads(lam):
    return [c["payload"] for c in lam.calls]


# --- reads -------------------------------------------------------------------------------------
def test_read_invokes_the_kits_own_reader_as_platform_for_kits_1_to_4():
    d, lam, sfn = dispatcher()
    out = d.dispatch("k1___read_balances", event({}, caller(tenant_id="tenant-a")))
    assert out["untrusted_data"]["items"][0]["available"] == "500.00"
    (call,) = lam.calls
    assert call["FunctionName"] == READER_FN
    assert call["payload"]["kit"] == "k1"
    assert set(call["payload"]) == {"kit", "read", "tenant_id"}  # no mode: the reader derives it
    assert call["payload"]["tenant_id"] == "platform"  # a k1-4 caller's tenant claim is ignored
    assert call["payload"]["read"]["on_behalf_of"] is None
    assert sfn.calls == []


def test_read_tenant_comes_from_claim_for_connected_account_kits():
    d, lam, _ = dispatcher(kit="k5")
    d.dispatch("read_balances", event({}, caller(tenant_id="tenant-a")))
    (p,) = _payloads(lam)
    assert p["tenant_id"] == "tenant-a"
    assert p["read"]["on_behalf_of"] == "acct_A"  # bound server-side from the resolved tenant


def test_connected_account_kit_without_tenant_claim_is_refused():
    d, lam, _ = dispatcher(kit="k5")
    out = d.dispatch("read_balances", event({}))
    assert out["error"]["code"] == "NOT_AUTHORISED"
    assert lam.calls == []


def test_unknown_tenant_claim_is_refused():
    d, lam, _ = dispatcher(kit="k5")
    out = d.dispatch("read_balances", event({}, caller(tenant_id="tenant-zzz")))
    assert out["error"]["code"] == "TENANT_SCOPE"
    assert lam.calls == []


def test_model_cannot_choose_tenant():
    d, lam, _ = dispatcher(kit="k5")
    out = d.dispatch(
        "read_balances", event({"tenant_id": "tenant-b"}, caller(tenant_id="tenant-a"))
    )
    assert out["error"]["code"] == "INVALID_INPUT"
    assert lam.calls == []


def test_reader_error_type_is_mapped():
    d, _, sfn = dispatcher(lam=StubLambda(fail="TenantScopeViolation"))
    assert d.dispatch("read_balances", event({}))["error"]["code"] == "TENANT_SCOPE"
    d, _, _ = dispatcher(lam=StubLambda(fail="SomethingOdd"))
    assert d.dispatch("read_balances", event({}))["error"]["code"] == "READ_FAILED"
    assert sfn.calls == []


# --- identity ----------------------------------------------------------------------------------
def test_missing_or_forged_token_is_refused():
    d, lam, sfn = dispatcher()
    assert d.dispatch("propose_pay_supplier", dict(PAY))["error"]["code"] == "NOT_AUTHORISED"
    forged = event(PAY, bad_signature_token(**{"cognito:groups": ["requester", "approver"]}))
    assert d.dispatch("propose_pay_supplier", forged)["error"]["code"] == "NOT_AUTHORISED"
    assert (
        d.dispatch("propose_pay_supplier", event(PAY, "not.a.jwt"))["error"]["code"]
        == "NOT_AUTHORISED"
    )
    assert lam.calls == []
    assert sfn.calls == []


def test_approver_only_cannot_propose():
    d, _, sfn = dispatcher()
    out = d.dispatch("propose_pay_supplier", event(PAY, caller(**{"cognito:groups": ["approver"]})))
    assert out["error"]["code"] == "NOT_AUTHORISED"
    assert sfn.calls == []


# --- propose -----------------------------------------------------------------------------------
def test_propose_starts_workflow_with_engine_execution_input():
    d, lam, sfn = dispatcher()
    out = d.dispatch("k1___propose_pay_supplier", event(PAY))
    assert out["risk_tier"] == RiskTier.SINGLE.value
    assert out["origin"] == "AGENT"
    assert out["status"] == "AWAITING_APPROVAL"
    (start,) = sfn.calls
    assert start["stateMachineArn"] == SM_ARN
    assert start["name"] == out["proposal_id"]
    body = transport.loads(start["input"])
    assert set(body) == {"proposal", "raw_input", "tenant_id", "decision", "origin"}
    assert body["origin"] == "AGENT"
    assert body["tenant_id"] == "platform"
    assert body["raw_input"] == PAY
    p = tools_mod.proposal_from(body["proposal"])  # hash verifies
    assert p.intent_hash == out["intent_hash"]
    assert p.origin is Origin.AGENT
    assert p.proposed_by == "user-requester"
    assert body["decision"]["risk_tier"] == p.risk_tier.value  # engine.start's consistency check
    assert body["decision"]["computed"] == p.computed
    assert out["summary"]["computed_by_code"]["fee"] == {"amount": "1.50", "currency": "AUD"}
    assert all(set(c["payload"]) == {"kit", "read", "tenant_id"} for c in lam.calls)


def test_preview_gets_the_runner_policy_context_store_platform_and_tenant():
    """The policy sees ctx.store, ctx.read_platform and the resolved connected account."""
    stores = {}
    d, lam, sfn = dispatcher(kit="k5", stores=stores)
    seeded = stores.setdefault(("k5", "tenant-a"), InMemoryKitStore())
    seeded.put("limits", {"x": "1"}, expected_version=None)
    out = d.dispatch("propose_pay_supplier", event(PAY, caller(tenant_id="tenant-a")))
    computed = out["summary"]["computed_by_code"]
    assert computed["tenant"] == "tenant-a"
    assert computed["connected_account"] == "acct_A"
    assert computed["platform_account"] == "acct_platform"
    assert computed["limit_version"] == 1
    assert transport.loads(sfn.calls[0]["input"])["tenant_id"] == "tenant-a"
    # Both reads carry the run's tenant; the tenant read is bound to its account, the platform
    # read is headerless (the reader derives platform scope from the missing header).
    scopes = {(c["payload"]["tenant_id"], c["payload"]["read"]["on_behalf_of"]) for c in lam.calls}
    assert scopes == {("tenant-a", "acct_A"), ("tenant-a", None)}


def test_preview_cannot_write_the_store():
    d, _, sfn = dispatcher()
    out = d.dispatch("propose_probe_write", event(PAY))
    assert out["error"]["code"] == "INTERNAL"
    assert sfn.calls == []


def test_agent_floor_raises_auto_to_single():
    d, _, _ = dispatcher()
    out = d.dispatch("propose_pay_supplier", event(SMALL))
    assert out["risk_tier"] == RiskTier.SINGLE.value
    assert out["status"] == "AWAITING_APPROVAL"
    assert out["origin"] == "AGENT"
    assert "fixture rule" in out["reasons"]


def test_spec_agent_min_tier_auto_keeps_auto(monkeypatch):
    from dataclasses import replace

    d, _, _ = dispatcher()
    d.specs = {
        **d.specs,
        "pay_supplier": replace(d.specs["pay_supplier"], agent_min_tier=RiskTier.AUTO),
    }
    out = d.dispatch("propose_pay_supplier", event(SMALL))
    assert out["risk_tier"] == "AUTO"
    assert out["status"] == "SUBMITTED_NO_APPROVAL_NEEDED"


def test_proposal_id_is_derived_and_a_retry_returns_the_existing_proposal():
    """The same call twice starts one execution and returns one proposal."""
    ticks = iter([NOW, NOW + timedelta(seconds=40), NOW + timedelta(seconds=80)])
    d, _, sfn = dispatcher(clock=lambda: next(ticks))
    first = d.dispatch("propose_pay_supplier", event(PAY))
    second = d.dispatch("propose_pay_supplier", event(PAY))
    assert second["proposal_id"] == first["proposal_id"]
    assert second["intent_hash"] == first["intent_hash"]
    assert second["existing_proposal"] is True
    assert first["existing_proposal"] is False
    assert len(sfn.executions) == 1
    intent = tools_mod.proposal_from(transport.loads(sfn.calls[0]["input"])["proposal"]).intent
    assert first["proposal_id"] == derive_proposal_id(
        kit="k1",
        action="pay_supplier",
        intent=intent,
        tenant_id="platform",
        proposed_by="user-requester",
        now=NOW,
        ttl=timedelta(hours=1),
        computed=tools_mod.proposal_from(
            transport.loads(sfn.calls[0]["input"])["proposal"]
        ).computed,
    )


def test_a_re_proposal_whose_computed_differs_gets_a_new_id_and_execution():
    """The same intent with different code-computed amounts is a new proposal."""
    stores = {}
    d, _, sfn = dispatcher(stores=stores)
    first = d.dispatch("propose_pay_supplier", event(PAY))
    from awx_common.store import InMemoryKitStore

    stores.setdefault(("k1", "platform"), InMemoryKitStore()).put(
        "limits", {"x": "2"}, expected_version=None
    )  # policy's computed now carries limit_version 1 instead of 0
    second = d.dispatch("propose_pay_supplier", event(PAY))
    assert second["proposal_id"] != first["proposal_id"]
    assert second["existing_proposal"] is False
    assert len(sfn.executions) == 2


def test_another_user_gets_a_different_proposal():
    d, _, sfn = dispatcher()
    a = d.dispatch("propose_pay_supplier", event(PAY))
    b = d.dispatch("propose_pay_supplier", event(PAY, caller(sub="someone-else")))
    assert a["proposal_id"] != b["proposal_id"]
    assert len(sfn.executions) == 2


def test_propose_validation_failure_starts_nothing():
    d, _, sfn = dispatcher()
    out = d.dispatch("propose_pay_supplier", event({**PAY, "currency": "EUR"}))
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "currency must be AUD or USD" in out["error"]["message"]  # validate(), not the schema
    assert sfn.calls == []


def test_bad_amount_is_a_kit_validation_error():
    d, _, sfn = dispatcher()
    out = d.dispatch("propose_pay_supplier", event({**PAY, "amount": "not-a-number"}))
    assert out["error"]["code"] == "INVALID_INPUT"
    assert sfn.calls == []


@pytest.mark.parametrize("exc", [ValueError, TypeError, KeyError, ArithmeticError])
def test_non_validation_error_is_generic_and_never_echoes_its_text(exc):
    """Only validate()'s own error text reaches the model."""
    from dataclasses import replace

    d, _, sfn = dispatcher()

    def bad_policy(intent, ctx):
        raise exc("SECRET internal detail arn:aws:secretsmanager:...")

    d.specs = {**d.specs, "pay_supplier": replace(d.specs["pay_supplier"], policy=bad_policy)}
    out = d.dispatch("propose_pay_supplier", event(PAY))
    assert out["error"] == {"code": "INTERNAL", "message": "the tool failed; nothing was proposed"}
    assert "SECRET" not in str(out)
    assert sfn.calls == []


@pytest.mark.parametrize(
    ("execution_status", "outcome", "state"),
    [
        ("SUCCEEDED", "REJECTED", "REJECTED"),
        ("SUCCEEDED", "EXPIRED", "EXPIRED"),
        ("SUCCEEDED", "SUCCEEDED", "SUCCEEDED"),
        ("FAILED", None, "EXECUTION_FAILED"),
        ("RUNNING", None, "IN_PROGRESS"),
    ],
)
def test_retry_reports_the_existing_executions_real_state(execution_status, outcome, state):
    """After rejection, expiry or completion a retry never says AWAITING_APPROVAL."""
    outcomes = {}
    ticks = iter([NOW, NOW + timedelta(seconds=40)])
    d, _, sfn = dispatcher(clock=lambda: next(ticks), outcomes=outcomes)
    first = d.dispatch("propose_pay_supplier", event(PAY))
    (arn,) = sfn.executions
    sfn.status[arn] = execution_status
    if outcome:
        outcomes[first["proposal_id"]] = outcome
    retry = d.dispatch("propose_pay_supplier", event(PAY))
    assert retry["proposal_id"] == first["proposal_id"]
    assert retry["existing_proposal"] is True
    assert retry["execution_status"] == execution_status
    assert retry["proposal_state"] == state
    assert retry["status"] == f"EXISTING_PROPOSAL_{state}"
    assert retry["status"] != "AWAITING_APPROVAL"
    assert len(sfn.executions) == 1


def test_unknown_tool():
    d, _, _ = dispatcher()
    assert d.dispatch("k1___transfers_create", event({}))["error"]["code"] == "UNKNOWN_TOOL"


def test_lambda_handler_reads_tool_name_from_gateway_context(monkeypatch):
    d, _, _ = dispatcher()
    monkeypatch.setattr(tools_mod, "_DISPATCHER", d)
    ctx = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "k1___read_balances"})
    )
    assert "untrusted_data" in tools_mod.handler(event({}), ctx)
    no_ctx = tools_mod.handler(event({}), SimpleNamespace(client_context=None))
    assert no_ctx["error"]["code"] == "NOT_AUTHORISED"


def test_execution_arn():
    assert tools_mod.execution_arn(SM_ARN, "p1") == (
        "arn:aws:states:ap-southeast-2:111111111111:execution:k1-pay_supplier:p1"
    )
    with pytest.raises(ValueError, match="state machine"):
        tools_mod.execution_arn("arn:aws:lambda:x", "p1")
