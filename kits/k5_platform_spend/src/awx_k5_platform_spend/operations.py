"""Operation builders. Every builder returns an Operation already bound to the tenant.

Request IDs are derived from a business key naming the object being moved or created,
never from the intent or the amounts: the same obligation, fee period, advance, cardholder,
card or scenario always gets the same request_id, so a second proposal for it is refused by
Airwallex and by the operation store. Only a confirmed FAILED bridge moves to a new step
(`replacement-<n>`).

Customer-scoped calls go through `bind_customer_operation` (x-on-behalf-of); platform movements
through `bind_platform_movement` (body field, no header). Bodies follow awx_spine
FAKE-FIDELITY.md and are sent as built, so endpoints that ignore request_id
(cardholder create, card simulation) get none.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from awx_common import Money, Operation, TenantContext, derive_request_id

from .config import (
    BRIDGE_REASON,
    FEE_REASON,
    KIT,
    REFERENCE_PREFIX,
    REPAYMENT_REASON,
    CardProgram,
)
from .tenants import TenantMap, bind_customer_operation, bind_platform_movement

MERCHANT_NAME_LEN = 25  # the sandbox pads (and so truncates) merchant names to 25 characters
SANDBOX_EMAIL_DOMAIN = "example.com"


NOT_BEFORE_MARK = "|not_before="


def business_key(tenant: TenantContext, book_id: str, kind: str, ref: str) -> str:
    """M2: every key built from a kit-local reference carries the book id minted with the
    kit's state, so a fresh book seeded like an old one can never adopt the old objects."""
    if not book_id:
        raise ValueError("book_id is required (M2)")
    return f"{tenant.tenant_id}:{book_id}:{kind}:{ref}"


def attempt_step(failures: int) -> str:
    return "create" if failures == 0 else f"replacement-{failures}"


def request_id_for(
    tenant: TenantContext, book_id: str, kind: str, ref: str, step: str = "create"
) -> str:
    return derive_request_id(KIT, business_key(tenant, book_id, kind, ref), step)


def _tag(bk: str, length: int) -> str:
    return hashlib.sha256(bk.encode("utf-8")).hexdigest()[:length]


def _reference(kind: str, ref: str) -> str:
    return f"{REFERENCE_PREFIX}{kind}-{ref}"[:64]


def cardholder_email(tenant: TenantContext, book_id: str, cardholder_ref: str) -> str:
    """Server-derived and deterministic. Cardholder create ignores request_id but refuses a
    second cardholder with the same email, so a fixed email is what makes it idempotent, and
    the generic read-back finds it by email. No model-supplied personal data."""
    tag = _tag(business_key(tenant, book_id, "cardholder", cardholder_ref), 16)
    return f"{REFERENCE_PREFIX}{tag}@{SANDBOX_EMAIL_DOMAIN}"


def merchant_tag(tenant: TenantContext, book_id: str, scenario_ref: str) -> str:
    """The deterministic merchant name a simulation carries, so reconcile can find it. Hex only
    (no underscores, which the sandbox uses as padding) and within the 25-character field."""
    tag = f"{REFERENCE_PREFIX}{_tag(business_key(tenant, book_id, 'sim', scenario_ref), 16)}"
    if len(tag) > MERCHANT_NAME_LEN:
        raise ValueError("merchant tag longer than the sandbox keeps")
    return tag


def card_nick_name(cardholder_ref: str) -> str:
    return _reference("card", cardholder_ref)


def bridge_operation(
    tenant: TenantContext,
    tenants: TenantMap,
    *,
    book_id: str,
    obligation_id: str,
    amount: Money,
    step: str,
    intent_hash: str,
) -> Operation:
    """POST /connected_account_transfers/create: platform wallet -> customer wallet."""
    if amount.amount <= 0:
        raise ValueError("bridge amount must be positive")
    bk = business_key(tenant, book_id, "bridge", obligation_id)
    rid = derive_request_id(KIT, bk, step)
    op = Operation(
        kit=KIT,
        name="connected_account_transfers.create",
        method="POST",
        path="/api/v1/connected_account_transfers/create",
        body={
            "request_id": rid,
            "amount": amount.amount,
            "currency": amount.currency,
            "reason": BRIDGE_REASON,
            "reference": _reference("bridge", obligation_id),
        },
        request_id=rid,
        business_key=bk,
        intent_hash=intent_hash,
    )
    return bind_platform_movement(op, tenant, tenants)


