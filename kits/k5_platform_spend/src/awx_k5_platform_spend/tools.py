"""Kit 5 agent tools. Read tools are per tenant (no cross-tenant listing exists); propose tools
take references and words only. No input carries an account, tenant, amount the code computes,
or request_id; `ToolSpec` refuses server-owned field names at import."""

from __future__ import annotations

from typing import Any

from awx_common import ToolKind, ToolSpec

from .validation import RATIONALE_MAX


def _schema(
    required: dict[str, dict[str, Any]], optional: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    props = {**required, **(optional or {})}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": props,
        "required": sorted(required),
    }


_REF = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$"}
_RATIONALE = {"rationale": {"type": "string", "maxLength": RATIONALE_MAX}}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "read_tenant_summary",
        ToolKind.READ,
        "Balances of the signed-in customer's wallet. One tenant only.",
        _schema({}),
    ),
    ToolSpec(
        "read_cards",
        ToolKind.READ,
        "Cards issued on the signed-in customer's account, with status and controls.",
        _schema({}),
    ),
    ToolSpec(
        "read_card_transactions",
        ToolKind.READ,
        "Card transactions for one of the customer's cards, with decline reasons.",
        _schema({"card_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}}),
    ),
    ToolSpec(
        "propose_issue_card",
        ToolKind.PROPOSE,
        "Propose a virtual card for the customer under a named card program. The cardholder is "
        "created server-side; limits come from the program.",
        _schema({"cardholder_ref": _REF, "program": _REF}, _RATIONALE),
    ),
    ToolSpec(
        "propose_simulate_card_spend",
        ToolKind.PROPOSE,
        "Sandbox only: simulate one card transaction on a card this kit issued. Each "
        "scenario_ref runs once; a replay is refused.",
        _schema(
            {
                "scenario_ref": _REF,
                "card_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                "amount": {"type": "string", "pattern": r"^[0-9]{1,9}(\.[0-9]{1,3})?$"},
                "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
                "merchant_category_code": {"type": "string", "pattern": "^[0-9]{4}$"},
            }
        ),
    ),
    ToolSpec(
        "read_bridge_position",
        ToolKind.READ,
        "The customer's own bridge receivable, bridges in flight and queued shortfalls. as_of is "
        "the UTC date the kit treats as today. Each queued shortfall has its obligation's due "
        "date (due_on) and a due_status of due_today, overdue or not_yet_due, each null if "
        "unknown. A queued shortfall is already registered: proposing register_shortfall for it "
        "again only refreshes its queued amount and observation time, and is blocked unless the "
        "payment is due today.",
        _schema({}),
    ),
    ToolSpec(
        "propose_register_shortfall",
        ToolKind.PROPOSE,
        "Queue the customer's shortfall on a payment due today for the day's bridge ranking. "
        "Code computes the shortfall; no money moves.",
        _schema({"obligation_ref": _REF}, _RATIONALE),
    ),
    ToolSpec(
        "propose_bridge_shortfall",
        ToolKind.PROPOSE,
        "Propose bridging the customer's shortfall on a payment due today from platform capital. "
        "Code computes the amount, ranks competing requests and enforces the reserve floor. "
        "Explain the priority rationale in `rationale`.",
        _schema({"obligation_ref": _REF}, _RATIONALE),
    ),
    ToolSpec(
        "propose_reconcile_bridge",
        ToolKind.PROPOSE,
        "Check a bridge that is still in flight for this customer's obligation and record its "
        "outcome if the provider has settled or failed it. Moves no money, and code records "
        "only a definitive outcome.",
        _schema({"obligation_ref": _REF}, _RATIONALE),
    ),
    ToolSpec(
        "propose_collect_fee",
        ToolKind.PROPOSE,
        "Propose charging the customer's monthly platform fee for a period (YYYY-MM). The fee "
        "comes from the fee schedule.",
        _schema(
            {"period": {"type": "string", "pattern": "^20[0-9]{2}-(0[1-9]|1[0-2])$"}}, _RATIONALE
        ),
    ),
    ToolSpec(
        "propose_collect_repayment",
        ToolKind.PROPOSE,
        "Propose collecting the customer's open bridge advance back. The amount is the ledger "
        "receivable. Code checks whether it can run when you propose.",
        _schema({}, _RATIONALE),
    ),
)
