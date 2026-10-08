"""Read tools. Each reads Airwallex through ctx.read and kit state (the book, obligation
locks) through ctx.store, and returns plain JSON for the model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from awx_common import RiskTier, to_jsonable
from awx_common.workflow import PolicyContext

from .api import read_balances, read_rates
from .book import NO_BOOK, TreasuryBook, load_book
from .intake import InvalidInput, parse_contradictions
from .intake import require_keys as _require_keys
from .locks import LockState, quote_owner, read_lock, read_ratelock
from .money_rules import BadProviderData, K1Error
from .plan import FUNDED, build_plan
from .policy import PolicyConfig, approval_tier, confidence_band

ReadFn = Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]


class NoBook(K1Error):
    pass


def _book(ctx: PolicyContext) -> TreasuryBook:
    book = load_book(ctx.store)
    if book is None:
        raise NoBook(NO_BOOK)
    return book


def build_reads(config: PolicyConfig | None = None) -> dict[str, ReadFn]:
    cfg = config or PolicyConfig()

    def balances(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
        _require_keys(args, set(), "read_balances input")
        book = _book(ctx)
        bal = read_balances(ctx.read, book)
        return to_jsonable(
            {
                "balances": [
                    {
                        "currency": c,
                        "available": bal[c],
                        "reserve_floor": book.floors[c],
                        "headroom": bal[c] - book.floors[c],
                    }
                    for c in book.currencies
                ]
            }
        )

    def obligations(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
        _require_keys(args, set(), "read_obligations input")
        book = _book(ctx)
        return to_jsonable(
            {
                "book_version": book.book_version,
                "funding_currency": book.funding_currency,
                "obligations": [
                    {
                        "obligation_id": o.obligation_id,
                        "description": o.description,
                        "amount": o.amount,
                        "method": o.method.value,
                        "due_at": o.due_at,
                        "priority": o.priority.name,
                        "policy_exception": o.policy_exception,
                        "lock": _lock_view(ctx, o.obligation_id, cfg),
                    }
                    for o in book.obligations
                ],
                "forecasts": [
                    {
                        "receipt_id": f.receipt_id,
                        "payer": f.payer,
                        "amount": f.amount,
                        "expected_at": f.expected_at,
                        "bank_corroborated": f.bank_corroborated,
                        "invoice_matched": f.invoice_matched,
                    }
                    for f in book.forecasts
                ],
            }
        )

    def fx_rate(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
        _require_keys(args, {"buy_currency"}, "read_fx_rate input")
        book = _book(ctx)
        cur = args.get("buy_currency")
        if cur not in book.currencies or cur == book.funding_currency:
            raise InvalidInput("buy_currency must be one of the book's non-funding currencies")
        rate = read_rates(ctx.read, book, {cur})[cur]
        return to_jsonable(
            {
                "buy_currency": cur,
                "sell_currency": book.funding_currency,
                "currency_pair": rate.currency_pair,
                "indicative_rate": rate.rate,
                "note": "indicative only; a quote is fetched inside the approved workflow",
            }
        )

    def treasury_plan(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
        _require_keys(args, {"contradictions"}, "read_treasury_plan input")
        book = _book(ctx)
        contradictions = [
            c
            for c in parse_contradictions(args.get("contradictions"))
            if book.has_forecast(c["receipt_id"])
        ]
        bal = read_balances(ctx.read, book)
        try:
            rates = read_rates(ctx.read, book, {o.amount.currency for o in book.obligations})
        except BadProviderData:
            rates = {}
        locked = {
            o.obligation_id
            for o in book.obligations
            if _lock_state(ctx, o.obligation_id, o.version) not in (None, "RELEASED")
        }
        open_ids = [o.obligation_id for o in book.obligations if o.obligation_id not in locked]
        plan = build_plan(book, bal, rates, cfg.plan, only=open_ids)
        band = confidence_band(book.forecasts, {c["receipt_id"] for c in contradictions})
        fundable = [d.obligation_id for d in plan.decisions if d.kind in FUNDED]
        tier = approval_tier(plan, band, book, cfg, requested=fundable) if fundable else None
        return to_jsonable(
            {
                "book_version": book.book_version,
                "confidence_band": band.value,
                "decisions": [
                    {
                        "obligation_id": d.obligation_id,
                        "decision": d.kind.value,
                        "reason": d.reason,
                        "amount_with_fee": d.need,
                        "converted": d.buy,
                    }
                    for d in plan.decisions
                ],
                "conversions": [{"buy": c.buy, "sell_at_most": c.sell} for c in plan.conversions],
                "projected_balances": plan.projected,
                "reserve_breaches": list(plan.breaches),
                "proposable_obligation_ids": fundable,
                "paid_or_in_flight": sorted(locked),
                "rate_locks": _rate_locks(ctx, book),
                "approval_tier": tier.tier.value if tier else RiskTier.AUTO.value,
                "approval_reasons": list(tier.reasons) if tier else [],
            }
        )

    return {
        "read_balances": balances,
        "read_obligations": obligations,
        "read_fx_rate": fx_rate,
        "read_treasury_plan": treasury_plan,
    }


def _lock_state(ctx: PolicyContext, obligation_id: str, version: str) -> str | None:
    lock = read_lock(ctx.store, obligation_id) if ctx.store is not None else None
    return lock.state.value if lock else None


def _lock_view(ctx: PolicyContext, obligation_id: str, cfg: PolicyConfig) -> dict[str, Any] | None:
    """The obligation's lock, with its age and whether a person must act. A SUBMITTED lock past
    the dedup window is reported here, since no later run's postcondition can reach it. Pure:
    reports, never writes."""
    lock = read_lock(ctx.store, obligation_id) if ctx.store is not None else None
    if lock is None:
        return None
    age = ctx.now - lock.submitted_at if lock.submitted_at is not None else None
    stale = lock.state is LockState.SUBMITTED and age is not None and age > cfg.dedup_window
    return {
        "state": lock.state.value,
        "attempt": lock.attempt,
        "days_since_submitted": age.days if age is not None else None,
        "needs_person": lock.state is LockState.NEEDS_PERSON or stale,
        "note": lock.note,
        "release_with": "propose_release_obligation"
        if lock.state is LockState.NEEDS_PERSON or stale
        else None,
    }


def _rate_locks(ctx: PolicyContext, book: TreasuryBook) -> list[dict[str, Any]]:
    out = []
    for cur in book.currencies:
        lock = read_ratelock(ctx.store, cur)
        if lock is None:
            continue
        out.append(
            {
                "buy": lock["buy"],
                "sell": lock["sell"],
                "valid_to": lock["valid_to"],
                "expired": lock["valid_to"] <= ctx.now,
                "used": quote_owner(ctx.store, lock["quote_id"]) is not None,
            }
        )
    return out


def _stated(fn: ReadFn) -> ReadFn:
    """On an empty kit store a read gives a stated answer, never an
    INTERNAL raise. Input is still validated first (InvalidInput is raised before the book is
    loaded)."""

    def read(args: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
        try:
            return fn(args, ctx)
        except NoBook:
            return {"status": "NOT_CONFIGURED", "book": None, "reason": NO_BOOK}

    return read


READS: dict[str, ReadFn] = {name: _stated(fn) for name, fn in build_reads().items()}
