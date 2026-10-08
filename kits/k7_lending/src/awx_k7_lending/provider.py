"""Airwallex shapes for kit 7: operation builders, read requests and response parsers.

Charges and disbursements are platform money movements: they run as the
platform, name the borrower's connected account in the body (`source` / `destination`, from
awx_common.tenancy.PLATFORM_BODY_FIELDS) and never send x-on-behalf-of. Live, the header makes
the named account the PAYER of a connected account transfer, and charges/create refuses it
with 400 "Permission denied." (FAKE-FIDELITY rows 103-105). Both are bound only through
awx_common.bind_platform_operation, which takes the account from the server-resolved tenant.

Their finality and reconcile reads are headerless, so in a borrower tenant they are platform
reads (rule 5) and go through `platform_read_allowlist`.

Shapes not verified against the sandbox are marked ASSUMPTION.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from awx_common import Money, Operation, OperationResult, ReadRequest, TenantContext
from awx_common.intent import derive_request_id
from awx_common.tenancy import bind_platform_operation
from awx_common.workflow import FinalitySpec, PolicyContext, ReconcileUndetermined

KIT = "k7"
REFERENCE_PREFIX = "poc-k7-"

# FAKE-FIDELITY row 106: both pass the sandbox's two-stage reason validation (the 22 accepted
# values), and the live sandbox runs settled charges and transfers sent with them. The other
# live-verified value is professional_business_services.
CHARGE_REASON = "loan_credit_repayment"
DISBURSE_REASON = "investment_capital"

# FAKE-FIDELITY row 109: NEW, PENDING, SETTLED, SUSPENDED, FAILED. SUSPENDED needs a person, so
# it ends the poll and leaves the week or tranche open for an operator.
PLATFORM_TERMINAL = frozenset({"SETTLED", "FAILED", "SUSPENDED"})
PLATFORM_SUCCESS = frozenset({"SETTLED"})

BALANCES_CURRENT = "/api/v1/balances/current"
BALANCES_HISTORY = "/api/v1/balances/history"
CHARGES = "/api/v1/charges"
CA_TRANSFERS = "/api/v1/connected_account_transfers"
_MAX_PAGES = 20


def to_money(value: Any, currency: str) -> Money:
    """Provider amount -> Money. JSON numbers must already be Decimal (the spine parses them
    exactly); a float here means precision was lost upstream."""
    if isinstance(value, float | bool) or value is None:
        raise TypeError(f"provider amount {value!r} is not an exact number")
    return Money.of(value if isinstance(value, Decimal | int) else str(value), currency)


def _items(body: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """A read is always a Mapping; list endpoints carry `items`."""
    return body.get("items") or []


def platform_reader(ctx: PolicyContext) -> Callable[[ReadRequest], Mapping[str, Any]]:
    """A headerless read: ctx.read in the platform tenant, ctx.read_platform in a borrower's."""
    if ctx.tenant.connected_account_id is None:
        return ctx.read
    if ctx.read_platform is None:
        raise ValueError("this spec declares no platform_read_allowlist")
    return ctx.read_platform


# Reads


def available_balance(read: Callable[[ReadRequest], Mapping[str, Any]], currency: str) -> Money:
    """Available balance of whichever wallet `read` is scoped to.

    ASSUMPTION (access probe 6 Oct): the row field is `available_amount`.
    """
    for row in _items(read(ReadRequest(BALANCES_CURRENT))):
        if row.get("currency") == currency:
            return to_money(row.get("available_amount"), currency)
    return Money.zero(currency)


def _is_deposit(row: Mapping[str, Any], currency: str) -> bool:
    """Any positive deposit in the currency, whatever it references."""
    return (
        row.get("transaction_type") == "DEPOSIT"
        and row.get("currency", currency) == currency
        and to_money(row.get("amount"), currency).amount > 0
    )


def is_revenue(row: Mapping[str, Any], currency: str, own_ids: frozenset[str]) -> bool:
    """A balance-history row counts as revenue only if it is an external deposit.

    Excluded: anything that is not a DEPOSIT, a non-positive amount,
    another currency, and any row that references a kit-7 movement - our advance landing in
    the wallet, or a charge reversal - by a known provider or request id, or by our
    reference prefix in any text field.
    """
    if row.get("transaction_type") != "DEPOSIT" or row.get("currency", currency) != currency:
        return False
    for key in ("source_id", "source", "id", "request_id"):
        if str(row.get(key, "")) in own_ids:
            return False
    for key in ("reference", "description", "source_type"):
        if REFERENCE_PREFIX in str(row.get(key, "")):
            return False
    return to_money(row.get("amount"), currency).amount > 0


