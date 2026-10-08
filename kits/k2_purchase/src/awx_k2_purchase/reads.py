"""Kit 2 read tools. Pure: they use ctx.read only and never build an Operation."""

from __future__ import annotations

import re
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
from .workflow import (
    _card_id,
    _existing_card_for_purchase,
    current_book,
    evaluate_purchase,
    live_unapplied_freezes,
    validate_terms_input,
)

_PURCHASE_REF = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")  # as tools.py's purchase_ref


def read_purchase_plan(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    return evaluate_purchase(validate_terms_input(raw), ctx)


def _card_for_read(raw: Mapping[str, Any], ctx: PolicyContext) -> tuple[str | None, str | None]:
    """The card id to read, from card_id or purchase_ref (the caller checked exactly one is
    given). A purchase_ref names the card kit 2's current book issued for it, found by its
    cardholder's email as issue_card's own retry check finds it, so the agent can learn a new
    card's id."""
    if raw.get("card_id") is not None:
        return _card_id(raw), None
    ref = raw["purchase_ref"]
    if not isinstance(ref, str) or not _PURCHASE_REF.match(ref):
        raise ValueError("purchase_ref must be a slug of 3-40 characters: a-z, 0-9 and '-'")
    found = _existing_card_for_purchase(ctx, ref, current_book(ctx))
    return (found[0] if found is not None else None), ref


def read_card_controls(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    if (raw.get("card_id") is None) == (raw.get("purchase_ref") is None):
        # Answered, not raised: a read's ValueError reaches the model only as INTERNAL.
        return {"card_id": None, "detail": "give exactly one of card_id or purchase_ref"}
    card_id, purchase_ref = _card_for_read(raw, ctx)
    if card_id is None:
        return {
            "purchase_ref": purchase_ref,
            "card_id": None,
            "detail": "kit 2 has issued no card for this purchase_ref; an issue_card proposal "
            "creates it only once it is approved and its run has succeeded",
        }
    card = ctx.read(read_card(card_id))
    out: dict[str, Any] = {
        "card_id": card_id_of(card) or card_id,
        "card_status": card.get("card_status"),
        "created_by_kit2": ops.is_ours(card),
    }
    if purchase_ref is not None:
        out["purchase_ref"] = purchase_ref
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
