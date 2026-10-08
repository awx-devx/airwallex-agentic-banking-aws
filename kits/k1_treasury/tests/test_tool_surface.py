"""Kit 1's tool surface through the real ToolDispatcher (awx_agent.testing.KitToolHarness).

For every tool in TOOLS: the approval binds to the proposal hash; tenant isolation holds (kit 1
runs on the platform's own account, whatever tenant a caller claims); model input never chooses
an amount, an account or a request_id; and a re-proposal after the execution closed reports the
existing proposal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_common import ApprovalDecision, Decision, transport
from awx_common.approvals import ApprovalBindingError
from awx_common.tenancy import TenantContext
from awx_k1_treasury import SPECS, TOOLS
from awx_k1_treasury.locks import LockState, read_lock, read_ratelock
from awx_spine.fake import EPOCH, Fault
from awx_spine_aws import engine

SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
CONTRADICTION = {
    "receipt_id": "rcpt-acme",
    "source": "email from Acme AP, 18 Oct 2026",
    "quote": "we will not be able to pay invoice INV-2207 until next month",
}

READS = sorted(t.name for t in TOOLS if t.name.startswith("read_"))
PROPOSES = sorted(t.name for t in TOOLS if t.name.startswith("propose_"))


def wall_env(make_env_fn):
    """A kit 1 env whose fake clock is the wall clock: tokens are signed on the wall clock."""
    env = make_env_fn()
    env.advance((datetime.now(UTC) - EPOCH).total_seconds())
    return env


def harness(env, **kw: Any) -> KitToolHarness:
    h = KitToolHarness.for_kit("k1", port=env.fake, clock=env.now, **kw)
    h.stores[("k1", "platform")] = env.store  # the platform tenant's kit store is the scenario's
    return h


ARGS: dict[str, dict[str, Any]] = {
    "read_balances": {},
    "read_obligations": {},
    "read_fx_rate": {"buy_currency": "EUR"},
    "read_treasury_plan": {},
    "propose_lock_rate": dict(SUPPLIER),
    "propose_execute_plan": {**SUPPLIER, "contradictions": [CONTRADICTION]},  # SINGLE
    "propose_release_obligation": {
        "obligation_id": "ob-supplier-eur",
        "reason": "transfer refused at create; EUR is in the wallet",
    },
}


def scenario(tool: str, make_env_fn):
    """A state in which `tool` has something to do."""
    env = wall_env(make_env_fn)
    if tool == "propose_execute_plan":
        env.lock(SUPPLIER)
    if tool == "propose_release_obligation":
        env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
        env.pay(SUPPLIER)
        assert read_lock(env.store, "ob-supplier-eur").state is LockState.NEEDS_PERSON
    return env


def test_every_tool_is_covered(make_env):
    assert set(READS) | set(PROPOSES) == {t.name for t in TOOLS} == set(ARGS)
    assert {p.removeprefix("propose_") for p in PROPOSES} == set(SPECS)


@pytest.mark.parametrize("tool", READS)
def test_read_tools_answer_through_the_reader_and_send_nothing(tool, make_env):
    env = scenario(tool, make_env)
    h = harness(env)
    out = h.call(tool, ARGS[tool])
    assert "error" not in out, out
    assert h.sfn.calls == []
    assert env.port.executed == []


# ---- approval bound to the proposal hash ---------------------------------------------------


@pytest.mark.parametrize("tool", PROPOSES)
def test_approval_binds_to_the_proposal_hash(tool, make_env):
    env = scenario(tool, make_env)
    h = harness(env)
    out = h.call(tool, ARGS[tool])
    assert "error" not in out, out
    (execution,) = h.executions
    spec = SPECS[tool.removeprefix("propose_")]
    run = engine.start(spec, execution, tenant=TenantContext("platform"))
    p = run.proposal
    assert p.intent_hash == out["intent_hash"]
    p.verify_hash()
    if tool == "propose_lock_rate":  # AUTO by design (DESIGN.md "AUTO for agent proposals")
        assert p.risk_tier.value == "AUTO"
        return
    assert p.risk_tier.value == "SINGLE_APPROVER", p.reasons
    now = env.now()
    good = ApprovalDecision(p.proposal_id, p.intent_hash, "approver", Decision.APPROVE, now)
    assert engine.binding(run, [good], now=now).approval == "APPROVED"
    wrong = replace(good, intent_hash="0" * 64)
    assert engine.binding(run, [wrong], now=now).state == "REJECTED"
    own = replace(good, approver=p.proposed_by)
    assert engine.binding(run, [own], now=now).state == "REJECTED"
    tampered = dict(execution)
    tampered["proposal"] = replace(p, computed={**p.computed, "_tampered": True})
    with pytest.raises(ApprovalBindingError):
        engine.start(spec, tampered, tenant=TenantContext("platform"))


def test_execute_plan_binds_the_locked_quote_and_exact_sell(make_env):
    env = scenario("propose_execute_plan", make_env)
    h = harness(env)
    h.call("propose_execute_plan", ARGS["propose_execute_plan"])
    (execution,) = h.executions
    (conv,) = execution["proposal"]["computed"]["conversions"]
    lock = read_ratelock(env.store, "EUR")
    assert conv["quote_id"] == lock["quote_id"]
    assert conv["sell"] == lock["sell"]


# ---- tenant isolation ----------------------------------------------------------------------


@pytest.mark.parametrize("tool", READS + PROPOSES)
def test_a_tenant_claim_never_moves_kit1_off_the_platform_account(tool, make_env):
    env = scenario(tool, make_env)
    h = harness(env, tenants={"tenant-x": "acct_X"})
    out = h.call(tool, ARGS[tool], tenant_id="tenant-x")
    assert "error" not in out, out
    if tool != "read_obligations":  # read_obligations reads only the kit store
        assert h.reader.calls
    assert all(c["payload"]["tenant_id"] == "platform" for c in h.reader.calls)
    assert all(c["FunctionName"] != ADAPTER_FN for c in h.reader.calls)
    reads = [transport.loads(transport.dumps(c["payload"]["read"])) for c in h.reader.calls]
    assert all(r.get("on_behalf_of") is None for r in reads)
    for execution in h.executions:
        assert execution["tenant_id"] == "platform"


def test_a_connected_accounts_wallet_is_never_read_by_kit1(make_env):
    env = scenario("read_balances", make_env)
    acct = env.fake.add_connected_account("poc-i1-other", {})
    env.fake.credit("EUR", "999999", account=acct)
    h = harness(env, tenants={"tenant-x": acct})
    out = h.call("read_balances", {}, tenant_id="tenant-x")
    eur = {b["currency"]: b for b in out["untrusted_data"]["balances"]}["EUR"]
    assert eur["available"]["amount"] == "2000.00"  # the platform's, not the tenant's


# ---- model input never chooses an amount, an account or a request_id -----------------------


@pytest.mark.parametrize("tool", READS + PROPOSES)
@pytest.mark.parametrize(
    "field",
    [
        "on_behalf_of",
        "tenant_id",
        "account_id",
        "request_id",
        "amount",
        "beneficiary_id",
        "sell_amount",
        "quote_id",
        "attempt",
    ],
)
def test_model_cannot_supply_amounts_accounts_or_request_ids(tool, field, make_env):
    env = scenario(tool, make_env)
    h = harness(env)
    out = h.call(tool, {**ARGS[tool], field: "acct_attacker"})
    assert out["error"]["code"] == "INVALID_INPUT", out
    assert h.sfn.calls == []


def test_amounts_beneficiaries_and_request_ids_come_from_the_book_and_the_lock(make_env):
    env = scenario("propose_execute_plan", make_env)
    h = harness(env)
    h.call("propose_execute_plan", ARGS["propose_execute_plan"])
    (execution,) = h.executions
    computed = execution["proposal"]["computed"]
    (payout,) = computed["payouts"]
    ob = env.book.obligation("ob-supplier-eur")
    assert payout["amount"] == ob.amount
    assert payout["beneficiary_id"] == ob.beneficiary_id
    assert "request_id" not in execution["raw_input"]
    assert set(execution["raw_input"]) == {"obligation_ids", "contradictions"}


# ---- re-proposal after the execution closed ------------------------------------------------


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
    tool, status, outcome, expected, make_env
):
    env = scenario(tool, make_env)
    h = harness(env)
    first = h.call(tool, ARGS[tool])
    (arn,) = h.sfn.executions
    h.sfn.status[arn] = status
    if outcome:
        h.outcomes[first["proposal_id"]] = outcome
    again = h.call(tool, ARGS[tool])
    assert again["proposal_id"] == first["proposal_id"]
    assert again["existing_proposal"] is True
    assert again["status"] == expected
    assert len(h.sfn.executions) == 1


# ---- a deployed kit starts with an empty store ----------------------------------------------


@pytest.mark.parametrize("tool", READS + PROPOSES)
def test_an_empty_kit_store_gives_a_stated_answer_never_internal(tool):
    from awx_k1_treasury.book import NO_BOOK
    from awx_spine.fake import FakeAirwallex

    h = KitToolHarness.for_kit("k1", port=FakeAirwallex(), clock=lambda: datetime.now(UTC))
    out = h.call(tool, ARGS[tool])
    assert "error" not in out, out
    if tool in READS:
        assert out["untrusted_data"] == {
            "status": "NOT_CONFIGURED",
            "book": None,
            "reason": NO_BOOK,
        }
    else:
        assert out["risk_tier"] == "BLOCKED", out
        assert NO_BOOK in out["reasons"]