def weekly_revenue(
    ctx: PolicyContext,
    currency: str,
    start: datetime,
    end: datetime,
    own_ids: frozenset[str] = frozenset(),
    own_credits: Money | None = None,
) -> Money:
    """Sum of external deposits into the borrower's wallet in [start, end).

    Two independent guards keep our own advance out of revenue: the row filter (is_revenue),
    and a backstop that does not depend on the row's shape: the result is never more than
    all positive deposits minus `own_credits` (the tranche money that may have landed in the
    window, LoanBook.tranche_credits). Both can only lower revenue, so a double count
    under-collects, never over-collects.

    ASSUMPTION: GET balances/history, its query parameters (from_post_at, to_post_at,
    page_num from 0, page_size) and its row fields (transaction_type, amount, currency,
    source_id, description) are not in the guide or in the spine's sandbox evidence.
    """
    total = Money.zero(currency)
    raw = Money.zero(currency)
    for page in range(_MAX_PAGES):
        body = ctx.read(
            ReadRequest(
                BALANCES_HISTORY,
                query={
                    "currency": currency,
                    "from_post_at": start.isoformat(),
                    "to_post_at": end.isoformat(),
                    "page_num": str(page),
                    "page_size": "100",
                },
            )
        )
        for row in _items(body):
            if _is_deposit(row, currency):
                raw = raw + to_money(row.get("amount"), currency)
            if is_revenue(row, currency, own_ids):
                total = total + to_money(row.get("amount"), currency)
        if not body.get("has_more"):
            credits = own_credits if own_credits is not None else Money.zero(currency)
            return max(min(total, raw - credits), Money.zero(currency))
    raise ValueError("balance history has more pages than the kit will read")


@dataclass(frozen=True)
class ProviderMovement:
    provider_id: str
    kind: str  # "charge" (borrower -> platform) or "transfer" (platform -> borrower)
    request_id: str
    amount: Money
    status: str
    counterparty: str
    reference: str
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)


def _movement(kind: str, row: Mapping[str, Any], currency: str) -> ProviderMovement:
    return ProviderMovement(
        provider_id=str(row["id"]),
        kind=kind,
        request_id=str(row.get("request_id", "")),
        amount=to_money(row.get("amount"), row.get("currency", currency)),
        status=str(row.get("status")),
        counterparty=str(row.get("source" if kind == "charge" else "destination")),
        reference=str(row.get("reference", "")),
        raw=dict(row),
    )


def find_by_request_id(
    read: Callable[[ReadRequest], Mapping[str, Any]], kind: str, request_id: str, currency: str
) -> ProviderMovement | None:
    """GET /charges?request_id= or /connected_account_transfers?request_id= (sandbox-verified,
    FAKE-FIDELITY rows 51-52). An unknown request_id returns no items."""
    path = CHARGES if kind == "charge" else CA_TRANSFERS
    rows = [r for r in _items(read(ReadRequest(path, query={"request_id": request_id})))]
    rows = [r for r in rows if str(r.get("request_id", request_id)) == request_id]
    if len(rows) > 1:
        raise ReconcileUndetermined(f"{len(rows)} objects share request_id {request_id}")
    return _movement(kind, rows[0], currency) if rows else None


def read_charge(provider_id: str) -> ReadRequest:
    return ReadRequest(f"{CHARGES}/{provider_id}")


def read_transfer(provider_id: str) -> ReadRequest:
    return ReadRequest(f"{CA_TRANSFERS}/{provider_id}")


def _status(body: Mapping[str, Any]) -> str:
    return str(body.get("status"))


def _need_id(result: OperationResult) -> str:
    if not result.provider_id:
        raise ValueError("operation result carries no provider id to read")
    return result.provider_id


CHARGE_FINALITY = FinalitySpec(
    read_request=lambda r: read_charge(_need_id(r)),
    status_of=_status,
    terminal=PLATFORM_TERMINAL,
    success=PLATFORM_SUCCESS,
)
TRANSFER_FINALITY = FinalitySpec(
    read_request=lambda r: read_transfer(_need_id(r)),
    status_of=_status,
    terminal=PLATFORM_TERMINAL,
    success=PLATFORM_SUCCESS,
)


def _reconcile_for(kind: str, account_field: str):
    """StepSpec.reconcile (rule 9): find the object by request_id and check it is this
    operation's object (amount, currency, counterparty) before the executor uses it."""

    def reconcile(
        op: Operation, read: Callable[[ReadRequest], Mapping[str, Any]]
    ) -> Mapping[str, Any] | None:
        currency = op.body["currency"]
        found = find_by_request_id(read, kind, op.request_id, currency)
        if found is None:
            # An empty read-back is not proof the write did not land (rule M1): stop at
            # OUTCOME_UNKNOWN so the executor never resends inside this run. A later run
            # re-emits the same Operation and holds on a refusal.
            raise ReconcileUndetermined(f"no object with request_id {op.request_id} yet")
        if (
            found.amount != Money.of(op.body["amount"], currency)
            or found.counterparty != op.body[account_field]
        ):
            raise ReconcileUndetermined(
                f"{found.provider_id} has request_id {op.request_id} but a different "
                "amount or account"
            )
        # The whole object: the spine's own reconcile match compares every field the operation
        # set, reason and reference included, so a trimmed copy reads as another payment.
        return dict(found.raw)

    return reconcile


