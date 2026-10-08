"""Airwallex request and response shapes kit 3 uses. Pure: builds ReadRequests and Operations,
parses reads and reconciles. Nothing here sends anything.

Endpoints (the hackathon guide's kit 3): POST /api/v1/transfers/create, GET /api/v1/transfers,
GET /api/v1/transfers/{id}, POST /api/v1/simulation/transfers/{id}/transition, and
GET /api/v1/balances/current for the funding check. Shapes follow the sandbox evidence in
awx_spine's FAKE-FIDELITY.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from awx_common import Money, Operation, ReadRequest, derive_request_id
from awx_common.workflow import ReconcileUndetermined

from .ledger import MAX_REPLACEMENT_ATTEMPTS

KIT = "k3"
POC_LABEL = "poc-i3-"  # prefix on every sandbox object kit 3 creates; code finds them by it
# awx_common.sandbox.make_transfer_advance never pushes a transfer whose reference carries this
# marker past SENT, so a staged original stays in flight until the
# failure simulation acts on it. A replacement never carries it, so it can settle.
NOSETTLE_MARKER = "-nosettle"
ORIGINAL_STEP = "original"
# transfers/create does not validate `reason` (FAKE-FIDELITY "reason is not validated");
# goods_purchased is also on the documented list for platform money movement.
SUPPLIER_REASON = "goods_purchased"
# Budget for one SWIFT payout when the original's own fee_amount cannot be used. The guide says
# a flat EUR 12.85; the sandbox charged EUR 6.21 on 1.00 and 100.00 (FAKE-FIDELITY SWIFT fee
# FINDING). The larger figure is kept so a stale fee never under-funds a payment.
SWIFT_FEE_FALLBACK: dict[str, Money] = {"EUR": Money.of("12.85", "EUR")}
# Sandbox transitions the state machine allows (FAKE-FIDELITY "Allowed:" row).
TRANSITION_OP = "transfers.simulate_transition"  # awx_spine endpoint table name
SIM_SENT_FROM = frozenset({"SCHEDULED", "PROCESSING"})

READ_ALLOWLIST: tuple[str, ...] = (
    r"^/api/v1/transfers$",
    r"^/api/v1/transfers/[A-Za-z0-9_-]{1,64}$",
    r"^/api/v1/balances/current$",
)
# The operator staging path also reads the beneficiary it pays (never the agent's reader).
STAGING_READ_ALLOWLIST: tuple[str, ...] = (
    *READ_ALLOWLIST,
    r"^/api/v1/beneficiaries/[A-Za-z0-9_-]{1,64}$",
)
# Staging guardrails: only kit 3's own demo beneficiaries, and only demo amounts.
STAGING_BENEFICIARY_PREFIX = POC_LABEL
STAGING_MAX_AMOUNT = Decimal("10.00")
STAGING_CURRENCY = "USD"  # decided: staging pays USD only, so the cap means 10.00 USD

Reader = Callable[[ReadRequest], Mapping[str, Any]]


class UnreadableAmount(ValueError):
    """An amount in a read is missing, a float or not a number. Never guess one."""


def amount_of(value: Any, currency: Any) -> Money:
    """Exact Money from a provider amount. Floats are refused: they lost precision upstream."""
    if isinstance(value, bool | float) or not isinstance(value, Decimal | int | str):
        raise UnreadableAmount(f"amount {value!r} is not an exact number")
    try:
        return Money.of(value if isinstance(value, Decimal) else Decimal(value), currency)
    except Exception as exc:
        raise UnreadableAmount(f"amount {value!r} {currency!r}: {exc}") from exc


# ---- reads -------------------------------------------------------------------------------


def transfer_read(transfer_id: str) -> ReadRequest:
    return ReadRequest(path=f"/api/v1/transfers/{transfer_id}")


def get_transfer(read: Reader, transfer_id: str) -> Mapping[str, Any]:
    return read(transfer_read(transfer_id))


def _items(body: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """A list endpoint's rows. The port always returns a Mapping, so the rows sit under `items`."""
    rows = body.get("items") or []
    return [r for r in rows if isinstance(r, Mapping)]


def find_transfers_by_request_id(read: Reader, request_id: str) -> list[Mapping[str, Any]]:
    """Transfers carrying this request_id. Filters client-side too, in case the provider ignores
    the query parameter."""
    body = read(ReadRequest(path="/api/v1/transfers", query={"request_id": request_id}))
    return [t for t in _items(body) if t.get("request_id") == request_id]


def available_balance(read: Reader, currency: str) -> Money:
    body = read(ReadRequest(path="/api/v1/balances/current"))
    for row in _items(body):
        if row.get("currency") == currency:
            return amount_of(row.get("available_amount"), currency)
    return Money.zero(currency)


# ---- request ids -------------------------------------------------------------------------


