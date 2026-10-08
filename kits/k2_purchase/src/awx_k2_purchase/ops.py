"""Operation builders. Pure: the same inputs always build byte-identical Operations.

Every request_id comes from derive_request_id(kit, business_key, step), where the business key
names the business object, never a hash of the intent, proposal or computed
amounts. Every business key starts with the kit's book id (rule M2): a random id minted once
when kit 2's state for the tenant is first created (workflow.ensure_book), so a fresh book
pointed at the same purchase or card never reuses an earlier run's request_ids:

- `purchase:<purchase_ref>` for the cardholder and card that pay one purchase, so a second
  proposal for the same purchase reuses the card's request_id and Airwallex refuses it
- `card:<card_id>` for control and status changes, with the card's change number in the step
  (`change:<n>`): every distinct change gets the next number, claimed once in the kit store and
  bound into the approval, so a retry of one change reuses its request_id (see
  workflow.claim_change and DESIGN.md, Request ids)
- `limit-test:<card_id>` for the probes of the one limit test a card can have

Op names follow awx_spine's endpoint table. The spine sends bodies exactly as built, so
request_id appears only where the endpoint uses it: card create (Airwallex rejects a reused one,
FAKE-FIDELITY.md, request_id idempotency). Cardholder create and simulation/issuing/create
ignore request_id, so it is not sent; their only duplicate guard is the operation store plus the
step's reconcile hook. Every object kit 2 creates carries the `poc-k2-` label.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common import Money, Operation, derive_request_id

from .controls import CardControls

KIT = "k2"
LABEL = "poc-k2-"
LIMIT_TEST_REF_PREFIX = "limit-test-"
LIMIT_TEST_LABEL = f"{LABEL}{LIMIT_TEST_REF_PREFIX}"

SIMULATE_AUTHORIZATION = "issuing.simulate_authorization"
CARDHOLDER_CREATE = "issuing.cardholders.create"
CARD_CREATE = "issuing.cards.create"
CARD_UPDATE = "issuing.cards.update"


def nick_name(purchase_ref: str) -> str:
    return f"{LABEL}{purchase_ref}"[:32]


def cardholder_email(book: str, purchase_ref: str) -> str:
    """Carries the book (M2): a fresh book never adopts an earlier book's cardholder on the
    shared sandbox, so a second card never lands under it."""
    return f"{LABEL}{_book(book)[3:11]}-{purchase_ref}@example.com"


def is_ours(card: Mapping[str, Any]) -> bool:
    name = card.get("nick_name")
    return isinstance(name, str) and name.startswith(LABEL)


def is_limit_test_card(card: Mapping[str, Any]) -> bool:
    name = card.get("nick_name")
    return isinstance(name, str) and name.startswith(LIMIT_TEST_LABEL)


def _book(book: str) -> str:
    if not isinstance(book, str) or not book.startswith("bk-") or len(book) < 12:
        raise ValueError("a minted book id is required (workflow.ensure_book)")
    return book


def purchase_key(book: str, purchase_ref: str) -> str:
    return f"{_book(book)}:purchase:{purchase_ref}"


def card_key(book: str, card_id: str) -> str:
    return f"{_book(book)}:card:{card_id}"


def limit_test_key(book: str, card_id: str) -> str:
    return f"{_book(book)}:limit-test:{card_id}"


MERCHANT_NAME_MAX = 25  # live sandbox: merchant.name keeps 25 chars; the rest goes to city
_PROBE_CODE = {
    "probe_currency": "cu",
    "probe_mcc": "mc",
    "probe_over_limit": "ol",
    "probe_at_limit": "al",
    "probe_cumulative": "cm",
}


def probe_merchant(card_id: str, probe: str) -> str:
    """Unique per card and probe, so reconcile can tell one probe's authorisation apart.

    25 characters at most: the live sandbox echoes only the first 25 characters of
    merchant_info as merchant.name on a transaction row (live run, 6 Oct; a longer value is
    split into merchant.name and merchant.city), so the whole value must fit."""
    compact = card_id.replace("-", "")
    return f"{LABEL}{_PROBE_CODE[probe]}-{compact}"[:MERCHANT_NAME_MAX]


def _op(
    name: str,
    path: str,
    body: dict[str, Any],
    *,
    business_key: str,
    step: str,
    intent_hash: str,
    send_request_id: bool,
) -> Operation:
    rid = derive_request_id(KIT, business_key, step)
    if send_request_id:
        body = {"request_id": rid, **body}
    return Operation(
        kit=KIT,
        name=name,
        method="POST",
        path=path,
        body=body,
        request_id=rid,
        business_key=business_key,
        intent_hash=intent_hash,
    )


def cardholder_create(book: str, purchase_ref: str, intent_hash: str) -> Operation:
    """DELEGATE cardholder: the shape verified in the sandbox (202, READY at once,
    FAKE-FIDELITY.md, Issuing)."""
    return _op(
        CARDHOLDER_CREATE,
        "/api/v1/issuing/cardholders/create",
        {"type": "DELEGATE", "email": cardholder_email(book, purchase_ref)},
        business_key=purchase_key(book, purchase_ref),
        step="cardholder",
        intent_hash=intent_hash,
        send_request_id=False,
    )


def card_create(
    book: str, purchase_ref: str, cardholder_id: str, controls: CardControls, intent_hash: str
) -> Operation:
    body = {
        "cardholder_id": cardholder_id,
        "created_by": "poc-k2 purchase agent",
        "form_factor": "VIRTUAL",
        "is_personalized": False,
        "nick_name": nick_name(purchase_ref),
        "program": {"purpose": "COMMERCIAL"},
        "authorization_controls": controls.to_payload(),
    }
    return _op(
        CARD_CREATE,
        "/api/v1/issuing/cards/create",
        body,
        business_key=purchase_key(book, purchase_ref),
        step="card",
        intent_hash=intent_hash,
        send_request_id=True,
    )


def card_update_controls(
    book: str, card_id: str, controls: CardControls, change_seq: int, intent_hash: str
) -> Operation:
    """Setting controls is a set-to-value, safe to repeat. `change_seq` is the card's change
    number from claim_change: one number per distinct change, never a calendar bucket."""
    return _op(
        CARD_UPDATE,
        f"/api/v1/issuing/cards/{card_id}/update",
        {"authorization_controls": controls.to_payload()},
        business_key=card_key(book, card_id),
        step=f"change:{_seq(change_seq)}",
        intent_hash=intent_hash,
        send_request_id=False,
    )


def card_update_status(
    book: str, card_id: str, status: str, change_seq: int, intent_hash: str
) -> Operation:
    if status not in ("ACTIVE", "INACTIVE", "CLOSED"):
        raise ValueError(f"card_status {status!r} is not one kit 2 sets")
    return _op(
        CARD_UPDATE,
        f"/api/v1/issuing/cards/{card_id}/update",
        {"card_status": status},
        business_key=card_key(book, card_id),
        step=f"change:{_seq(change_seq)}",
        intent_hash=intent_hash,
        send_request_id=False,
    )


def _seq(change_seq: int) -> int:
    if isinstance(change_seq, bool) or not isinstance(change_seq, int) or change_seq < 1:
        raise ValueError("change_seq must be a positive int")
    return change_seq


def simulate_authorization(
    book: str, card_id: str, amount: Money, merchant_category: str, probe: str, intent_hash: str
) -> Operation:
    """Sandbox authorisation simulation with single_phase: an approval comes back as an
    approved CLEARING in one call (FAKE-FIDELITY.md, Issuing: the guide's capture path returns
    400 and the lifecycle path needs an admin key). The endpoint ignores request_id, so none is
    sent."""
    body = {
        "card_id": card_id,
        "transaction_amount": amount.amount,
        "transaction_currency": amount.currency,
        "merchant_category_code": merchant_category,
        "merchant_info": probe_merchant(card_id, probe),
        "single_phase": True,
    }
    return _op(
        SIMULATE_AUTHORIZATION,
        "/api/v1/simulation/issuing/create",
        body,
        business_key=limit_test_key(book, card_id),
        step=probe,
        intent_hash=intent_hash,
        send_request_id=False,
    )
