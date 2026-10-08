"""Business keys, request_ids, Operation builders and outcome rules shared by kit 1's actions.

request_ids derive from the business object and the book id (kit rule M2): a transfer from
the book id, the obligation and the lock's attempt, a conversion from the book id, the
currency and the obligations it funds with their attempts. The obligation's book
version is not part of either, so a version bump never mints a new request_id by itself; a
fresh book (a new random book id) always does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from awx_common import Operation, OperationResult, OperationStatus, derive_request_id
from awx_common.workflow import StepContext

from .api import FX_CONVERSIONS_CREATE
from .money_rules import K1Error

KIT = "k1"
TRANSFER_REASON = "payment_to_supplier"  # accepted on transfers/create (FAKE-FIDELITY row 75)


class MissingBookId(K1Error):
    """A book with no `book/id` (M2 forbids a default). Policy blocks it before this."""


def book_id_of(computed: Mapping[str, Any]) -> str:
    """The approved book id. Never str(None): a missing id raises."""
    value = computed.get("book_id")
    if not isinstance(value, str) or not value:
        raise MissingBookId("computed carries no book id")
    return value


def obligation_key(book_id: str, obligation_id: str) -> str:
    if not book_id:
        raise MissingBookId("empty book id")
    return f"poc-k1-{book_id}-ob-{obligation_id}"


def transfer_request_id(book_id: str, obligation_id: str, attempt: int) -> str:
    return derive_request_id(KIT, obligation_key(book_id, obligation_id), f"transfer:a{attempt}")


def conversion_key(book_id: str, currency: str, funds: Sequence[tuple[str, int]]) -> str:
    if not book_id:
        raise MissingBookId("empty book id")
    parts = "+".join(f"{oid}.a{att}" for oid, att in sorted(funds))
    return f"poc-k1-{book_id}-conv-{currency}-{parts}"


def conversion_request_id(
    computed: Mapping[str, Any], conv: Mapping[str, Any], attempts: Mapping[str, int]
) -> str:
    funds = [(oid, attempts[oid]) for oid in conv["funds"]]
    key = conversion_key(book_id_of(computed), conv["currency"], funds)
    return derive_request_id(KIT, key, "conversion")


def op(
    ctx: StepContext,
    name: str,
    path: str,
    body: Mapping[str, Any],
    request_id: str,
    business: str,
) -> Operation:
    return Operation(
        kit=KIT,
        name=name,
        method="POST",
        path=path,
        body=body,
        request_id=request_id,
        business_key=business,
        intent_hash=ctx.intent_hash,
    )


def conversion_body(conv: Mapping[str, Any], rid: str) -> dict[str, Any]:
    """The conversion booked under the rate-locked quote (exact buy, quote fixes the sell)."""
    return {
        "request_id": rid,
        "quote_id": conv["quote_id"],
        "buy_currency": conv["buy"].currency,
        "sell_currency": conv["sell"].currency,
        "buy_amount": conv["buy"].amount,
    }


def conversion_op(ctx: StepContext, conv: Mapping[str, Any], rid: str) -> Operation:
    return op(
        ctx,
        "fx.conversions.create",
        FX_CONVERSIONS_CREATE,
        conversion_body(conv, rid),
        rid,
        conv["currency"],
    )


def transfer_reference(book_id: str, obligation_id: str) -> str:
    """`poc-i1-<book8>-<oid>`, where book8 is the first 8 hex of the random book id. Every
    sandbox object kit 1 creates carries a `poc-i1-` label, and the book part keeps the
    release's DUAL cross-check from matching another book's transfers."""
    if not book_id:
        raise MissingBookId("empty book id")
    return f"poc-i1-{book_id[:8]}-{obligation_id}"[:35]


def transfer_body(p: Mapping[str, Any], rid: str, book_id: str) -> dict[str, Any]:
    return {
        "request_id": rid,
        "beneficiary_id": p["beneficiary_id"],
        "transfer_currency": p["amount"].currency,
        "transfer_amount": p["amount"].amount,
        "source_currency": p["amount"].currency,
        "transfer_method": p["method"],
        "reference": transfer_reference(book_id, p["obligation_id"]),
        "reason": TRANSFER_REASON,
    }


# A FAILED result with one of these types is a definitive refusal at create: nothing was
# created and the request_id was not consumed (FAKE-FIDELITY "Rejected requests (4xx)"). The
# engine's own StateConflict codes are NOT refusals: request_id_reused means an object holds
# our request_id with other fields, provider_failure_state means a reconciled object is in a
# terminal failure state (judged from the provider read instead).
_REFUSED_NOT_LANDED = frozenset(
    {"ValidationFailed", "InsufficientFunds", "LimitExceeded", "NotFound"}
)
_ENGINE_NOT_REFUSALS = frozenset({"request_id_reused", "provider_failure_state"})


def landed_or_unknown(result: OperationResult) -> bool:
    """False only for a definitive refusal at create. OUTCOME_UNKNOWN (including a refused
    resend, body["refused"], and multiple_objects) is money in flight (M1)."""
    if result.status is OperationStatus.SUCCEEDED:
        return True
    if result.status is not OperationStatus.FAILED:
        return True
    if result.error_type == "StateConflict":
        return result.error_code in _ENGINE_NOT_REFUSALS or result.error_code is None
    return result.error_type not in _REFUSED_NOT_LANDED
