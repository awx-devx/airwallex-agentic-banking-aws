"""Read tools. Pure: they run in the platform context and reach exactly one employer through
ctx.read_tenant, which resolves that employer's tenant server-side (unknown employers are
refused by the TenantResolver). They never add figures across employers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from awx_common import ReadRequest, to_jsonable
from awx_common.errors import TenantScopeViolation
from awx_common.workflow import PolicyContext

from .model import CODE, employer_tenant_id, parse_pay_period
from .workflow import (
    assess_employer,
    get,
    keys_for,
    list_items,
    one_by_request_id,
    require_operator,
    status_line,
    validate_employer,
)


def _reader(ctx: PolicyContext, employer: Any) -> Callable[[ReadRequest], Mapping[str, Any]]:
    if not isinstance(employer, str) or not CODE.match(employer):
        raise TenantScopeViolation("read names no valid employer")
    require_operator(ctx.tenant)
    if ctx.read_tenant is None:
        raise TenantScopeViolation("kit 6 read tools run in the platform context")
    tenant_id = employer_tenant_id(employer)
    read_tenant = ctx.read_tenant
    return lambda req: read_tenant(tenant_id, req)


def read_payroll_plan(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    """Price one employer's payroll and check it against that employer's own wallet."""
    read = _reader(ctx, args.get("employer"))
    intent = validate_employer(args)
    a = assess_employer(intent, read, ctx.read_platform, ctx.platform_store)
    plan = a["plan"]
    return to_jsonable(
        {
            "employer": intent["employer"],
            "pay_period": intent["pay_period"],
            "fundable": plan["shortfall"].is_zero() and not intent["pricing"]["blockers"],
            "blockers": intent["pricing"]["blockers"],
            "pricing": intent["pricing"],
            "required": plan["required"],
            "available": plan["available"],
            "shortfall": plan["shortfall"],
            "conversions": plan["conversions"],
            "already_paid": a["done_transfers"],
            "fee": a["fee_check"],
        }
    )


def read_employer_balances(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    read = _reader(ctx, args.get("employer"))
    rows = list_items(get(read, "/api/v1/balances/current"))
    keep = ("currency", "available_amount", "pending_amount", "total_amount")
    return {
        "employer": args["employer"],
        "balances": [{k: r[k] for k in keep if k in r} for r in rows],
    }


def read_payout_status(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    employer = args.get("employer")
    read = _reader(ctx, employer)
    period = parse_pay_period(args.get("pay_period"))
    contractor = args.get("contractor")
    if not isinstance(contractor, str) or not CODE.match(contractor):
        raise ValueError("contractor must be a platform code")
    k = keys_for(ctx.platform_store, {"employer": employer, "pay_period": period})
    row = (
        None
        if k is None
        else one_by_request_id(read, "/api/v1/transfers", k.payout_rid(contractor))
    )
    out: dict[str, Any] = {
        "employer": employer,
        "contractor": contractor,
        "status": "NOT_SUBMITTED" if row is None else str(row.get("status")),
        "detail": status_line(row),
    }
    if row is not None:
        for k in ("id", "transfer_amount", "transfer_currency", "fee_amount", "failure"):
            if k in row:
                out[k] = row[k]
    return out


READS: dict[str, Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]] = {
    "read_payroll_plan": read_payroll_plan,
    "read_employer_balances": read_employer_balances,
    "read_payout_status": read_payout_status,
}
