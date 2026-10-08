"""Kit 8 read tools. ctx.read for the seller's wallet; ctx.store (this seller's KitStore only)
for the ledger and carrier evidence, which are kit state, not Airwallex objects."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from awx_common.errors import TenantScopeViolation, ValidationFailed
from awx_common.workflow import PolicyContext

from . import store as st
from .common import account_problem
from .evidence import incident_exposure, model_view, parse_carrier_event
from .operations import available_balance, seller_balance_request

_CCY = re.compile(r"^[A-Z]{3}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

Read = Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]


def _only(raw: Mapping[str, Any], allowed: set[str]) -> None:
    if set(raw) - allowed:
        raise ValidationFailed("unexpected fields")


def _guard(ctx: PolicyContext) -> None:
    """Account binding for reads: before any figure or Airwallex call, the tenants row must match
    the seeded account. The answer is the fixed reason, with no figure and no account id."""
    if (problem := account_problem(ctx)) is not None:
        raise TenantScopeViolation(problem)


def read_seller_balance(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    _guard(ctx)
    _only(raw, {"currency"})
    currency = raw.get("currency")
    if not isinstance(currency, str) or not _CCY.match(currency):
        raise ValidationFailed("currency is missing or malformed")
    body = ctx.read(seller_balance_request(ctx.tenant))
    return {"currency": currency, "available": available_balance(body, currency)}


def read_seller_ledger(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    _guard(ctx)
    _only(raw, set())
    kstore = st.kit_store(ctx)
    state = st.ledger_state(kstore, ctx.tenant.tenant_id, st.profile(kstore).currency)
    record = state.ledger.to_record()
    record.pop("seller")
    return {
        **record,
        "identity_residual": state.ledger.residual(),
        "platform_share": state.ledger.platform_share(),
        "unresolved_payout_cycles": list(st.unresolved_payouts(state)),
        "needs_person": [dict(x) for x in st.needs_person(kstore)],
        "recovery_attempts": [
            {"attempt": a.attempt, "amount": a.amount, "outcome": a.outcome}
            for a in st.recovery_attempts(kstore)
        ],
    }


def read_carrier_event(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    _guard(ctx)
    _only(raw, {"carrier_event_id"})
    event_id = raw.get("carrier_event_id")
    if not isinstance(event_id, str) or not _ID.match(event_id):
        raise ValidationFailed("carrier_event_id is missing or malformed")
    kstore = st.kit_store(ctx)
    seller = ctx.tenant.tenant_id
    event = parse_carrier_event(st.carrier_event(kstore, event_id))
    exposure = incident_exposure(
        event, st.orders(kstore, seller), seller=seller, currency=st.profile(kstore).currency
    )  # raises TenantScopeViolation if the event names another seller
    return model_view(event, exposure)


READS: dict[str, Read] = {
    "read_seller_balance": read_seller_balance,
    "read_seller_ledger": read_seller_ledger,
    "read_carrier_event": read_carrier_event,
}
