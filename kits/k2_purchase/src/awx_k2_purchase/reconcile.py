"""StepSpec.reconcile hooks: find what a write created, without writing (runner rule 9).

The executor calls a step's hook after OUTCOME_UNKNOWN or DuplicateRequest, with a read already
bound to the operation's scope. Each hook returns the provider object in the shape of that
endpoint's create reply, or None when nothing landed, or raises ReconcileUndetermined when it
cannot tell. Every match is checked against the operation itself: a near miss is
undetermined, never "not landed", because simulation/issuing/create and cardholder create
ignore request_id and a resend after a wrong "not landed" would make a second object.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from awx_common import Money, Operation, ReadRequest
from awx_common.workflow import ReconcileUndetermined

from .controls import CardControls
from .provider import card_id_of, items, row_amount, row_card_transaction_id, row_merchant

Reader = Callable[[ReadRequest], Mapping[str, Any]]


def reconcile_cardholder(op: Operation, read: Reader) -> Mapping[str, Any] | None:
    email = op.body["email"]
    rows = [r for r in items(read(ReadRequest("/api/v1/issuing/cardholders", {"email": email})))]
    matches = [r for r in rows if r.get("email") == email]
    if not matches:
        return None
    if len(matches) > 1:
        raise ReconcileUndetermined(f"{len(matches)} cardholders share {email}")
    found = matches[0]
    if found.get("type") not in (None, op.body["type"]):
        raise ReconcileUndetermined("the cardholder with this email has a different type")
    return found


def reconcile_card(op: Operation, read: Reader) -> Mapping[str, Any] | None:
    holder, name = op.body["cardholder_id"], op.body["nick_name"]
    rows = items(read(ReadRequest("/api/v1/issuing/cards", {"cardholder_id": holder})))
    matches = [r for r in rows if r.get("nick_name") == name]
    if not matches:
        return None
    if len(matches) > 1:
        raise ReconcileUndetermined(f"{len(matches)} cards named {name}")
    found = matches[0]
    if not card_id_of(found):
        raise ReconcileUndetermined("the matching card has no id")
    want = CardControls.from_provider(op.body["authorization_controls"])
    try:
        got = CardControls.from_provider(found.get("authorization_controls") or {})
    except (KeyError, TypeError, ValueError) as exc:
        raise ReconcileUndetermined(f"the matching card's controls are unreadable: {exc}") from exc
    if got != want:
        raise ReconcileUndetermined("a card with this name exists with different controls")
    return found


def reconcile_simulation(op: Operation, read: Reader) -> Mapping[str, Any] | None:
    """Find the probe's authorisation in GET /issuing/transactions?card_id=.

    A row matches on card, abs(amount) (list amounts are negative), currency, MCC and the
    probe's unique merchant name. The result is reshaped like the create reply, keyed by the
    simulation's card_transaction_id from card_transaction_data.
    """
    body = op.body
    want_amount = Money.of(Decimal(body["transaction_amount"]), body["transaction_currency"])
    rows = items(read(ReadRequest("/api/v1/issuing/transactions", {"card_id": body["card_id"]})))
    matches = []
    for row in rows:
        if row.get("card_id") not in (None, body["card_id"]):
            continue
        if row_amount(row) != want_amount:
            continue
        mcc, name = row_merchant(row)
        if mcc not in (None, body["merchant_category_code"]):
            continue
        if name is None:
            raise ReconcileUndetermined(
                "a transaction with this amount has no merchant name to tell probes apart"
            )
        if name == body["merchant_info"]:
            matches.append(row)
    if not matches:
        return None
    if len(matches) > 1:
        raise ReconcileUndetermined(f"{len(matches)} authorisations match this probe")
    row = matches[0]
    txn_id = row_card_transaction_id(row)
    if txn_id is None:
        raise ReconcileUndetermined("the matching row carries no card_transaction_id")
    reason = row.get("failure_reason")
    approved = row.get("status") in ("APPROVED", "PENDING") and not reason
    reply: dict[str, Any] = {
        "card_id": body["card_id"],
        "card_transaction_id": txn_id,
        "process_result": "APPROVED" if approved else "DECLINED",
        "type": row.get("transaction_type"),
        "transaction_amount": want_amount.amount,
        "transaction_currency": want_amount.currency,
    }
    if reason:
        reply["failure_reason"] = reason
    return reply


def reconcile_card_update(op: Operation, read: Reader) -> Mapping[str, Any] | None:
    """A status or control update is a set-to-value: read the card back. If it already shows
    the value the operation set, the write landed (or an equal one did, which is the same
    state); otherwise the outcome stays unknown, because cards/update ignores request_id and
    the runner never resends it (rule 9)."""
    card_id = op.path.rsplit("/", 2)[-2]
    card = read(ReadRequest(f"/api/v1/issuing/cards/{card_id}"))
    if "card_status" in op.body:
        return card if card.get("card_status") == op.body["card_status"] else None
    want = CardControls.from_provider(op.body["authorization_controls"])
    try:
        got = CardControls.from_provider(card.get("authorization_controls") or {})
    except (KeyError, TypeError, ValueError):
        return None
    return card if got == want else None