def replacement_request_id(original_transfer_id: str, attempt: int = 1) -> str:
    """The request_id of replacement attempt `attempt` (1 to 3) for one original transfer.

    The business key names the original transfer (the obligation being re-paid), not the
    incident, so two incidents (or one incident delivered twice) about the same transfer
    collapse onto the same request_id and the operation store, then Airwallex, refuse the
    second write (request ids derive from business objects). It is an Airwallex object id, so
    M2's book id is not needed and would weaken the guard (see ledger.py). Attempt n+1 exists
    only after attempt n was definitively refused (M1).
    """
    if not 1 <= attempt <= MAX_REPLACEMENT_ATTEMPTS:
        raise ValueError("replacement attempt out of range")
    return derive_request_id(KIT, f"transfer:{original_transfer_id}", f"replacement-{attempt}")


def all_replacement_request_ids(original_transfer_id: str) -> tuple[str, ...]:
    return tuple(
        replacement_request_id(original_transfer_id, n)
        for n in range(1, MAX_REPLACEMENT_ATTEMPTS + 1)
    )


def find_replacements(read: Reader, original_transfer_id: str) -> list[Mapping[str, Any]]:
    """Every transfer under any of the original's replacement request_ids."""
    found: list[Mapping[str, Any]] = []
    for rid in all_replacement_request_ids(original_transfer_id):
        found += find_transfers_by_request_id(read, rid)
    return found


def original_request_id(book_id: str, payment_key: str) -> str:
    """M2: payment_key is kit-local, so the key carries the book id."""
    if not book_id:
        raise ValueError("a book id is required")
    return derive_request_id(KIT, f"{book_id}:payment:{payment_key}", ORIGINAL_STEP)


def simulation_request_id(transfer_id: str, next_status: str) -> str:
    """Store key for a simulation write. The simulation body carries no request_id."""
    return derive_request_id(KIT, f"sim:{transfer_id}", next_status)


# ---- operations --------------------------------------------------------------------------


def create_transfer_op(
    *,
    business_key: str,
    request_id: str,
    intent_hash: str,
    beneficiary_id: str,
    amount: Money,
    transfer_method: str,
    reference: str,
    metadata: Mapping[str, str],
) -> Operation:
    body = {
        "request_id": request_id,
        "beneficiary_id": beneficiary_id,
        "transfer_amount": amount.amount,
        "transfer_currency": amount.currency,
        "source_currency": amount.currency,
        "transfer_method": transfer_method,
        "reference": reference,
        "reason": SUPPLIER_REASON,
        "metadata": dict(metadata),
    }
    return Operation(
        kit=KIT,
        name="transfers.create",
        method="POST",
        path="/api/v1/transfers/create",
        body=body,
        request_id=request_id,
        business_key=business_key,
        intent_hash=intent_hash,
    )


def transition_op(
    *, transfer_id: str, next_status: str, intent_hash: str, failure_type: str | None = None
) -> Operation:
    body: dict[str, Any] = {"next_status": next_status}
    if failure_type is not None:
        body["failure_type"] = failure_type
    return Operation(
        kit=KIT,
        name=TRANSITION_OP,
        method="POST",
        path=f"/api/v1/simulation/transfers/{transfer_id}/transition",
        body=body,
        request_id=simulation_request_id(transfer_id, next_status),
        business_key=f"sim:{transfer_id}",
        intent_hash=intent_hash,
    )


def is_ours(transfer: Mapping[str, Any]) -> bool:
    """True when kit 3 created this transfer (POC_LABEL in its metadata or reference)."""
    meta = transfer.get("metadata")
    if isinstance(meta, Mapping) and str(meta.get("poc_label", "")).startswith(POC_LABEL):
        return True
    return str(transfer.get("reference") or "").startswith(POC_LABEL)


# ---- reconcile (runner rule 9) ------------------------------------------------------------


def reconcile_transfer_create(op: Operation, read: Reader) -> Mapping[str, Any] | None:
    """Find the transfer a transfers.create Operation made, by its request_id.

    None when none is listed (which is NOT a failure, M1). The engine checks the found object
    echoes the operation's amount, currency, beneficiary and method and fails it
    closed as request_id_reused otherwise. Two transfers under one request_id are
    ReconcileUndetermined, so nothing is resent.
    """
    found = find_transfers_by_request_id(read, op.request_id)
    if not found:
        return None
    if len(found) > 1:
        raise ReconcileUndetermined("more than one transfer under this request_id")
    return found[0]


def make_transition_reconcile(reached: frozenset[str]) -> Callable[[Operation, Reader], Any]:
    """A simulation transition can return 500 and still apply (FAKE-FIDELITY Errors row,
    `[tr3-sent]`). The endpoint ignores request_id, so the runner never resends it; reconcile
    reads the transfer and reports the transition landed once its status is in `reached`."""

    def reconcile(op: Operation, read: Reader) -> Mapping[str, Any] | None:
        transfer_id = op.path.split("/")[-2]
        transfer = get_transfer(read, transfer_id)
        return transfer if transfer.get("status") in reached else None

    return reconcile