def charge_operation(
    tenant: TenantContext,
    tenants: TenantMap,
    *,
    book_id: str,
    kind: str,
    ref: str,
    amount: Money,
    step: str = "create",
    intent_hash: str,
) -> Operation:
    """POST /charges/create: customer wallet -> platform (fee or bridge repayment)."""
    reasons = {"fee": FEE_REASON, "repayment": REPAYMENT_REASON}
    if kind not in reasons:
        raise ValueError("charge kind is fee or repayment")
    if amount.amount <= 0:
        raise ValueError("charge amount must be positive")
    bk = business_key(tenant, book_id, kind, ref)
    rid = derive_request_id(KIT, bk, step)
    op = Operation(
        kit=KIT,
        name="charges.create",
        method="POST",
        path="/api/v1/charges/create",
        body={
            "request_id": rid,
            "amount": amount.amount,
            "currency": amount.currency,
            "reason": reasons[kind],
            "reference": _reference(kind, ref),
        },
        request_id=rid,
        business_key=bk,
        intent_hash=intent_hash,
    )
    return bind_platform_movement(op, tenant, tenants)


def cardholder_operation(
    tenant: TenantContext,
    tenants: TenantMap,
    *,
    book_id: str,
    cardholder_ref: str,
    intent_hash: str,
) -> Operation:
    """DELEGATE cardholders are READY at once (INDIVIDUAL is PENDING for about 2 s)."""
    bk = business_key(tenant, book_id, "cardholder", cardholder_ref)
    op = Operation(
        kit=KIT,
        name="issuing.cardholders.create",
        method="POST",
        path="/api/v1/issuing/cardholders/create",
        body={"type": "DELEGATE", "email": cardholder_email(tenant, book_id, cardholder_ref)},
        request_id=derive_request_id(KIT, bk, "create"),
        business_key=bk,
        intent_hash=intent_hash,
    )
    return bind_customer_operation(op, tenant, tenants)


def card_operation(
    tenant: TenantContext,
    tenants: TenantMap,
    *,
    book_id: str,
    cardholder_ref: str,
    cardholder_id: str,
    program: CardProgram,
    intent_hash: str,
) -> Operation:
    """A virtual card whose limits come only from the server-side program.

    PER_TRANSACTION is not cumulative in Airwallex, so MONTHLY rides alongside it."""
    bk = business_key(tenant, book_id, "card", cardholder_ref)
    rid = derive_request_id(KIT, bk, "create")
    controls: dict[str, object] = {
        "allowed_transaction_count": "MULTIPLE",
        "allowed_currencies": list(program.allowed_currencies),
        "transaction_limits": {
            "currency": program.currency,
            "limits": [
                {"amount": program.per_transaction_limit.amount, "interval": "PER_TRANSACTION"},
                {"amount": program.monthly_limit.amount, "interval": "MONTHLY"},
            ],
        },
    }
    if program.allowed_merchant_categories:
        controls["allowed_merchant_categories"] = list(program.allowed_merchant_categories)
    op = Operation(
        kit=KIT,
        name="issuing.cards.create",
        method="POST",
        path="/api/v1/issuing/cards/create",
        body={
            "request_id": rid,
            "cardholder_id": cardholder_id,
            "created_by": f"{REFERENCE_PREFIX}agent",
            "nick_name": card_nick_name(cardholder_ref),
            "form_factor": "VIRTUAL",
            "is_personalized": False,
            "program": {"purpose": "COMMERCIAL"},
            "authorization_controls": controls,
            "metadata": {"poc": card_nick_name(cardholder_ref)},
        },
        request_id=rid,
        business_key=bk,
        intent_hash=intent_hash,
    )
    return bind_customer_operation(op, tenant, tenants)


def card_simulation_operation(
    tenant: TenantContext,
    tenants: TenantMap,
    *,
    book_id: str,
    scenario_ref: str,
    card_id: str,
    amount: Money,
    merchant_category_code: str,
    not_before: datetime,
    intent_hash: str,
) -> Operation:
    """POST /simulation/issuing/create, always with x-on-behalf-of: without it a
    connected-account card returns 400 yet the authorisation is created and funds reserved.

    `single_phase: true` gives an approved CLEARING in one call (capture via the lifecycle path
    needs an admin key). The endpoint ignores request_id, so the body carries none.
    `not_before` is the first send time, kept in the tenant's registry so a re-run reuses it;
    reconcile refuses any authorisation older than it."""
    bk = business_key(tenant, book_id, "sim", scenario_ref)
    if not_before.tzinfo is None:
        raise ValueError("not_before must be timezone-aware")
    op = Operation(
        kit=KIT,
        name="issuing.simulate_authorization",
        method="POST",
        path="/api/v1/simulation/issuing/create",
        body={
            "card_id": card_id,
            "transaction_amount": amount.amount,
            "transaction_currency": amount.currency,
            "merchant_category_code": merchant_category_code,
            "merchant_info": merchant_tag(tenant, book_id, scenario_ref),
            "single_phase": True,
        },
        request_id=derive_request_id(KIT, bk, "create"),
        # Not sent to Airwallex; reconcile reads the send time from it.
        business_key=f"{bk}{NOT_BEFORE_MARK}{not_before.astimezone(UTC).isoformat()}",
        intent_hash=intent_hash,
    )
    return bind_customer_operation(op, tenant, tenants)
