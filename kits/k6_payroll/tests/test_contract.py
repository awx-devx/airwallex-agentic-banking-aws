"""The kit surface: SPECS, TOOLS and READS line up; spec shape matches runner rules 3, 5 and 10."""

from awx_common import RiskTier, ToolKind
from awx_common.workflow import KitWorkflowSpec
from awx_k6_payroll import EMPLOYER_SPEC, PAYROLL_SPEC, READS, SPECS, TOOLS
from awx_k6_payroll.workflow import EMPLOYER_LOCK_SPEC, LOCK_SPEC


def test_every_propose_tool_has_a_spec_and_every_read_tool_a_read():
    proposes = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    reads = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert proposes == {s.tool_name for s in SPECS.values()}
    assert proposes == {"propose_run_payroll", "propose_lock_rates", "propose_release_run_claim"}
    assert reads == set(READS)
    assert all(isinstance(s, KitWorkflowSpec) and s.kit == "k6" for s in SPECS.values())


def test_spec_shape():
    assert PAYROLL_SPEC.child is EMPLOYER_SPEC
    assert LOCK_SPEC.child is EMPLOYER_LOCK_SPEC
    assert PAYROLL_SPEC.child_tenant({"employer": "a"}) == "k6:employer:a"
    assert [s.name for s in EMPLOYER_SPEC.steps] == [
        "snapshot",
        "validate_beneficiaries",
        "create_beneficiaries",
        "fx_conversions",
        "payouts",
        "platform_fee",
    ]
    payouts = next(s for s in EMPLOYER_SPEC.steps if s.name == "payouts")
    assert payouts.finality.sandbox_advance is not None  # rule 10, awx_spine's driver
    assert EMPLOYER_SPEC.allows_platform_read("/api/v1/charges/ch_1")
    assert not EMPLOYER_SPEC.allows_read("/api/v1/charges/ch_1")
    assert not PAYROLL_SPEC.allows_platform_read("/api/v1/fx/rates/current")  # no live rate
    # The lock is the only AUTO action; the payroll keeps the default agent floor.
    assert LOCK_SPEC.agent_min_tier is RiskTier.AUTO
    assert PAYROLL_SPEC.agent_min_tier is RiskTier.SINGLE


def test_read_tools_reach_one_employer_through_read_tenant():
    import pytest
    from awx_common import ReadRequest, bind_read
    from awx_common.errors import TenantScopeViolation
    from awx_common.workflow import PolicyContext
    from k6_data import PERIOD, batch
    from k6_fake import PLATFORM, make_env

    env = make_env(a="100", b="99999")

    def read_tenant(tenant_id: str, req: ReadRequest):
        t = env.resolver.resolve(tenant_id, kit="k6")
        return env.fake.read(bind_read(req, t))

    ctx = PolicyContext(
        tenant=PLATFORM,
        now=env.fake.now(),
        read=env.fake.read,
        store=env.platform_store,
        platform_store=env.platform_store,
        read_platform=env.fake.read,
        read_tenant=read_tenant,
    )
    plan = READS["read_payroll_plan"]({"pay_period": PERIOD, **batch()["employers"][0]}, ctx)
    assert plan["fundable"] is False
    assert plan["available"] == {"amount": "100.00", "currency": "USD"}
    obo = {r[2] for r in env.fake.requests if r[0] == "GET" and r[2]}
    assert obo == {env.accts["a"]}
    status = READS["read_payout_status"](
        {"employer": "a", "pay_period": PERIOD, "contractor": "alice"}, ctx
    )
    assert status["status"] == "NOT_SUBMITTED"
    with pytest.raises(TenantScopeViolation):
        READS["read_employer_balances"]({"employer": "zed"}, ctx)
