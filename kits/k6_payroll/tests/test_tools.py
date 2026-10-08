"""Kit 6's tool surface through the real ToolDispatcher (awx_agent.testing.KitToolHarness).

Kit 6 is a connected-account kit, so every caller carries a tenant claim. Its batch, lock,
release and read tools run as the payroll operator tenant `k6:operator` (a tenants-table row
with no connected account); an employer tenant is refused on every tool.
"""

from datetime import UTC, datetime

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_common import Proposal, transport
from awx_common.tenancy import TenantRow
from awx_k6_payroll import TOOLS
from k6_data import PERIOD, batch, employer, us
from k6_fake import PayrollFake

OPERATOR = "k6:operator"


def harness():
    fake = PayrollFake(instance_id="tools")
    accts = {code: fake.employer(code, "20000") for code in "abc"}
    tenants = {
        OPERATOR: TenantRow("k6", None, "operator"),
        "k5:pool": TenantRow("k5", None, "operator"),  # another kit's operator row
        "k7:borrower:x": TenantRow("k7", "acct_k7borrowerx", "customer"),
        **{f"k6:employer:{c}": TenantRow("k6", a, "customer") for c, a in accts.items()},
    }
    fixed = datetime.now(UTC).replace(microsecond=0)  # per test: tokens are signed "now"
    h = KitToolHarness.for_kit("k6", port=fake, tenants=tenants, clock=lambda: fixed)
    return h, accts


def carl():
    return {"pay_period": PERIOD, "employers": [employer("c", us("carl", "800.00"))]}


ARGS = {
    "propose_run_payroll": carl(),
    "propose_lock_rates": batch(),
    "propose_release_run_claim": {"employer": "c", "pay_period": PERIOD, "reason": "crash"},
    "read_payroll_plan": {"pay_period": PERIOD, **employer("c", us("carl", "800.00"))},
    "read_employer_balances": {"employer": "a"},
    "read_payout_status": {"employer": "a", "pay_period": PERIOD, "contractor": "alice"},
}


def test_every_tool_is_covered():
    assert {t.name for t in TOOLS} == set(ARGS)


@pytest.mark.parametrize("tool", sorted(ARGS))
def test_an_employer_tenant_is_refused_on_every_tool(tool):
    h, _ = harness()
    out = h.call(tool, ARGS[tool], tenant_id="k6:employer:a")
    assert out["error"]["code"] == "TENANT_SCOPE", out
    assert h.sfn.calls == []


@pytest.mark.parametrize("tool", sorted(ARGS))
def test_a_caller_without_a_tenant_claim_is_refused(tool):
    h, _ = harness()
    out = h.call(tool, ARGS[tool])
    assert out["error"]["code"] == "NOT_AUTHORISED"
    assert h.sfn.calls == []
    assert h.reader.calls == []


@pytest.mark.parametrize("tool", [t for t in sorted(ARGS) if t.startswith("propose_")])
def test_model_supplied_accounts_amounts_and_ids_are_refused(tool):
    h, _ = harness()
    for extra in (
        {"request_id": "r-1"},
        {"account_id": "acct_x"},
        {"connected_account_id": "acct_x"},
        {"intent_hash": "0" * 64},
        {"pricing": {}},
    ):
        out = h.call(tool, {**ARGS[tool], **extra}, tenant_id=OPERATOR)
        assert out["error"]["code"] == "INVALID_INPUT", (tool, extra, out)
    assert h.sfn.calls == []


def test_the_payroll_proposal_binds_its_approval_and_amounts_come_from_code():
    h, _ = harness()
    out = h.call("propose_run_payroll", carl(), tenant_id=OPERATOR)
    assert out["status"] == "AWAITING_APPROVAL", out
    assert out["origin"] == "AGENT"
    (execution,) = h.executions
    from awx_agent.handlers.tools import proposal_from

    p = proposal_from(transport.loads(h.sfn.calls[0]["input"])["proposal"])  # verifies hash
    assert isinstance(p, Proposal)
    assert p.intent_hash == out["intent_hash"]
    child = p.computed["children"][0]
    assert str(child["pricing"]["platform_fee"]) == "12.00 USD"  # 10 + 2 per contractor
    assert execution["tenant_id"] == OPERATOR
    assert {c["FunctionName"] for c in h.reader.calls} <= {"fake-reader"}
    assert ADAPTER_FN not in {c["FunctionName"] for c in h.reader.calls}


def test_the_rate_lock_is_the_only_tool_that_needs_no_approval():
    h, _ = harness()
    out = h.call("propose_lock_rates", batch(), tenant_id=OPERATOR)
    assert out["status"] == "SUBMITTED_NO_APPROVAL_NEEDED", out
    assert out["risk_tier"] == "AUTO"


def test_the_claim_release_with_no_claim_is_blocked_by_policy():
    h, _ = harness()
    out = h.call("propose_release_run_claim", ARGS["propose_release_run_claim"], tenant_id=OPERATOR)
    assert out["status"] == "BLOCKED_BY_POLICY", out
    assert any("no run claim" in r for r in out["reasons"])


@pytest.mark.parametrize(
    ("status", "outcome", "expected"),
    [
        ("SUCCEEDED", None, "EXISTING_PROPOSAL_EXECUTION_SUCCEEDED"),
        ("FAILED", None, "EXISTING_PROPOSAL_EXECUTION_FAILED"),
        ("SUCCEEDED", "REJECTED", "EXISTING_PROPOSAL_REJECTED"),
    ],
)
def test_a_re_proposal_after_the_execution_closed_reports_the_existing_proposal(
    status, outcome, expected
):
    h, _ = harness()
    first = h.call("propose_run_payroll", carl(), tenant_id=OPERATOR)
    (arn,) = h.sfn.executions
    h.sfn.status[arn] = status
    if outcome:
        h.outcomes[first["proposal_id"]] = outcome
    again = h.call("propose_run_payroll", carl(), tenant_id=OPERATOR)
    assert again["proposal_id"] == first["proposal_id"]
    assert again["status"] == expected
    assert len(h.sfn.executions) == 1


