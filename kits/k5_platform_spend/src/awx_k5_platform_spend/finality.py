"""Finality and reconciliation: when a kit 5 write is really done, and finding it when unsure.

Platform movements (`connected_account_transfers/create`, `charges/create`) return
`status: NEW`. A create response is never final; the object is read until SETTLED. The API
reference lists NEW, PENDING, SETTLED, SUSPENDED and FAILED (awx_spine FAKE-FIDELITY.md, row
(d)): SETTLED is success, FAILED is failure, SUSPENDED is terminal for the workflow because it
needs a person. Any other status stays non-terminal and the poll times out into an unknown
outcome rather than being guessed at.

Reconcile hooks (`StepSpec.reconcile(op, read)`) run after OUTCOME_UNKNOWN or DuplicateRequest.
`read` already carries the operation's on_behalf_of (rule 5). A hook returns the object it
found, None when it is certainly absent, or raises `ReconcileUndetermined` when it cannot tell.
Any object found is checked against the operation before it is accepted (rule 9).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from awx_common import Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.workflow import FinalitySpec, ReconcileUndetermined

from .amounts import items_of, provider_decimal

SETTLED = "SETTLED"
SUSPENDED = "SUSPENDED"
MOVEMENT_FAILED = "FAILED"
MOVEMENT_TERMINAL = frozenset({SETTLED, MOVEMENT_FAILED, SUSPENDED})
MOVEMENT_SUCCESS = frozenset({SETTLED})

TRANSFERS_PATH = "/api/v1/connected_account_transfers"
CHARGES_PATH = "/api/v1/charges"
CARDHOLDERS_PATH = "/api/v1/issuing/cardholders"
CARDS_PATH = "/api/v1/issuing/cards"
TRANSACTIONS_PATH = "/api/v1/issuing/transactions"

ReadFn = Callable[[ReadRequest], Mapping[str, Any]]


def _object_read(base: str) -> Callable[[OperationResult], ReadRequest]:
    def read_request(result: OperationResult) -> ReadRequest:
        if not result.provider_id:
            raise ValueError("cannot poll an object without its provider id; reconcile first")
        return ReadRequest(path=f"{base}/{result.provider_id}")  # header inherited (rule 5)

    return read_request


def _status(key: str) -> Callable[[Mapping[str, Any]], str]:
    def status_of(body: Mapping[str, Any]) -> str:
        status = body.get(key)
        if not isinstance(status, str):
            raise ValueError(f"object has no {key}")
        return status

    return status_of


NOT_VISIBLE = "NOT_VISIBLE"  # our own label: the list does not show the object yet
AMBIGUOUS = "AMBIGUOUS"  # our own label: two objects under one request_id (never terminal)


def _by_request_id(base: str) -> Callable[[OperationResult], ReadRequest]:
    """Platform movements are polled through `?request_id=` rather than GET by id: a create can
    stay out of reads for a while after it lands (list lag, FAKE-FIDELITY), and a GET by id
    then answers 404, which would fail the run while the money has moved."""

    def read_request(result: OperationResult) -> ReadRequest:
        return ReadRequest(path=base, query={"request_id": result.request_id})

    return read_request


def movement_status(body: Mapping[str, Any]) -> str:
    """Status of the one movement a `?request_id=` list returns. Absent or duplicated objects
    get labels outside the provider vocabulary, so they are never terminal."""
    if "items" not in body:
        return _status("status")(body)
    items = items_of(body)
    if not items:
        return NOT_VISIBLE
    if len(items) > 1:
        return AMBIGUOUS
    return _status("status")(items[0])


def select_movement(body: Mapping[str, Any], result: OperationResult) -> Mapping[str, Any] | None:
    """`FinalitySpec.select`: this operation's own row in a `?request_id=` list. When the
    create reply named its object, only that object counts. None (absent, or two candidate rows)
    keeps the poll non-terminal; the postcondition then keeps the money in flight."""
    rows = [i for i in items_of(body) if i.get("request_id") == result.request_id]
    if result.provider_id:
        rows = [i for i in rows if i.get("id") == result.provider_id]
    return rows[0] if len(rows) == 1 else None


BRIDGE_FINALITY = FinalitySpec(
    read_request=_by_request_id(TRANSFERS_PATH),
    status_of=movement_status,
    select=select_movement,
    terminal=MOVEMENT_TERMINAL,
    success=MOVEMENT_SUCCESS,
)
CHARGE_FINALITY = FinalitySpec(
    read_request=_by_request_id(CHARGES_PATH),
    status_of=movement_status,
    select=select_movement,
    terminal=MOVEMENT_TERMINAL,
    success=MOVEMENT_SUCCESS,
)
# Issuing objects are customer-scoped: the finality read inherits the op's x-on-behalf-of.
CARDHOLDER_FINALITY = FinalitySpec(
    read_request=_object_read(CARDHOLDERS_PATH),
    status_of=_status("status"),
    terminal=frozenset({"READY", "DISABLED"}),
    success=frozenset({"READY"}),
    interval_s=2,
    timeout_s=60,
)
CARD_FINALITY = FinalitySpec(
    read_request=_object_read(CARDS_PATH),
    status_of=_status("card_status"),
    terminal=frozenset({"ACTIVE", "INACTIVE", "CLOSED", "LOST", "STOLEN", "FAILED"}),
    success=frozenset({"ACTIVE"}),
    interval_s=2,
    timeout_s=60,
)


class CreateOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"  # 2xx; still not final until the finality read says so
    REJECTED = "REJECTED"  # provider refused it; nothing was created
    DUPLICATE = "DUPLICATE"  # request_id seen before; read the original
    UNKNOWN = "UNKNOWN"  # may or may not exist; never treat as rejected


def interpret_create(result: OperationResult) -> CreateOutcome:
    if result.status is OperationStatus.SUCCEEDED:
        return CreateOutcome.ACCEPTED
    if result.status is OperationStatus.OUTCOME_UNKNOWN:
        return CreateOutcome.UNKNOWN
    if result.error_type == "DuplicateRequest":
        return CreateOutcome.DUPLICATE
    err = result.error()
    if err is not None and (err.retryable or err.outcome_unknown):
        return CreateOutcome.UNKNOWN
    return CreateOutcome.REJECTED


# ---- reconcile hooks --------------------------------------------------------------------


def _one(items: list[Mapping[str, Any]], match: Callable[[Mapping[str, Any]], bool]):
    found = [i for i in items if match(i)]
    if len(found) > 1:
        raise ReconcileUndetermined("more than one provider object matches this operation")
    return found[0] if found else None


def find_movement(
    read: ReadFn,
    list_path: str,
    request_id: str,
    *,
    amount: Any,
    currency: str,
    account_field: str,
    account: str,
) -> Mapping[str, Any] | None:
    """The platform movement under request_id (`?request_id=` filter), checked against the
    expected amount, currency and connected account. None when absent; a mismatch or a
    second match raises ReconcileUndetermined."""
    body = read(ReadRequest(path=list_path, query={"request_id": request_id}))
    item = _one(items_of(body), lambda i: i.get("request_id") == request_id)
    if item is None:
        return None
    same = (
        provider_decimal(item.get("amount")) == amount
        and item.get("currency") == currency
        and item.get(account_field) == account
    )
    if not same:
        raise ReconcileUndetermined("object under our request_id differs from the operation")
    return item


def _movement_reconcile(list_path: str, account_field: str):
    def reconcile(op: Operation, read: ReadFn) -> Mapping[str, Any] | None:
        return find_movement(
            read,
            list_path,
            op.request_id,
            amount=op.body["amount"],
            currency=op.body["currency"],
            account_field=account_field,
            account=op.body[account_field],
        )

    return reconcile


bridge_reconcile = _movement_reconcile(TRANSFERS_PATH, "destination")
charge_reconcile = _movement_reconcile(CHARGES_PATH, "source")


def cardholder_reconcile(op: Operation, read: ReadFn) -> Mapping[str, Any] | None:
    email = op.body["email"]
    body = read(ReadRequest(path=CARDHOLDERS_PATH, query={"email": email}))
    return _one(items_of(body), lambda i: i.get("email") == email)


def card_reconcile(op: Operation, read: ReadFn) -> Mapping[str, Any] | None:
    holder, nick = op.body["cardholder_id"], op.body["nick_name"]
    body = read(ReadRequest(path=CARDS_PATH, query={"cardholder_id": holder}))
    if isinstance(body, Mapping) and body.get("has_more"):
        raise ReconcileUndetermined("card list did not fit in one page")
    return _one(
        items_of(body), lambda i: i.get("cardholder_id") == holder and i.get("nick_name") == nick
    )


def merchant_name(item: Mapping[str, Any]) -> str:
    merchant = item.get("merchant")
    name = merchant.get("name") if isinstance(merchant, Mapping) else None
    return name.rstrip("_") if isinstance(name, str) else ""


SIM_PAGE_SIZE = 100
SIM_MAX_PAGES = 20
CLOCK_SKEW = timedelta(minutes=2)
_NOT_BEFORE_MARK = "|not_before="


def _not_before(op: Operation) -> datetime:
    _, sep, stamp = op.business_key.partition(_NOT_BEFORE_MARK)
    if not sep:
        raise ReconcileUndetermined("simulation operation carries no send time")
    return datetime.fromisoformat(stamp)


def _transaction_time(item: Mapping[str, Any]) -> datetime | None:
    raw = item.get("transaction_date") or item.get("posted_date")
    if not isinstance(raw, str):
        return None
    # Sandbox rows carry milliseconds ("...:06.196+0000"); the fake's carry none ("...:06+0000").
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(raw, fmt)  # noqa: DTZ007 - every format ends in %z
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def simulation_reconcile(op: Operation, read: ReadFn) -> Mapping[str, Any]:
    """Find a card simulation by its merchant tag, amount and currency, sent no earlier than
    this operation's first send (less a small clock skew).

    The endpoint ignores request_id, so a resend makes a second authorisation. The tag carries
    the tenant's book id (M2) and an authorisation older than the send is refused, so a re-run
    after a store reset cannot adopt an earlier run's authorisation. The card's list is paged
    to its end, bounded; past the bound, or when nothing matches (the list may lag), it raises
    ReconcileUndetermined: the run stops at OUTCOME_UNKNOWN and nothing is resent."""
    tag = op.body["merchant_info"]
    amount = op.body["transaction_amount"]
    not_before = _not_before(op) - CLOCK_SKEW
    found: list[Mapping[str, Any]] = []
    for page in range(SIM_MAX_PAGES):
        body = read(
            ReadRequest(
                path=TRANSACTIONS_PATH,
                query={
                    "card_id": op.body["card_id"],
                    "page_num": str(page),
                    "page_size": str(SIM_PAGE_SIZE),
                },
            )
        )
        for i in items_of(body):
            raw = i.get("transaction_amount")
            if not (
                merchant_name(i) == tag
                and raw is not None
                and abs(provider_decimal(raw)) == amount
                and i.get("transaction_currency") == op.body["transaction_currency"]
            ):
                continue
            at = _transaction_time(i)
            if at is None:
                raise ReconcileUndetermined("matching authorisation has no readable time")
            if at >= not_before and all(
                i.get("transaction_id") != f.get("transaction_id")
                or i.get("transaction_id") is None
                for f in found
            ):
                found.append(i)  # deduped by transaction id in case pages overlap
        if not (isinstance(body, Mapping) and body.get("has_more")):
            break
    else:
        raise ReconcileUndetermined("card transaction list did not end within the page bound")
    if len(found) > 1:
        raise ReconcileUndetermined("more than one authorisation matches this operation")
    if not found:
        raise ReconcileUndetermined("simulation not found; it may still have landed, never resend")
    return found[0]


# ---- card transaction vocabulary --------------------------------------------------------


class CardTxnOutcome(StrEnum):
    CLEARED = "CLEARED"
    DECLINED = "DECLINED"
    PENDING = "PENDING"


def card_transaction_outcome(body: Mapping[str, Any]) -> tuple[CardTxnOutcome, str | None]:
    """The simulation reply has `process_result` (APPROVED/DECLINED) and `type`; a list row has
    `transaction_type` (AUTHORIZATION/CLEARING) and `status` (PENDING/FAILED/APPROVED). A
    failure_reason is always a decline."""
    reason = body.get("failure_reason")
    if reason or body.get("process_result") == "DECLINED" or body.get("status") == "FAILED":
        return CardTxnOutcome.DECLINED, str(reason) if reason else None
    kind = body.get("type") or body.get("transaction_type")
    if kind == "CLEARING" and body.get("process_result", "APPROVED") == "APPROVED":
        return CardTxnOutcome.CLEARED, None
    return CardTxnOutcome.PENDING, None
