"""Airwallex paths, reads, reconcile and finality for kit 1.

Shapes follow the sandbox evidence in awx_spine's FAKE-FIDELITY.md. The client never sends
x-api-version (FAKE-FIDELITY row 20), which satisfies the guide's FX rule.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from awx_common import Money, Operation, OperationResult, ReadRequest
from awx_common.workflow import FinalitySpec, ReconcileUndetermined, SandboxAdvance

from .book import TreasuryBook
from .money_rules import BadProviderData, IndicativeRate, parse_balances, parse_rate, to_decimal

BALANCES = "/api/v1/balances/current"
FX_RATES = "/api/v1/fx/rates/current"
FX_QUOTES_CREATE = "/api/v1/fx/quotes/create"
FX_CONVERSIONS_CREATE = "/api/v1/fx/conversions/create"
FX_CONVERSIONS = "/api/v1/fx/conversions"
TRANSFERS_CREATE = "/api/v1/transfers/create"
TRANSFERS = "/api/v1/transfers"

READ_ALLOWLIST: tuple[str, ...] = (
    r"^/api/v1/balances/current$",
    r"^/api/v1/fx/rates/current$",
    r"^/api/v1/fx/conversions$",  # ?request_id= look-up for reconcile
    r"^/api/v1/fx/conversions/[A-Za-z0-9_-]{1,64}$",
    r"^/api/v1/transfers$",  # ?request_id= look-up for reconcile
    r"^/api/v1/transfers/[A-Za-z0-9_-]{1,64}$",
)

Reader = Callable[[ReadRequest], Mapping[str, Any]]


def read_balances(read: Reader, book: TreasuryBook) -> dict[str, Money]:
    return parse_balances(read(ReadRequest(BALANCES)), book.currencies)


def read_rates(
    read: Reader, book: TreasuryBook, buy_currencies: Iterable[str]
) -> dict[str, IndicativeRate]:
    out: dict[str, IndicativeRate] = {}
    for cur in sorted(set(buy_currencies) - {book.funding_currency}):
        body = read(
            ReadRequest(FX_RATES, {"buy_currency": cur, "sell_currency": book.funding_currency})
        )
        out[cur] = parse_rate(body, buy_currency=cur, sell_currency=book.funding_currency)
    return out


def provider_id(result: OperationResult, *keys: str) -> str:
    if result.provider_id:
        return result.provider_id
    body: Mapping[str, Any] = result.body or {}
    for key in keys:
        if body.get(key):
            return str(body[key])
    raise BadProviderData("write result carries no provider object id")


def _status(body: Mapping[str, Any]) -> str:
    return str(body.get("status", ""))


def _same_amount(found: Any, sent: Any) -> bool:
    try:
        return to_decimal(found) == to_decimal(sent)
    except BadProviderData:
        return False


def _lookup(read: Reader, path: str, request_id: str) -> Mapping[str, Any] | None:
    items = read(ReadRequest(path, {"request_id": request_id})).get("items")
    if not isinstance(items, list):
        raise ReconcileUndetermined(f"{path} look-up did not return items")
    matches = [i for i in items if isinstance(i, Mapping) and i.get("request_id") == request_id]
    if len(matches) > 1:
        raise ReconcileUndetermined(f"{path} returned {len(matches)} objects for one request_id")
    return matches[0] if matches else None


def find_conversion(op: Operation, read: Reader) -> Mapping[str, Any] | None:
    """StepSpec.reconcile for fx/conversions/create (rule 9: reconcile before any resend).

    A found object must match the operation's currencies and buy amount, else the run cannot
    tell what happened and stops (ReconcileUndetermined, rule 9).
    """
    found = _lookup(read, FX_CONVERSIONS, op.request_id)
    if found is None:
        return None
    b = op.body
    if (
        found.get("buy_currency") != b["buy_currency"]
        or found.get("sell_currency") != b["sell_currency"]
        or not _same_amount(found.get("buy_amount"), b["buy_amount"])
    ):
        raise ReconcileUndetermined("conversion found by request_id does not match the operation")
    if b.get("quote_id") is not None and found.get("quote_id") != b["quote_id"]:
        # M3: the landed conversion must have booked our quote.
        raise ReconcileUndetermined("conversion found by request_id booked another quote")
    return found


def find_transfer(op: Operation, read: Reader) -> Mapping[str, Any] | None:
    """StepSpec.reconcile for transfers/create. Checks currency, amount and beneficiary.

    The transfer object's field names (transfer_currency, transfer_amount, beneficiary_id)
    mirror the create body; the live sandbox list response carries them.
    """
    found = _lookup(read, TRANSFERS, op.request_id)
    if found is None:
        return None
    b = op.body
    if (
        found.get("transfer_currency") != b["transfer_currency"]
        or not _same_amount(found.get("transfer_amount"), b["transfer_amount"])
        or found.get("beneficiary_id", b["beneficiary_id"]) != b["beneficiary_id"]
    ):
        raise ReconcileUndetermined("transfer found by request_id does not match the operation")
    return found


class AmbiguousRow(Exception):
    """Two rows under one request_id in a finality read: the engine fails the run closed."""


def make_select(id_key: str):
    """FinalitySpec.select: this operation's own row out of a `?request_id=` list read.

    With a provider_id in the result, ONLY the row whose id equals it; any other row is another
    object (an older one under the same request_id) and is never returned, so a lagging list
    gives None: a non-terminal poll that fails closed at the finality timeout.
    Without a provider_id, the single row carrying the request_id; two fail closed."""

    def select(body: Mapping[str, Any], result: OperationResult) -> Mapping[str, Any] | None:
        items = body.get("items")
        if not isinstance(items, list):
            return body  # a GET by id
        rows = [
            i for i in items if isinstance(i, Mapping) and i.get("request_id") == result.request_id
        ]
        if result.provider_id:
            mine = [r for r in rows if r.get(id_key) == result.provider_id]
            return mine[0] if mine else None
        if len(rows) > 1:
            raise AmbiguousRow(f"{len(rows)} objects under request_id {result.request_id}")
        return rows[0] if rows else None

    return select


# Finality polls read `?request_id=` lists and pick their own row with `make_select`.
# Conversions settle at create with status SETTLED (FAKE-FIDELITY [fx1-create]). Anything else
# keeps polling until timeout, which fails closed.
CONVERSION_FINALITY = FinalitySpec(
    read_request=lambda r: ReadRequest(FX_CONVERSIONS, {"request_id": r.request_id}),
    status_of=_status,
    terminal=frozenset({"SETTLED", "CANCELLED"}),
    success=frozenset({"SETTLED"}),
    interval_s=2,
    timeout_s=60,
    select=make_select("conversion_id"),
)

TRANSFER_TERMINAL = frozenset({"PAID", "CANCELLED"})  # a failed transfer reads CANCELLED
TRANSFER_SUCCESS = frozenset({"PAID"})


def transfer_finality(sandbox_advance: SandboxAdvance | None = None) -> FinalitySpec:
    """Transfers go SCHEDULED -> PROCESSING at create and move no further without a
    simulation call (FAKE-FIDELITY [tr1-create]). SENT is in flight, never final. In sandbox
    mode the runner drives SENT then PAID through `sandbox_advance` (rule 10;
    awx_common.sandbox.make_transfer_advance("k1") in workflow.SPECS), which sees the selected
    row. Without it a transfer stays PROCESSING, the poll times out, the run fails closed and
    the lock records SUBMITTED."""
    return FinalitySpec(
        read_request=lambda r: ReadRequest(TRANSFERS, {"request_id": r.request_id}),
        status_of=_status,
        terminal=TRANSFER_TERMINAL,
        success=TRANSFER_SUCCESS,
        interval_s=5,
        timeout_s=600,
        sandbox_advance=sandbox_advance,
        select=make_select("id"),
    )