reconcile_charge = _reconcile_for("charge", "source")
reconcile_transfer = _reconcile_for("transfer", "destination")


# Request ids and references. Business keys name the obligation being paid: one loan's
# week, or one loan's tranche, inside one minted book (M2), plus the attempt, which moves only
# after a definitive failure (M1). Never the amount or the intent hash.


def collection_key(book_id: str, loan_id: str, week: str, attempt: int) -> str:
    return f"{book_id}:collect:{loan_id}:{week}:{attempt}"


def collection_request_id(book_id: str, loan_id: str, week: str, attempt: int) -> str:
    return derive_request_id(KIT, collection_key(book_id, loan_id, week, attempt), "charge")


def disbursement_key(book_id: str, loan_id: str, tranche: int, attempt: int) -> str:
    return f"{book_id}:disburse:{loan_id}:{tranche}:{attempt}"


def disbursement_request_id(book_id: str, loan_id: str, tranche: int, attempt: int) -> str:
    return derive_request_id(KIT, disbursement_key(book_id, loan_id, tranche, attempt), "transfer")


def reference_prefix(book_id: str) -> str:
    return f"{REFERENCE_PREFIX}{book_id}-"


# Operation builders


def _platform_op(
    name: str,
    path: str,
    body: Mapping[str, Any],
    *,
    request_id: str,
    business_key: str,
    intent_hash: str,
    tenant: TenantContext,
) -> Operation:
    return bind_platform_operation(
        Operation(
            kit=KIT,
            name=name,
            method="POST",
            path=path,
            body={"request_id": request_id, **body},
            request_id=request_id,
            business_key=business_key,
            intent_hash=intent_hash,
        ),
        tenant,
    )


def charge_operation(
    *,
    book_id: str,
    loan_id: str,
    week: str,
    attempt: int,
    amount: Money,
    intent_hash: str,
    tenant: TenantContext,
) -> Operation:
    """Borrower -> platform. `source` is filled from the tenant by bind_platform_operation."""
    if amount.amount <= 0:
        raise ValueError("a charge must be positive")
    return _platform_op(
        "charges.create",
        "/api/v1/charges/create",
        {
            "amount": amount.amount,
            "currency": amount.currency,
            "reason": CHARGE_REASON,
            "reference": f"{reference_prefix(book_id)}collect-{loan_id}-{week}-a{attempt}",
        },
        request_id=collection_request_id(book_id, loan_id, week, attempt),
        business_key=collection_key(book_id, loan_id, week, attempt),
        intent_hash=intent_hash,
        tenant=tenant,
    )


def transfer_operation(
    *,
    book_id: str,
    loan_id: str,
    tranche: int,
    attempt: int,
    amount: Money,
    intent_hash: str,
    tenant: TenantContext,
) -> Operation:
    """Platform -> borrower. `destination` is filled from the tenant, with no header."""
    if amount.amount <= 0:
        raise ValueError("a disbursement must be positive")
    return _platform_op(
        "connected_account_transfers.create",
        "/api/v1/connected_account_transfers/create",
        {
            "amount": amount.amount,
            "currency": amount.currency,
            "reason": DISBURSE_REASON,
            "reference": f"{reference_prefix(book_id)}advance-{loan_id}-t{tranche}-a{attempt}",
        },
        request_id=disbursement_request_id(book_id, loan_id, tranche, attempt),
        business_key=disbursement_key(book_id, loan_id, tranche, attempt),
        intent_hash=intent_hash,
        tenant=tenant,
    )


def report_operation(
    *, book_id: str, ledger_version: int, intent_hash: str, tenant: TenantContext
) -> Operation:
    """Portfolio balance report, run as the platform: corroborating evidence, not the ledger
    of record. Keyed by the ledger version it snapshots, so a re-run asks for the same one."""
    if tenant.connected_account_id is not None:
        raise ValueError("the portfolio report runs as the platform")
    key = f"{book_id}:report:v{ledger_version}"
    request_id = derive_request_id(KIT, key, "report")
    return Operation(
        kit=KIT,
        name="platform_reports.create",
        method="POST",
        path="/api/v1/platform_reports/create",
        # platform_reports/create takes no request_id (the guide names only type and
        # file_format), so a retry is deduplicated only in the operation store; a lost
        # response can create a second report. It moves no money.
        body={"type": "BALANCE_REPORT", "file_format": "CSV"},
        request_id=request_id,
        business_key=key,
        intent_hash=intent_hash,
    )
