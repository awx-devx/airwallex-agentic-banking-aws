"""Read tools. Pure: they use only `ctx.read`, always bound to the signed-in tenant, and strip
every account identifier before the model sees the result."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from awx_common import Money, ReadRequest
from awx_common.workflow import PolicyContext

from .amounts import items_of, provider_decimal
from .finality import card_transaction_outcome
from .tenants import customer_read, redact_accounts
from .validation import CARD_ID, strict

ReadFn = Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]


def _amount(value: Any, currency: str | None = None) -> str:
    """Provider amounts arrive normalised (10.0, 25, 12.3); show them at the currency's scale."""
    raw = provider_decimal(value if value is not None else 0)
    if currency:
        try:
            return format(Money.of(raw, currency).amount, "f")
        except ValueError:
            pass
    return format(raw, "f")


def _disagrees(ctx: PolicyContext) -> Mapping[str, Any] | None:
    """For reads: when the run tenant's row in the tenants table points at an account other
    than the one the setup seeded for it, answer with the fixed reason only - no figure, no id,
    no Airwallex call. An unknown tenant id still raises TenantScopeViolation."""
    from .workflow import DISAGREE, NOT_CONFIGURED, KitNotConfigured, book_for, current_setup

    try:
        setup = current_setup(ctx)
    except KitNotConfigured:
        return {"refused": NOT_CONFIGURED}  # no setup seeded yet: a stated refusal
    seeded = book_for(ctx, setup).tenants.resolve(ctx.tenant.tenant_id)
    if seeded.connected_account_id == ctx.tenant.connected_account_id:
        return None
    return {"refused": DISAGREE}


def read_tenant_summary(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    strict(raw, {}, {})
    if (refused := _disagrees(ctx)) is not None:
        return refused
    body = ctx.read(customer_read(ReadRequest(path="/api/v1/balances/current"), ctx.tenant))
    balances = [
        {
            "currency": str(b.get("currency")),
            "available": _amount(b.get("available_amount"), b.get("currency")),
            "pending": _amount(b.get("pending_amount"), b.get("currency")),
            "total": _amount(b.get("total_amount"), b.get("currency")),
        }
        for b in items_of(body)
    ]
    return redact_accounts({"balances": sorted(balances, key=lambda b: b["currency"])})


def read_cards(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    strict(raw, {}, {})
    if (refused := _disagrees(ctx)) is not None:
        return refused
    body = ctx.read(customer_read(ReadRequest(path="/api/v1/issuing/cards"), ctx.tenant))
    cards = [
        {
            "card_id": str(c.get("card_id")),
            "status": c.get("card_status"),
            "nick_name": c.get("nick_name"),
            "authorization_controls": c.get("authorization_controls"),
        }
        for c in items_of(body)
    ]
    return redact_accounts({"cards": cards})


def read_card_transactions(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    args = strict(raw, {"card_id": CARD_ID}, {})
    if (refused := _disagrees(ctx)) is not None:
        return refused
    req = ReadRequest(path="/api/v1/issuing/transactions", query={"card_id": args["card_id"]})
    body = ctx.read(customer_read(req, ctx.tenant))
    txns = []
    for t in items_of(body):
        outcome, reason = card_transaction_outcome(t)
        txns.append(
            {
                "transaction_id": t.get("transaction_id"),
                "outcome": str(outcome),
                "decline_reason": reason,
                "amount": format(abs(provider_decimal(t.get("transaction_amount") or 0)), "f"),
                "currency": t.get("transaction_currency"),
            }
        )
    return redact_accounts({"transactions": txns})


def read_bridge_position(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    """This tenant's own bridge position only: no platform capital, headroom, floor or other
    tenant's figures (they would reach the tenant caller), and no account identifiers."""
    from .book import queue_key
    from .workflow import book_for, current_setup

    strict(raw, {}, {})
    if (refused := _disagrees(ctx)) is not None:
        return refused
    book = book_for(ctx, current_setup(ctx))
    record = book.tenants.record(ctx.tenant)
    ledger = book.ledger
    tenant_holds = [h for h in ledger.holds() if h.tenant_id == record.tenant_id]
    queued = []
    for item in book.store.query(queue_key(record.tenant_id, "")):
        if not item.value.get("removed"):
            queued.append(
                {
                    "obligation_ref": item.value["obligation_id"],
                    "shortfall": str(item.value["shortfall"]),
                }
            )
    return {
        "receivable": str(ledger.receivable(record.tenant_id)),
        "bridges_in_flight": [str(h.amount) for h in tenant_holds],
        "queued_shortfalls": queued,
    }


READS: dict[str, ReadFn] = {
    "read_tenant_summary": read_tenant_summary,
    "read_cards": read_cards,
    "read_card_transactions": read_card_transactions,
    "read_bridge_position": read_bridge_position,
}

# GET paths the reader Lambda must allow for these tools (its per-kit allowlist). They are all
# tenant-bound (x-on-behalf-of); read_bridge_position reads only the platform store.
READ_ALLOWLIST: tuple[str, ...] = (
    r"^/api/v1/balances/current$",
    r"^/api/v1/issuing/cards$",
    r"^/api/v1/issuing/transactions$",
)