def test_read_tools_read_only_the_named_employer_through_the_kit_reader():
    h, _ = harness()
    out = h.call("read_employer_balances", {"employer": "a"}, tenant_id=OPERATOR)
    assert out["untrusted_data"]["employer"] == "a", out
    tenants = {c["payload"].get("tenant_id") for c in h.reader.calls}
    assert tenants == {"k6:employer:a"}
    plan = h.call("read_payroll_plan", ARGS["read_payroll_plan"], tenant_id=OPERATOR)
    assert plan["untrusted_data"]["fundable"] is True, plan
    status = h.call("read_payout_status", ARGS["read_payout_status"], tenant_id=OPERATOR)
    assert status["untrusted_data"]["status"] == "NOT_SUBMITTED"
    unknown = h.call("read_employer_balances", {"employer": "zed"}, tenant_id=OPERATOR)
    assert unknown["error"]["code"] == "TENANT_SCOPE"
    assert h.sfn.calls == []


@pytest.mark.parametrize("tool", sorted(ARGS))
def test_another_kits_operator_row_is_refused(tool):
    h, _ = harness()
    out = h.call(tool, ARGS[tool], tenant_id="k5:pool")
    assert out["error"]["code"] == "TENANT_SCOPE", out
    assert h.sfn.calls == []


def test_a_release_at_propose_defers_the_root_status_to_recheck():
    """The tool plane has no run_status (a None callable means decide at recheck), so a
    held claim with a named root is proposed for approval; recheck reads the root."""
    from awx_k6_payroll.books import ensure_book, take_claim
    from awx_k6_payroll.workflow import keys_for

    h, _ = harness()
    store = h.store()  # the kit's platform store
    ensure_book(store)
    k = keys_for(store, {"employer": "c", "pay_period": PERIOD})
    take_claim(store, k, "dead-child", _now(), root_proposal_id="root-1", root_action="run_payroll")
    out = h.call("propose_release_run_claim", ARGS["propose_release_run_claim"], tenant_id=OPERATOR)
    assert out["status"] == "AWAITING_APPROVAL", out
    assert out["summary"]["computed_by_code"]["root_proposal_id"] == "root-1"


def _now():
    return datetime.now(UTC)


def test_read_payroll_plan_after_a_paid_payroll_through_the_tool_plane():
    """The deployed read path as k6:operator (read_platform present
    for a no-account tenant) returns a plan after a paid payroll, the fee reported unchecked."""
    from awx_k6_payroll import PAYROLL_SPEC
    from k6_fake import Env, children, run

    fake = PayrollFake(instance_id="tools-paid")
    acct = fake.employer("c", "5000")
    env = Env(fake, {"c": acct})
    assert children(run(PAYROLL_SPEC, carl(), env))["c"].state == "SUCCEEDED"
    tenants = {
        OPERATOR: TenantRow("k6", None, "operator"),
        "k6:employer:c": TenantRow("k6", acct, "customer"),
    }
    h = KitToolHarness.for_kit("k6", port=fake, tenants=tenants)
    h.stores[("k6", "platform")] = env.platform_store  # the same kit state as the run
    out = h.call("read_payroll_plan", ARGS["read_payroll_plan"], tenant_id=OPERATOR)
    plan = out["untrusted_data"]
    assert plan["fee"] == "charge found; source not checked by this read", out
    assert plan["already_paid"] == ["carl"]


@pytest.mark.parametrize("tool", sorted(ARGS))
def test_before_the_operator_row_is_seeded_every_tool_says_not_configured(tool):
    """Kit 6 needs no operator command (its state is minted at first run and its
    accounts come from tenants rows); before the k6:operator row exists every tool answers
    TENANT_SCOPE "unknown tenant", never INTERNAL, and nothing is read or started."""
    fake = PayrollFake(instance_id="unseeded")
    h = KitToolHarness.for_kit("k6", port=fake, tenants={})
    out = h.call(tool, ARGS[tool], tenant_id=OPERATOR)
    assert out["error"]["code"] == "TENANT_SCOPE", out
    assert "unknown tenant" in out["error"]["message"]
    assert h.sfn.calls == []
    assert h.reader.calls == []


@pytest.mark.parametrize("tool", sorted(ARGS))
def test_another_kits_customer_row_is_refused(tool):
    h, _ = harness()
    out = h.call(tool, ARGS[tool], tenant_id="k7:borrower:x")
    assert out["error"]["code"] == "TENANT_SCOPE", out
    assert h.sfn.calls == []
    assert h.reader.calls == []


def test_the_operator_and_employer_rows_resolve_in_kit_k6_only():
    from awx_common.errors import TenantScopeViolation
    from awx_common.tenancy import StaticTenantResolver

    r = StaticTenantResolver(
        {
            OPERATOR: TenantRow("k6", None, "operator"),
            "k6:employer:a": TenantRow("k6", "acct_a1", "customer"),
        }
    )
    op = r.resolve(OPERATOR, kit="k6")
    assert (op.connected_account_id, op.kit, op.role) == (None, "k6", "operator")
    emp = r.resolve("k6:employer:a", kit="k6")
    assert (emp.connected_account_id, emp.kit, emp.role) == ("acct_a1", "k6", "customer")
    for other in ("k5", "k7", "k8"):
        for tid in (OPERATOR, "k6:employer:a"):
            with pytest.raises(TenantScopeViolation):
                r.resolve(tid, kit=other)
