"""Fixture specs. pay_supplier's policy is AUTO at or below 100.00 and reads every PolicyContext
capability (tenant read, platform read, kit store) the way the kit 5, 7 and 8 ledgers do."""

from datetime import timedelta
from decimal import Decimal

from awx_common.approvals import RiskTier
from awx_common.money import Money
from awx_common.ops import ReadRequest
from awx_common.workflow import KitWorkflowSpec, PolicyDecision, StepSpec

AUTO_LIMIT = Decimal("100")


def validate(raw):
    if raw.get("currency") not in ("AUD", "USD"):
        raise ValueError("currency must be AUD or USD")
    amount = Money.of(raw["amount"], raw["currency"])
    if amount.amount <= 0:
        raise ValueError("amount must be positive")
    return {"amount": amount, "beneficiary_name": str(raw["beneficiary_name"])}


def policy(intent, ctx):
    balances = ctx.read(ReadRequest(path="/api/v1/balances/current"))
    account = ctx.read_platform(ReadRequest(path="/api/v1/account"))
    limit = ctx.store.get("limits")
    tier = RiskTier.SINGLE if intent["amount"].amount > AUTO_LIMIT else RiskTier.AUTO
    fee = Money.of("1.50", intent["amount"].currency)
    return PolicyDecision(
        risk_tier=tier,
        reasons=("fixture rule",),
        computed={
            "fee": fee,
            "tenant": ctx.tenant.tenant_id,
            "connected_account": ctx.tenant.connected_account_id or "",
            "platform_account": str(account.get("id", "")),
            "limit_version": limit.version if limit else 0,
            "balance_rows": len(balances.get("items", balances.get("balances", []))),
        },
    )


def policy_that_writes(intent, ctx):
    ctx.store.put("oops", {"x": "1"}, expected_version=None)
    return PolicyDecision(risk_tier=RiskTier.SINGLE, reasons=())


def build(step_ctx):
    raise AssertionError("tool handlers must never build operations")


def _spec(action, pol):
    return KitWorkflowSpec(
        kit="k1",
        action=action,
        validate=validate,
        policy=pol,
        steps=(StepSpec(name="transfer", build=build),),
        approval_ttl=timedelta(hours=1),
        read_allowlist=("^/api/v1/balances/current$",),
        # The platform tenant (kits 1 to 4) reads its own account under the platform list.
        platform_read_allowlist=("^/api/v1/balances/current$", "^/api/v1/account$"),
    )


SPECS = {
    "pay_supplier": _spec("pay_supplier", policy),
    "probe_write": _spec("probe_write", policy_that_writes),
}
