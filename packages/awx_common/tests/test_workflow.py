"""KitWorkflowSpec, StepSpec and FinalitySpec shape rules and optional hooks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from awx_common import OperationResult, OperationStatus, ReadRequest, RiskTier
from awx_common.workflow import (
    ABANDON_REASONS,
    FinalitySpec,
    KitWorkflowSpec,
    PolicyDecision,
    ReconcileUndetermined,
    StepSpec,
)


def spec(**overrides):
    fields = {
        "kit": "k4",
        "action": "submit_evidence",
        "validate": dict,
        "policy": lambda intent, ctx: PolicyDecision(RiskTier.SINGLE, ("evidence submission",)),
        "steps": (StepSpec("submit", lambda ctx: ()),),
        "approval_ttl": timedelta(hours=4),
        "read_allowlist": (r"^/api/v1/pa/payment_disputes/[A-Za-z0-9_-]+$",),
    }
    fields.update(overrides)
    return KitWorkflowSpec(**fields)


def test_valid_spec_and_tool_name():
    s = spec()
    assert s.tool_name == "propose_submit_evidence"
    assert s.allows_read("/api/v1/pa/payment_disputes/dsp_123")
    assert not s.allows_read("/api/v1/pa/payment_disputes/dsp_123/../../transfers")
    assert not s.allows_read("/api/v1/transfers")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kit": "k9"}, "unknown kit"),
        ({"action": "Submit"}, "snake_case"),
        ({"steps": ()}, "steps or a fan_out"),
        ({"approval_ttl": timedelta(0)}, "positive"),
        ({"read_allowlist": ("/api/v1/transfers",)}, "anchored"),
        ({"steps": (StepSpec("a", lambda c: ()), StepSpec("a", lambda c: ()))}, "unique"),
        ({"fan_out": lambda i, d: ()}, "go together"),
    ],
)
def test_invalid_specs(overrides, message):
    with pytest.raises(ValueError, match=message):
        spec(**overrides)


def test_finality_rules():
    with pytest.raises(ValueError, match="terminal"):
        FinalitySpec(
            read_request=lambda r: None,
            status_of=lambda b: b["status"],
            terminal=frozenset({"SETTLED"}),
            success=frozenset({"PAID"}),
        )
    with pytest.raises(ValueError, match="interval"):
        FinalitySpec(
            read_request=lambda r: None,
            status_of=lambda b: b["status"],
            terminal=frozenset({"SETTLED"}),
            success=frozenset({"SETTLED"}),
            interval_s=10,
            timeout_s=5,
        )


def test_platform_read_allowlist():
    spec = KitWorkflowSpec(
        kit="k7",
        action="reconcile_portfolio",
        validate=dict,
        policy=lambda i, c: PolicyDecision(RiskTier.AUTO, ("read only",)),
        steps=(StepSpec("noop", lambda ctx: ()),),
        approval_ttl=timedelta(hours=1),
        read_allowlist=(),
        platform_read_allowlist=(r"^/api/v1/balances/current$",),
    )
    assert spec.allows_platform_read("/api/v1/balances/current")
    assert not spec.allows_read("/api/v1/balances/current")
    with pytest.raises(ValueError, match="anchored"):
        replace(spec, platform_read_allowlist=("/api/v1/balances/current",))


def base_spec(**overrides):
    fields = {
        "kit": "k6",
        "action": "run_payroll",
        "validate": dict,
        "policy": lambda intent, ctx: PolicyDecision(RiskTier.DUAL, ("payroll run",)),
        "steps": (),
        "approval_ttl": timedelta(hours=8),
        "read_allowlist": (r"^/api/v1/balances/current$",),
    }
    fields.update(overrides)
    return KitWorkflowSpec(**fields)


def child_spec():
    return base_spec(
        action="pay_employer",
        policy=lambda intent, ctx: PolicyDecision(RiskTier.AUTO, ("covered by parent",)),
        steps=(StepSpec("payouts", lambda ctx: (), reconcile=lambda op, read: None),),
    )


def test_child_tenant_requires_fan_out():
    with pytest.raises(ValueError, match="child_tenant needs a fan_out"):
        replace(child_spec(), child_tenant=lambda intent: "k6:employer:acme")
    parent = base_spec(
        fan_out=lambda intent, decision: [{"employer": "acme"}],
        child=child_spec(),
        child_tenant=lambda intent: f"k6:employer:{intent['employer']}",
    )
    assert parent.child_tenant({"employer": "acme"}) == "k6:employer:acme"


def test_abandon_hook_and_reasons():
    seen = []
    spec = replace(child_spec(), on_abandon=lambda ctx, reason: seen.append(reason))
    spec.on_abandon(None, "STALE")
    assert seen == ["STALE"]
    assert frozenset({"REJECTED", "EXPIRED", "BLOCKED", "STALE"}) == ABANDON_REASONS


def test_reconcile_hook_is_optional():
    assert StepSpec("x", lambda ctx: ()).reconcile is None
    assert child_spec().steps[0].reconcile(None, None) is None


def test_reconcile_can_signal_undetermined():
    def reconcile(op, read):
        raise ReconcileUndetermined("list read is stale")

    step = StepSpec("simulate", lambda ctx: (), reconcile=reconcile)
    with pytest.raises(ReconcileUndetermined):
        step.reconcile(None, None)


def test_sandbox_advance_is_optional_and_receives_attempts():
    calls = []

    def advance(op, result, body, now, attempt):
        calls.append((body["status"], attempt))
        return None

    spec = FinalitySpec(
        read_request=lambda r: ReadRequest(path=f"/api/v1/transfers/{r.provider_id}"),
        status_of=lambda b: b["status"],
        terminal=frozenset({"PAID", "CANCELLED"}),
        success=frozenset({"PAID"}),
        sandbox_advance=advance,
    )
    result = OperationResult(request_id="r", status=OperationStatus.SUCCEEDED, provider_id="t1")
    assert spec.read_request(result).path == "/api/v1/transfers/t1"
    assert spec.max_sandbox_advances == 6
    now = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
    assert spec.sandbox_advance(None, result, {"status": "SENT"}, now, 2) is None
    assert calls == [("SENT", 2)]
    plain = FinalitySpec(
        read_request=spec.read_request,
        status_of=spec.status_of,
        terminal=spec.terminal,
        success=spec.success,
    )
    assert plain.sandbox_advance is None
