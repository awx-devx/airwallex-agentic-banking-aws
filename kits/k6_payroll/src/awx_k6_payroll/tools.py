"""Kit 6 agent tools, as data. The model names employers and contractors by platform code; it
never supplies a connected account, tenant, request_id or computed amount."""

from __future__ import annotations

from typing import Any

from awx_common import ToolKind, ToolSpec

from .model import CODE, MAX_CONTRACTORS, MAX_EMPLOYERS, PAY_PERIOD


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else sorted(props),
        "additionalProperties": False,
    }


_STR = {"type": "string", "minLength": 1, "maxLength": 140}
_CODE = {"type": "string", "pattern": CODE.pattern}
_PERIOD = {"type": "string", "pattern": PAY_PERIOD.pattern}
_MONEY = _obj(
    {
        "amount": {"type": "string", "pattern": r"^\d+(\.\d+)?$"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
    }
)
_CONTRACTOR = _obj(
    {
        "contractor": _CODE,
        "first_name": _STR,
        "last_name": _STR,
        "amount": _MONEY,
        "transfer_method": {"type": "string", "enum": ["LOCAL", "SWIFT"]},
        "address": _obj(
            {
                "street_address": _STR,
                "city": _STR,
                "state": _STR,
                "postcode": _STR,
                "country_code": {"type": "string", "enum": ["US", "GB", "DE"]},
            },
            ["street_address", "city", "postcode", "country_code"],
        ),
        "bank": _obj(
            {
                "account_name": _STR,
                "account_number": _STR,
                "routing_number": _STR,
                "category": {"type": "string", "enum": ["Checking", "Savings"]},
                "sort_code": _STR,
                "bank_name": _STR,
                "iban": _STR,
                "swift_code": _STR,
            },
            ["account_name"],
        ),
    }
)
_EMPLOYER_PROPS = {
    "employer": _CODE,
    "funding_currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
    "contractors": {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_CONTRACTORS,
        "items": _CONTRACTOR,
    },
}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="propose_run_payroll",
        kind=ToolKind.PROPOSE,
        description=(
            "Propose one pay period's contractor payroll for several employers, copied from the "
            "payroll file. Code prices fees and FX, checks each employer against its own wallet "
            "only, and starts one run per employer. A short employer is "
            "escalated, never funded from another employer."
        ),
        input_schema=_obj(
            {
                "pay_period": _PERIOD,
                "employers": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_EMPLOYERS,
                    "items": _obj(_EMPLOYER_PROPS),
                },
            }
        ),
    ),
    ToolSpec(
        name="propose_lock_rates",
        kind=ToolKind.PROPOSE,
        description=(
            "Propose locking FX quotes for one pay period's payroll, same input as "
            "propose_run_payroll. Code quotes each employer's conversions on that employer's "
            "own account and stores the quotes; it moves no money. Then propose the payroll, "
            "which binds the exact locked sell amounts. Locks last about an hour."
        ),
        input_schema=_obj(
            {
                "pay_period": _PERIOD,
                "employers": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_EMPLOYERS,
                    "items": _obj(_EMPLOYER_PROPS),
                },
            }
        ),
    ),
    ToolSpec(
        name="propose_release_run_claim",
        kind=ToolKind.PROPOSE,
        description=(
            "Propose releasing a stuck run claim for one employer and pay period, left by "
            "a run that crashed. Refused while the claim is young enough for its run to be "
            "live. Moves no money: anything that run left in flight stays tracked."
        ),
        input_schema=_obj(
            {
                "employer": _CODE,
                "pay_period": _PERIOD,
                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
            }
        ),
    ),
    ToolSpec(
        name="read_payroll_plan",
        kind=ToolKind.READ,
        description=(
            "Price one employer's payroll for a pay period and compare it with that employer's "
            "own funding balance. Returns fees, conversions, required, available and shortfall "
            "as computed by code; explain them, do not recompute them."
        ),
        input_schema=_obj({"pay_period": _PERIOD, **_EMPLOYER_PROPS}),
    ),
    ToolSpec(
        name="read_employer_balances",
        kind=ToolKind.READ,
        description="Current wallet balances of one employer. Never add balances across employers.",
        input_schema=_obj({"employer": _CODE}),
    ),
    ToolSpec(
        name="read_payout_status",
        kind=ToolKind.READ,
        description=(
            "Status of one contractor's payout for a pay period, with failure_type and "
            "failure_reason. SENT is still in flight; CANCELLED means it failed."
        ),
        input_schema=_obj({"employer": _CODE, "pay_period": _PERIOD, "contractor": _CODE}),
    ),
)
