"""Kit 2 agent tools. read_* explain; propose_* start a workflow that may need approval.

No input carries a server-owned field, and no input is a computed amount: prices, discounts,
dates and forecast flows are what the model extracts from documents. Limits, the plan, the
reconsideration date and every request_id are code's.
"""

from __future__ import annotations

from typing import Any

from awx_common import ToolKind, ToolSpec

from .amounts import AMOUNT_SCHEMA
from .policy_config import POLICY

_DATE = {"type": "string", "format": "date", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
_CARD_ID = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9_-]{1,64}$",
    "description": "Airwallex card id, as returned by read_card_controls or a proposal result",
}

_TERMS_PROPERTIES: dict[str, Any] = {
    "purchase_ref": {
        "type": "string",
        "pattern": r"^[a-z0-9][a-z0-9-]{2,39}$",
        "description": 'Short slug naming this purchase, for example "crm-seat-plan"',
    },
    "vendor": {"type": "string", "minLength": 1, "maxLength": 80},
    "purchase_category": {"type": "string", "enum": sorted(POLICY.mcc_table)},
    "currency": {"type": "string", "enum": sorted(POLICY.currencies)},
    "monthly_price": {**AMOUNT_SCHEMA, "description": "Monthly price as stated in the terms"},
    "annual_price": {**AMOUNT_SCHEMA, "description": "Annual price, only if the terms state it"},
    "annual_discount_percent": {
        "type": "string",
        "pattern": r"^[0-9]{1,2}(\.[0-9]{1,2})?$",
        "description": 'Annual discount as stated, for example "18"; only if no annual_price',
    },
    "cancellation_notice_days": {"type": "integer", "minimum": 0, "maximum": 365},
    "start_date": _DATE,
    "renewal_date": _DATE,
    "terms_document_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
    "forecast_flows": {
        "type": "array",
        "maxItems": 200,
        "description": "Other dated cash flows read from the forecast document",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["date", "direction", "amount"],
            "properties": {
                "date": _DATE,
                "direction": {"type": "string", "enum": ["IN", "OUT"]},
                "amount": AMOUNT_SCHEMA,
                "label": {"type": "string", "maxLength": 80},
            },
        },
    },
}
_TERMS_REQUIRED = [
    "purchase_ref",
    "vendor",
    "purchase_category",
    "currency",
    "monthly_price",
    "cancellation_notice_days",
    "start_date",
    "terms_document_sha256",
]


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


_TERMS_SCHEMA = _obj(_TERMS_PROPERTIES, _TERMS_REQUIRED)
_CARD_SCHEMA = _obj({"card_id": _CARD_ID}, ["card_id"])
_CARD_ACTION_SCHEMA = _obj(
    {"card_id": _CARD_ID, "reason": {"type": "string", "maxLength": 200}}, ["card_id"]
)

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "read_purchase_plan",
        ToolKind.READ,
        "Compare monthly and annual plans for extracted SaaS terms against the current cash "
        "balance and reserve floor. Returns the cash curve, the plan code would choose, the "
        "reconsideration date and the card control set it would propose. Changes nothing.",
        _TERMS_SCHEMA,
    ),
    ToolSpec(
        "read_card_controls",
        ToolKind.READ,
        "Read a card's status, authorization controls and remaining limits.",
        _CARD_SCHEMA,
    ),
    ToolSpec(
        "read_card_transactions",
        ToolKind.READ,
        "List a card's transactions with kit 2's state (CLEARED, DECLINED, ...) and the "
        "provider's decline reason.",
        _CARD_SCHEMA,
    ),
    ToolSpec(
        "propose_issue_card",
        ToolKind.PROPOSE,
        "Propose a cardholder and a virtual card whose controls enforce the chosen plan. Needs "
        "human approval of the exact control set before anything is created.",
        _TERMS_SCHEMA,
    ),
    ToolSpec(
        "propose_update_card_controls",
        ToolKind.PROPOSE,
        "Propose new controls for a kit 2 card from updated terms. A raise needs approval; a "
        "pure reduction runs without one.",
        _obj({"card_id": _CARD_ID, **_TERMS_PROPERTIES}, ["card_id", *_TERMS_REQUIRED]),
    ),
    ToolSpec(
        "propose_freeze_card",
        ToolKind.PROPOSE,
        "Freeze a kit 2 card now (revokes its authority; no approval needed).",
        _CARD_ACTION_SCHEMA,
    ),
    ToolSpec(
        "propose_unfreeze_card",
        ToolKind.PROPOSE,
        "Unfreeze a kit 2 card. Restores spending authority, so it needs approval.",
        _CARD_ACTION_SCHEMA,
    ),
    ToolSpec(
        "propose_close_card",
        ToolKind.PROPOSE,
        "Close a kit 2 card permanently. Irreversible, so it needs approval.",
        _CARD_ACTION_SCHEMA,
    ),
    ToolSpec(
        "propose_run_limit_test",
        ToolKind.PROPOSE,
        "Run the sandbox limit-boundary test on a fresh, pre-funded kit 2 card: wrong currency, "
        "wrong merchant category, limit + 0.01, exactly the limit, and the cumulative limit.",
        _CARD_SCHEMA,
    ),
)
