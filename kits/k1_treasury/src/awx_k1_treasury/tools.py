"""Agent tools for kit 1. IDs and cited evidence in; no amount, account or request_id ever."""

from __future__ import annotations

from awx_common import ToolKind, ToolSpec

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}

_CONTRADICTIONS = {
    "type": "array",
    "maxItems": 10,
    "description": (
        "Evidence that contradicts a receipt forecast, each with the receipt id, the source "
        "document and the exact line quoted. Contradictions can only lower confidence."
    ),
    "items": {
        "type": "object",
        "properties": {
            "receipt_id": {"type": "string", "maxLength": 64},
            "source": {"type": "string", "maxLength": 200},
            "quote": {"type": "string", "maxLength": 500},
        },
        "required": ["receipt_id", "source", "quote"],
        "additionalProperties": False,
    },
}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "read_balances",
        ToolKind.READ,
        "Current available balance, reserve floor and headroom for each treasury wallet.",
        _EMPTY,
    ),
    ToolSpec(
        "read_obligations",
        ToolKind.READ,
        "Every obligation in the treasury book with its due time and due_window (OVERDUE, "
        "NEXT_72_HOURS or LATER, from as_of), and the expected receipts.",
        _EMPTY,
    ),
    ToolSpec(
        "read_fx_rate",
        ToolKind.READ,
        "Indicative rate for buying one of the book's currencies with the funding currency. "
        "Not a quote and books nothing.",
        {
            "type": "object",
            "properties": {"buy_currency": {"type": "string", "pattern": "^[A-Z]{3}$"}},
            "required": ["buy_currency"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "read_treasury_plan",
        ToolKind.READ,
        "The deterministic plan for every obligation (fund, convert and fund, defer, escalate), "
        "fees included and checked against the reserve floor across the whole plan, with the "
        "approval tier the fundable set would need. Pass contradictions to preview their effect.",
        {
            "type": "object",
            "properties": {"contradictions": _CONTRADICTIONS},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "propose_lock_rate",
        ToolKind.PROPOSE,
        "Lock today's FX rate for the conversions the named obligations need: one Airwallex "
        "quote per currency, stored for propose_execute_plan. Moves no money. Do this first; "
        "execute_plan is blocked without a current lock.",
        {
            "type": "object",
            "properties": {
                "obligation_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "items": {"type": "string", "maxLength": 64},
                }
            },
            "required": ["obligation_ids"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "propose_release_obligation",
        ToolKind.PROPOSE,
        "Ask a person to release (or mark paid) an obligation whose payment is stuck and that "
        "read_obligations reports as needs_person. Code reads the provider evidence and sets "
        "the approval tier; nothing is sent to Airwallex.",
        {
            "type": "object",
            "properties": {
                "obligation_id": {"type": "string", "maxLength": 64},
                "reason": {"type": "string", "minLength": 10, "maxLength": 500},
            },
            "required": ["obligation_id", "reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "propose_execute_plan",
        ToolKind.PROPOSE,
        "Propose executing the plan for the named obligations: one FX conversion per currency "
        "that needs it, then one transfer per obligation. Code computes every amount and the "
        "approval tier; a person approves when the tier requires it.",
        {
            "type": "object",
            "properties": {
                "obligation_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "items": {"type": "string", "maxLength": 64},
                },
                "contradictions": _CONTRADICTIONS,
            },
            "required": ["obligation_ids"],
            "additionalProperties": False,
        },
    ),
)
