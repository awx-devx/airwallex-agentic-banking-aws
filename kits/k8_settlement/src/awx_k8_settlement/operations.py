"""Operation builders, tenant binding, finality, reconcile and outcome mapping.

connected_account_transfers/create (payout) and charges/create (recovery) are platform calls
that name the seller in a body field (awx_common.tenancy.PLATFORM_BODY_FIELDS) and must never
send x-on-behalf-of: sandbox evidence shows the header on a connected account transfer makes the
named account the PAYER (FAKE-FIDELITY.md row 105). Every op is bound with
awx_common.bind_platform_operation and checked with check_tenant_scope before it leaves the kit.

Statuses for both objects (FAKE-FIDELITY.md row 109, API reference): NEW, PENDING, SETTLED,
SUSPENDED, FAILED. SETTLED is success, FAILED is failure, SUSPENDED needs a person. CANCELLED is
not documented for these objects; it is kept as a failure only as a defensive mapping.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from awx_common import (
    PLATFORM_BODY_FIELDS,
    Money,
    Operation,
    OperationResult,
    OperationStatus,
    ReadRequest,
    TenantContext,
    bind_platform_operation,
    bind_read,
    check_tenant_scope,
    derive_request_id,
)
from awx_common.errors import AwxError, TenantScopeViolation, ValidationFailed
from awx_common.ops import DEDUP_RESEND_MARGIN
from awx_common.workflow import FinalitySpec, ReconcileUndetermined

from .ledger import CycleSettlement

KIT = "k8"
TRANSFER_CREATE = "/api/v1/connected_account_transfers/create"
CHARGE_CREATE = "/api/v1/charges/create"
TRANSFERS = "/api/v1/connected_account_transfers"
CHARGES = "/api/v1/charges"
BALANCES = "/api/v1/balances/current"
# FAKE-FIDELITY.md row 106: 22 accepted values; goods_purchased verified live on BOTH endpoints
# and describes marketplace seller proceeds and their refund recovery. settlement and commission
# pass the first validator and then fail.
PAYOUT_REASON = "goods_purchased"
RECOVERY_REASON = "goods_purchased"
LABEL = "poc-i8-"  # label on every kit 8 sandbox object; code finds objects by it
# Both platform endpoints deduplicate on request_id (FAKE-FIDELITY "request_id idempotency");
# the guide gives about 7 days. Re-emit only inside the spine's own resend margin, then
# route to a person. One constant for the kit and the executor, so they never disagree.
DEDUP_WINDOW = DEDUP_RESEND_MARGIN
DEFINITIVE_FAILURES = frozenset({"FAILED", "NOT_CREATED", "INSUFFICIENT_FUNDS"})
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

SETTLED = "SETTLED"
SUSPENDED = "SUSPENDED"
FAILURE_STATUSES = frozenset({"FAILED", "CANCELLED"})  # CANCELLED: defensive, undocumented here
TERMINAL = frozenset({SETTLED, SUSPENDED}) | FAILURE_STATUSES  # SUSPENDED stops the poll


# Kit rule M2: every key built from kit-local values (seller and cycle ids, attempt counters)
# carries the seller book's id, minted once when the book is first created (store.seed_seller),
# so a fresh book seeded the same way can never adopt an earlier run's payouts or charges.


def _book(book_id: str) -> str:
    if not book_id or not _PROVIDER_ID.match(book_id):
        raise ValueError("a minted book id is required for every request_id (rule M2)")
    return book_id


def payout_business_key(book_id: str, seller: str, cycle_id: str) -> str:
    # names the payout object: this book, this seller, this cycle. A payout that failed
    # definitively is re-paid inside a LATER cycle's payout, so it gets that cycle's key.
    return f"payout:{_book(book_id)}:{seller}:{cycle_id}"


def recovery_business_key(book_id: str, seller: str, attempt: int) -> str:
    # one recovery attempt; attempt+1 exists only after a definitive outcome
    return f"recovery:{_book(book_id)}:{seller}:{attempt}"


def payout_request_id(book_id: str, seller: str, cycle_id: str) -> str:
    return derive_request_id(KIT, payout_business_key(book_id, seller, cycle_id), "payout")


def recovery_request_id(book_id: str, seller: str, attempt: int) -> str:
    return derive_request_id(KIT, recovery_business_key(book_id, seller, attempt), "charge")


def _amount(m: Money) -> Decimal:
    # Guide and sandbox: amounts are major units. Money already sits at minor-unit scale.
    if not m.amount > 0:
        raise ValueError("a money movement must be positive")
    return m.amount


def _bound(op: Operation, tenant: TenantContext) -> Operation:
    out = bind_platform_operation(op, tenant)
    check_tenant_scope(out, tenant)  # the same deny-by-default check every runner applies
    return out


def build_payout_op(
    settlement: CycleSettlement, tenant: TenantContext, *, book_id: str, intent_hash: str
) -> Operation:
    if settlement.seller != tenant.tenant_id:
        raise TenantScopeViolation("settlement belongs to a different seller")
    rid = payout_request_id(book_id, settlement.seller, settlement.cycle_id)
    op = Operation(
        kit=KIT,
        name="connected_account_transfers.create",
        method="POST",
        path=TRANSFER_CREATE,
        body={
            "request_id": rid,
            "amount": _amount(settlement.payout),
            "currency": settlement.currency,
            "reason": PAYOUT_REASON,
            "reference": f"{LABEL}payout {settlement.cycle_id}"[:64],
        },
        request_id=rid,
        business_key=payout_business_key(book_id, settlement.seller, settlement.cycle_id),
        intent_hash=intent_hash,
    )
    return _bound(op, tenant)


def build_recovery_op(
    attempt: int, amount: Money, tenant: TenantContext, *, book_id: str, intent_hash: str
) -> Operation:
    rid = recovery_request_id(book_id, tenant.tenant_id, attempt)
    op = Operation(
        kit=KIT,
        name="charges.create",
        method="POST",
        path=CHARGE_CREATE,
        body={
            "request_id": rid,
            "amount": _amount(amount),
            "currency": amount.currency,
            "reason": RECOVERY_REASON,
            "reference": f"{LABEL}refund shortfall recovery {attempt}",
        },
        request_id=rid,
        business_key=recovery_business_key(book_id, tenant.tenant_id, attempt),
        intent_hash=intent_hash,
    )
    return _bound(op, tenant)


# Reading objects back ---------------------------------------------------------------------------


def status_of(body: Mapping[str, Any]) -> str:
    status = body.get("status")
    return status if isinstance(status, str) else "UNKNOWN"


def _collection(op: Operation) -> str:
    if op.path == TRANSFER_CREATE:
        return TRANSFERS
    if op.path == CHARGE_CREATE:
        return CHARGES
    raise ValueError(f"no read-back for {op.path}")


def _by_id(collection: str) -> Callable[[OperationResult], ReadRequest]:
    def read_request(result: OperationResult) -> ReadRequest:
        if not result.provider_id or not _PROVIDER_ID.match(result.provider_id):
            raise ValidationFailed("create result has no usable provider id to poll")
        return ReadRequest(path=f"{collection}/{result.provider_id}")

    return read_request


def matches(op: Operation, body: Mapping[str, Any]) -> bool:
    """An object found by request_id is ours only if amount, currency and party agree."""
    field = PLATFORM_BODY_FIELDS[op.path]
    try:
        amount = Decimal(str(body.get("amount")))
    except ArithmeticError:
        return False
    return (
        body.get("request_id") == op.request_id
        and amount == Decimal(op.body["amount"])
        and body.get("currency") == op.body["currency"]
        and body.get(field) == op.body[field]
    )


def find_by_request_id(
    op: Operation, read: Callable[[ReadRequest], Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """StepSpec.reconcile: GET /<collection>?request_id= (FAKE-FIDELITY rows 51-52).

    None when the list is cleanly empty. A mismatched object or an unreadable reply raises
    ReconcileUndetermined, so the executor never resends and nothing is posted on a guess.
    """
    body = read(ReadRequest(path=_collection(op), query={"request_id": op.request_id}))
    items = body.get("items") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        raise ReconcileUndetermined("read-back reply has no items list")
    found = [i for i in items if isinstance(i, Mapping) and i.get("request_id") == op.request_id]
    if not found:
        return None
    if len(found) > 1 or not matches(op, found[0]):
        raise ReconcileUndetermined("object found by request_id does not match the operation")
    return found[0]


PAYOUT_FINALITY = FinalitySpec(
    read_request=_by_id(TRANSFERS),
    status_of=status_of,
    terminal=TERMINAL,
    success=frozenset({SETTLED}),
)
CHARGE_FINALITY = FinalitySpec(
    read_request=_by_id(CHARGES),
    status_of=status_of,
    terminal=TERMINAL,
    success=frozenset({SETTLED}),
)
# Seller-scoped reads (x-on-behalf-of via bind_read).
TENANT_READS = (r"^/api/v1/balances/current$",)
# Platform reads (no header): finality, reconcile and postcondition read-backs (rule 5).
PLATFORM_READS = (
    r"^/api/v1/connected_account_transfers/[A-Za-z0-9_-]{1,80}$",
    r"^/api/v1/charges/[A-Za-z0-9_-]{1,80}$",
    r"^/api/v1/connected_account_transfers$",
    r"^/api/v1/charges$",
)


class Outcome(StrEnum):
    SETTLED = "SETTLED"
    FAILED = "FAILED"  # the object exists and failed: the money did not move
    NOT_CREATED = "NOT_CREATED"  # rejected at create (FAILED, not a duplicate): nothing exists
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # rejected at create for funds: nothing exists
    NEEDS_PERSON = "NEEDS_PERSON"  # SUSPENDED: hold, escalate, post nothing
    IN_FLIGHT = "IN_FLIGHT"  # NEW, PENDING, unknown status: post nothing
    UNKNOWN = "UNKNOWN"  # cannot tell whether it landed (incl. an empty read-back): in flight


def read_outcome(body: Mapping[str, Any]) -> Outcome:
    status = status_of(body)
    if status == SETTLED:
        return Outcome.SETTLED
    if status == SUSPENDED:
        return Outcome.NEEDS_PERSON
    if status == "FAILED":
        return Outcome.FAILED
    if status == "CANCELLED":  # undocumented here; definitive only with a failure block
        has_failure = isinstance(body.get("failure"), Mapping) or bool(body.get("failure_reasons"))
        return Outcome.FAILED if has_failure else Outcome.UNKNOWN
    return Outcome.IN_FLIGHT


def resolve(
    op: Operation,
    result: OperationResult | None,
    read_platform: Callable[[ReadRequest], Mapping[str, Any]] | None,
) -> tuple[Outcome, Mapping[str, Any] | None]:
    """Decide what really happened to op, reading the provider rather than trusting a reply.

    Kit rule M1: only a definitive outcome moves money. Definitive failures are a FAILED create
    (other than DuplicateRequest), which created nothing, and the provider's own failure state.
    An empty ?request_id= read-back, a missing result, an unreadable or mismatched object are
    all UNKNOWN: the money stays in flight on its request_id.
    """
    if result is not None and result.status is OperationStatus.FAILED:
        err = result.error()
        if err is not None and err.retryable:
            # A retryable FAILED (429, 5xx before acceptance) as the LAST reply only says the
            # resends were refused; the first send may have landed invisibly. Never definitive,
            # in any mode: read back, else keep it in flight.
            result = replace(result, status=OperationStatus.OUTCOME_UNKNOWN, provider_id=None)
    if result is not None and result.status is OperationStatus.FAILED:
        refused = (
            Outcome.INSUFFICIENT_FUNDS
            if result.error_type == "InsufficientFunds"
            else Outcome.NOT_CREATED
        )
        if result.error_type != "DuplicateRequest":
            # The executor may have resent after an unknown first reply, so a FAILED create
            # does not prove an earlier copy is absent. Look it up before calling it final.
            if read_platform is None:
                return refused, None
            try:
                found = find_by_request_id(op, read_platform)
            except (ReconcileUndetermined, AwxError):  # an unreadable lookup is not definitive
                return Outcome.UNKNOWN, None
            return (refused, None) if found is None else (read_outcome(found), found)
    if read_platform is None:
        return Outcome.UNKNOWN, None
    try:
        if result is not None and result.provider_id:
            body = read_platform(_by_id(_collection(op))(result))
            if not matches(op, body):
                return Outcome.UNKNOWN, None
        else:
            body = find_by_request_id(op, read_platform)
    except (ReconcileUndetermined, AwxError):
        # Not found by id (a landed object can stay invisible for a while), a 429 or a 5xx:
        # the read proves nothing, so the money stays in flight (M1).
        return Outcome.UNKNOWN, None
    if body is None:
        return Outcome.UNKNOWN, None  # M1: an empty read-back is not a failure
    return read_outcome(body), body


# Final OUTCOME_UNKNOWN codes (rule 9): a resend cannot change them, a person decides.
PERSON_CODES = frozenset(
    {
        "request_id_reused",
        "request_id_bound_to_another_body",
        "multiple_objects",
        "first_attempt_unknown",
        "past_dedupe_margin",
    }
)


def person_reason(
    op: Operation,
    result: OperationResult | None,
    read_platform: Callable[[ReadRequest], Mapping[str, Any]] | None,
) -> str | None:
    """Why money still in flight needs a person now, or None when
    waiting or a resend can still settle it. A final unknown code, or a read-back that finds
    something it cannot match (two objects, a mismatch, an unreadable reply)."""
    if result is not None and result.error_code in PERSON_CODES:
        return f"Airwallex answered {result.error_code} for request_id {op.request_id}"
    if read_platform is None:
        return None
    try:
        find_by_request_id(op, read_platform)
    except ReconcileUndetermined as exc:
        return f"the read-back of request_id {op.request_id} is undetermined ({exc})"
    except AwxError:
        return None  # transient: the next read decides
    return None


def resend_outcome(outcome: Outcome) -> Outcome:
    """On a resend, a refusal at create is NOT definitive: the first send of the same
    request_id may still be on its way, and it would land after the refusal. Only the
    provider's own failure state, or SETTLED, is definitive then (M1)."""
    if outcome in (Outcome.NOT_CREATED, Outcome.INSUFFICIENT_FUNDS):
        return Outcome.UNKNOWN
    return outcome


def within_dedup_window(first_sent_at: datetime, now: datetime) -> bool:
    """True while re-emitting the same request_id is still deduplicated by the provider."""
    return now - first_sent_at <= DEDUP_WINDOW


# Balances ---------------------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool | float):
        raise TypeError("balance amount arrived as a float; the reader must parse as Decimal")
    if isinstance(value, Decimal | int | str):
        return Decimal(value)
    raise TypeError("balance amount has an unexpected type")


def available_balance(body: Mapping[str, Any], currency: str) -> Money:
    """GET /balances/current comes back as {"items": [...]}. Missing row means zero."""
    rows = body.get("items") if isinstance(body, Mapping) else None
    if not isinstance(rows, list):
        raise ValidationFailed("balances response has no items list")
    for row in rows:
        if isinstance(row, Mapping) and row.get("currency") == currency:
            return Money.of(_decimal(row.get("available_amount", 0)), currency)
    return Money.zero(currency)


def seller_balance_request(tenant: TenantContext) -> ReadRequest:
    """A customer-scoped read: this one DOES carry x-on-behalf-of, via bind_read."""
    if not tenant.connected_account_id:
        raise TenantScopeViolation("seller tenant has no connected account")
    return bind_read(ReadRequest(path=BALANCES), tenant)
