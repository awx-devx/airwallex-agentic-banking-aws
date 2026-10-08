"""Kit 7 read tools. Pure: Airwallex only through ctx.read / ctx.read_platform, plus the loan
book loaded (never written) from ctx.platform_store.

Outputs carry no acct_ ids, so the model never sees one it could echo into a later call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from awx_common.errors import TenantScopeViolation
from awx_common.workflow import PolicyContext

from .book import LoanBook, PortfolioNotOpen, load_book, week_window
from .ledger import Acct
from .provider import available_balance, weekly_revenue
from .reconcile import reconcile
from .repayment import plan_collection
from .workflow import DISAGREE, log, validate_collect, validate_disburse, validate_reconcile

ReadFn = Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]


def _loan_summary(book: LoanBook, loan_id: str) -> dict[str, Any]:
    led = book.ledger
    terms = book.loan(loan_id)
    pending = book.pending_collection(loan_id)
    return {
        "loan_id": loan_id,
        "commitment": terms.commitment,
        "disbursed": book.disbursed(loan_id),
        "commitment_remaining": book.commitment_remaining(loan_id),
        "principal_outstanding": led.balance(Acct.PRINCIPAL_RECEIVABLE, loan_id),
        "fee_outstanding": led.balance(Acct.FEE_RECEIVABLE, loan_id),
        "arrears": led.arrears(loan_id),
        "total_outstanding": led.outstanding(loan_id),
        "fee_earned": -led.balance(Acct.FEE_INCOME, loan_id),
        "share_rate": terms.repayment.share_rate,
        "weekly_cap": terms.repayment.weekly_cap,
        "weekly_floor": terms.repayment.weekly_floor,
        "next_collectible_week": book.next_collectible_week(loan_id),
        "collection_in_flight": pending.week if pending else None,
        "tranche_in_flight": book.pending_for(loan_id) is not None,
    }


NOT_OPEN = "no loan portfolio is open"


def _guard(book: LoanBook, ctx: PolicyContext) -> Mapping[str, Any] | None:
    """The tenants-row check for reads: run before any Airwallex call or book figure."""
    if book.tenant_row_disagrees(ctx.tenant):
        log.warning(
            "k7 read refused, tenants row disagrees: tenant=%s row_account=%s",
            ctx.tenant.tenant_id,
            ctx.tenant.connected_account_id,
        )
        return {"blocked": DISAGREE}
    return None


def _borrower_position(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    intent = validate_disburse(args)
    try:
        book, _ = load_book(ctx.platform_store)
    except PortfolioNotOpen:
        return {"blocked": NOT_OPEN}  # a stated not-configured answer
    if (stop := _guard(book, ctx)) is not None:
        return stop
    terms = book.loan_for_tenant(intent["loan_id"], ctx.tenant)
    return {
        **_loan_summary(book, terms.loan_id),
        "wallet_available": available_balance(ctx.read, terms.currency),
        "weeks": [
            {
                "week": r.week,
                "due": r.due,
                "collected": r.collected,
                "shortfall": r.shortfall,
                "charge_status": r.charge_status,
            }
            for r in book.week_records(terms.loan_id)
        ],
    }


def _collection_preview(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    intent = validate_collect(args)
    try:
        book, _ = load_book(ctx.platform_store)
    except PortfolioNotOpen:
        return {"blocked": NOT_OPEN}  # a stated not-configured answer
    if (stop := _guard(book, ctx)) is not None:
        return stop
    terms = book.loan_for_tenant(intent["loan_id"], ctx.tenant)
    start, end = week_window(intent["week"])
    own = frozenset(book.issued_request_ids()) | frozenset(
        e.provider_id for e in book.ledger.entries if e.provider_id
    )
    plan = plan_collection(
        weekly_revenue(
            ctx,
            terms.currency,
            start,
            end,
            own,
            book.tranche_credits(terms.loan_id, start, end),
        ),
        book.ledger.outstanding(terms.loan_id),
        available_balance(ctx.read, terms.currency),
        terms.repayment,
    )
    return {
        "loan_id": terms.loan_id,
        "week": intent["week"],
        "week_ended": end <= ctx.now,
        "is_next_collectible_week": book.next_collectible_week(terms.loan_id) == intent["week"],
        "already_closed": book.week_record(terms.loan_id, intent["week"]) is not None,
        "revenue": plan.revenue,
        "share": plan.share,
        "due": plan.due,
        "wallet_available": plan.available,
        "would_collect": plan.collect,
        "shortfall_to_arrears": plan.shortfall,
    }


def _portfolio_position(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    validate_reconcile(args)
    if ctx.tenant.connected_account_id is not None:
        raise TenantScopeViolation("portfolio position is platform scope only")
    try:
        book, _ = load_book(ctx.platform_store)
    except PortfolioNotOpen:
        return {"blocked": NOT_OPEN}  # a stated not-configured answer
    cfg = book.config
    available = available_balance(ctx.read, cfg.currency)
    result = reconcile(book, ctx.read, available)
    last = book.last_reconcile
    return {
        "ledger_cash": book.ledger.platform_cash(),
        "provider_available": available,
        "usable_cash": book.usable_cash(available),
        "reserve_floor": cfg.reserve_floor,
        "in_flight": book.pending_total(),
        "last_reconcile_at": last.at if last else None,
        "reconciled": result.ok,
        "breaks": list(result.breaks),
        "loans": [_loan_summary(book, t.loan_id) for t in book.loans()],
        "tranche_sizings": book.sizings(),  # platform scope: operators only
    }


READS: dict[str, ReadFn] = {
    "read_borrower_position": _borrower_position,
    "read_weekly_collection_preview": _collection_preview,
    "read_portfolio_position": _portfolio_position,
}
