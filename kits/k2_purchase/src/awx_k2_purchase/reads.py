"""Kit 2 read tools. Pure: they use ctx.read only and never build an Operation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from awx_common.workflow import PolicyContext

from . import ops
from .amounts import parse_money
from .provider import (
    card_controls,
    card_id_of,
    decline_reason,
    items,
    read_card,
    read_card_limits,
    read_card_transactions,
    rest_txn_state,
    row_amount,
    row_card_transaction_id,
    row_merchant,
)
from .workflow import _card_id, evaluate_purchase, live_unapplied_freezes, validate_terms_input


def read_purchase_plan(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    return evaluate_purchase(validate_terms_input(raw), ctx)


def read_card_controls(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    card_id = _card_id(raw)
    card = ctx.read(read_card(card_id))
    out: dict[str, Any] = {
        "card_id": card_id_of(card) or card_id,
        "card_status": card.get("card_status"),
        "created_by_kit2": ops.is_ours(card),
    }
    notices = live_unapplied_freezes(ctx.store, card_id, card.get("card_status"))
    if notices:
        out["unapplied_freezes"] = notices
    try:
        controls = card_controls(card)
        out["controls"] = controls.to_dict()
        currency = controls.currency
    except (ValueError, KeyError) as exc:
        out["controls_error"] = str(exc)
        return out
    limits = ctx.read(read_card_limits(card_id))
    rows = []
    for row in limits.get("limits") or ():
        entry: dict[str, Any] = {"interval": row.get("interval")}
        for key in ("amount", "remaining"):
            if key in row:
                entry[key] = format(parse_money(row[key], currency, field=key).amount, "f")
        rows.append(entry)
    out["limits"] = rows
    return out


def read_card_transactions_tool(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    card_id = _card_id(raw)
    rows = []
    for t in items(ctx.read(read_card_transactions(card_id))):
        reason = decline_reason(t)
        amount = row_amount(t)  # list amounts are negative; report the absolute value
        mcc, merchant = row_merchant(t)
        rows.append(
            {
                "transaction_id": t.get("transaction_id"),
                "card_transaction_id": row_card_transaction_id(t),
                "state": rest_txn_state(t).value,
                "failure_reason": str(reason) if reason else None,
                "transaction_type": t.get("transaction_type"),
                "provider_status": t.get("status"),
                "amount": amount.to_dict() if amount else None,
                "merchant_category_code": mcc,
                "merchant_name": merchant,
            }
        )
    return {"card_id": card_id, "transactions": rows}


READS: dict[str, Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]] = {
    "read_purchase_plan": read_purchase_plan,
    "read_card_controls": read_card_controls,
    "read_card_transactions": read_card_transactions_tool,
}
