"""Kit 7 operator commands: the shipped entry point for deploy-time state.

A deployed kit runs no setup code, so the portfolio, its loans and its capital reach the
`k7#platform` store only through these commands, run by an operator (`awx-deploy apply`)
or by tests, always through `awx_common.operator_commands.run_operator_command`. They are not
tools: the agent can never reach them. None of them moves money: the context has no write
port, so funding the platform wallet in the sandbox stays a separate step under the flock.

- open_portfolio (once): create the book with its config and mint the book id (M2). A replace
  keeps the book id, the loans and the ledger, and swaps only the config.
- add_loan (feed, keyed by loan_id): one borrower's loan terms, with its tenant and connected
  account. The account is checked through `read` (exists, ACTIVE, `poc-i7-` nickname). The
  same terms again are a no-op; different terms under a loaded loan_id are refused.
- fund_capital (feed, keyed by the Airwallex deposit that funded it): a CAPITAL ledger entry
  earmarking platform-wallet money for this portfolio. The deposit is read through
  `GET /api/v1/financial_transactions` (platform scope): it must exist, be a DEPOSIT into the
  platform wallet in the book currency, and cover the amount. One deposit backs at most one
  capital entry, so the same money can never be earmarked twice. Ledger state only. The same
  deposit and amount again is a no-op; another amount under it is refused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from awx_common import Money, ReadRequest
from awx_common.errors import AwxError
from awx_common.operator_commands import (
    OperatorCommand,
    OperatorCommandError,
    OperatorContext,
    require_fresh_or_replace,
)
from awx_common.store import KitStore, VersionConflict

from .book import (
    BOOK_KEY,
    LoanBook,
    LoanTerms,
    PortfolioConfig,
    check_loan_id,
    is_demo_period,
    load_book,
    mint_book_id,
    update_book,
    week_window,
)
from .ledger import LedgerError, capital_entry
from .repayment import RepaymentTerms

LABEL = "poc-i7-"
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _only(args: Mapping[str, Any], allowed: set[str], required: set[str]) -> None:
    extra = set(args) - allowed
    if extra:
        raise OperatorCommandError(f"unexpected arguments {sorted(extra)}")
    missing = required - set(args)
    if missing:
        raise OperatorCommandError(f"missing arguments {sorted(missing)}")


def _money(value: Any, currency: str, what: str) -> Money:
    """Money from a decimal string. Floats and other types are refused (no precision loss)."""
    if not isinstance(value, str):
        raise OperatorCommandError(f'{what} must be a decimal string such as "1000.00"')
    try:
        return Money.of(value, currency)
    except (InvalidOperation, ValueError) as exc:
        raise OperatorCommandError(f"{what}: {exc}") from None


def _decimal(value: Any, what: str) -> Decimal:
    if not isinstance(value, str):
        raise OperatorCommandError(f'{what} must be a decimal string such as "0.10"')
    try:
        return Decimal(value)
    except InvalidOperation:
        raise OperatorCommandError(f"{what} is not a decimal") from None


# open_portfolio

_CONFIG_FIELDS = {
    "currency",
    "reserve_floor",
    "min_disbursement",
    "disbursement_increment",
    "dual_approval_above",
    "demo_period_s",
}


def _config(args: Mapping[str, Any]) -> PortfolioConfig:
    cur = args.get("currency")
    if not isinstance(cur, str):
        raise OperatorCommandError("currency is required")
    demo = args.get("demo_period_s")
    if demo is not None and (isinstance(demo, bool) or not isinstance(demo, int)):
        raise OperatorCommandError("demo_period_s must be an integer number of seconds")
    try:
        return PortfolioConfig(
            currency=cur,
            reserve_floor=_money(args["reserve_floor"], cur, "reserve_floor"),
            min_disbursement=_money(args["min_disbursement"], cur, "min_disbursement"),
            disbursement_increment=_money(
                args["disbursement_increment"], cur, "disbursement_increment"
            ),
            dual_approval_above=_money(args["dual_approval_above"], cur, "dual_approval_above"),
            demo_period_s=demo,
        )
    except ValueError as exc:
        raise OperatorCommandError(str(exc)) from None


def _open_portfolio(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    _only(args, _CONFIG_FIELDS | {"replace"}, _CONFIG_FIELDS - {"demo_period_s"})
    config = _config(args)
    store = ctx.platform_store
    existing = store.get(BOOK_KEY)
    replacing = require_fresh_or_replace(existing is not None, args, "the kit 7 portfolio")
    if not replacing:
        book = LoanBook(config, mint_book_id())
        try:
            store.put(BOOK_KEY, book.to_state(), expected_version=None)
        except VersionConflict:
            raise OperatorCommandError(
                "the kit 7 portfolio was created concurrently; run again"
            ) from None
        return {"book_id": book.book_id, "replaced": False, "loans": 0}

    def swap(book: LoanBook) -> str:
        if config.currency != book.config.currency:
            raise OperatorCommandError("a replace cannot change the portfolio currency")
        if config.demo_period_s != book.config.demo_period_s and (
            book.week_records_any() or book.pending()
        ):
            raise OperatorCommandError("demo_period_s cannot change once collections have started")
        book.config = config  # the book id, loans and ledger are kept (rule M2)
        return book.book_id

    book_id = _write(store, swap)
    return {"book_id": book_id, "replaced": True, "loans": len(load_book(store)[0].loans())}


# add_loan

_LOAN_FIELDS = {
    "loan_id",
    "tenant_id",
    "connected_account_id",
    "commitment",
    "fee_rate",
    "share_rate",
    "weekly_cap",
    "weekly_floor",
    "first_week",
}


def _check_account(ctx: OperatorContext, account: str) -> None:
    """The borrower's connected account exists, is ACTIVE and is labelled ours."""
    if ctx.read is None:
        raise OperatorCommandError("add_loan needs Airwallex reads to check the account")
    if not isinstance(account, str) or not account.startswith("acct_"):
        raise OperatorCommandError("connected_account_id must be an acct_ id")
    try:
        body = ctx.read(ReadRequest(f"/api/v1/accounts/{account}"))
    except AwxError as exc:
        raise OperatorCommandError(
            f"connected account {account} not readable: {type(exc).__name__}"
        ) from None
    nickname = str(body.get("nickname") or "")
    if not nickname.startswith(LABEL):
        raise OperatorCommandError(f"connected account {account} is not a {LABEL} account")
    if body.get("status") != "ACTIVE":
        raise OperatorCommandError(f"connected account {account} is {body.get('status')}")


def _terms(args: Mapping[str, Any], currency: str) -> LoanTerms:
    tenant = args.get("tenant_id")
    if not isinstance(tenant, str) or not _TENANT.match(tenant) or tenant == "platform":
        raise OperatorCommandError("tenant_id must be the borrower's tenant id")
    first_week = args.get("first_week")
    if not isinstance(first_week, str):
        raise OperatorCommandError("first_week must be an ISO week such as 2026-W42")
    try:
        week_window(first_week)
        return LoanTerms(
            loan_id=check_loan_id(args.get("loan_id")),
            borrower_tenant_id=tenant,
            borrower_account=args["connected_account_id"],
            commitment=_money(args["commitment"], currency, "commitment"),
            fee_rate=_decimal(args["fee_rate"], "fee_rate"),
            repayment=RepaymentTerms(
                _decimal(args["share_rate"], "share_rate"),
                _money(args["weekly_cap"], currency, "weekly_cap"),
                _money(args["weekly_floor"], currency, "weekly_floor"),
            ),
            first_week=first_week,
        )
    except OperatorCommandError:
        raise
    except (ValueError, TypeError, AwxError) as exc:
        raise OperatorCommandError(str(exc)) from None


def _add_loan(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    _only(args, _LOAN_FIELDS, _LOAN_FIELDS)
    store = _store(ctx)
    book, _ = load_book(store)
    terms = _terms(args, book.config.currency)
    demo = book.config.demo_period_s
    if (demo is None) == is_demo_period(terms.first_week):
        raise OperatorCommandError(
            "first_week must be an ISO week on a weekly portfolio and a DEMO period on a "
            f"demo portfolio (demo_period_s={demo})"
        )
    if demo is not None and not terms.first_week.startswith(f"DEMO{demo}S-"):
        raise OperatorCommandError(f"first_week must be a DEMO{demo}S- period")
    _check_account(ctx, terms.borrower_account)

    def add(b: LoanBook) -> str:
        existing = {t.loan_id: t for t in b.loans()}.get(terms.loan_id)
        if existing is not None:
            if existing != terms:
                raise OperatorCommandError(
                    f"loan {terms.loan_id} is already loaded with different terms"
                )
            return "unchanged"
        try:
            b.add_loan(terms)  # refuses a second loan on one tenant or connected account
        except ValueError as exc:
            raise OperatorCommandError(str(exc)) from None
        return "added"

    result = _write(store, add)
    return {"loan_id": terms.loan_id, "result": result}


# fund_capital


FINANCIAL_TRANSACTIONS = "/api/v1/financial_transactions"
_DEPOSIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,80}$")


def _find_deposit(ctx: OperatorContext, deposit_id: str, currency: str) -> Money:
    """The deposit into the platform wallet, read through Airwallex (headerless, platform
    scope). Returns its amount. FAKE-FIDELITY.md marks this row shape as assumed (A)."""
    if ctx.read is None:
        raise OperatorCommandError("fund_capital needs Airwallex reads to check the deposit")
    for page in range(50):
        try:
            body = ctx.read(
                ReadRequest(
                    FINANCIAL_TRANSACTIONS,
                    query={"currency": currency, "page_num": str(page), "page_size": "100"},
                )
            )
        except AwxError as exc:
            raise OperatorCommandError(
                f"financial transactions not readable: {type(exc).__name__}"
            ) from None
        for row in body.get("items") or []:
            if deposit_id not in (str(row.get("source_id")), str(row.get("id"))):
                continue
            if row.get("transaction_type") != "DEPOSIT":
                raise OperatorCommandError(f"{deposit_id} is not a deposit")
            if row.get("currency") != currency:
                raise OperatorCommandError(f"deposit {deposit_id} is not in {currency}")
            try:
                return Money.of(str(row.get("amount")), currency)
            except (InvalidOperation, ValueError) as exc:
                raise OperatorCommandError(f"deposit {deposit_id} amount: {exc}") from None
        if not body.get("has_more"):
            break
    raise OperatorCommandError(f"no deposit {deposit_id} into the platform wallet")


def _fund_capital(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    _only(args, {"deposit_id", "amount"}, {"deposit_id", "amount"})
    deposit_id = args["deposit_id"]
    if not isinstance(deposit_id, str) or not _DEPOSIT_ID.match(deposit_id):
        raise OperatorCommandError("deposit_id must be the Airwallex deposit id")
    store = _store(ctx)
    book, _ = load_book(store)
    currency = book.config.currency
    amount = _money(args["amount"], currency, "amount")
    if amount.amount <= 0:
        raise OperatorCommandError("amount must be greater than zero")
    deposited = _find_deposit(ctx, deposit_id, currency)
    if amount > deposited:
        raise OperatorCommandError(
            f"deposit {deposit_id} is {deposited}, less than the {amount} to earmark"
        )
    entry = capital_entry(f"capital:{deposit_id}", amount, provider_id=deposit_id)

    def fund(b: LoanBook) -> str:
        prior = b.ledger.get(entry.key)
        if prior is not None:
            if prior.fingerprint() != entry.fingerprint():
                raise OperatorCommandError(
                    f"deposit {deposit_id} already funds capital with another amount"
                )
            return "unchanged"
        if any(e.provider_id == deposit_id and e.key != entry.key for e in b.ledger.entries):
            raise OperatorCommandError(f"deposit {deposit_id} already backs a capital entry")
        b.ledger.post(entry)
        return "funded"

    result = _write(store, fund)
    return {
        "deposit_id": deposit_id,
        "result": result,
        "ledger_cash": str(load_book(store)[0].ledger.platform_cash()),
    }


def _write(store: KitStore, change: Any) -> Any:
    """One versioned write of the whole book, after every check inside `change`
    passed; a refusal (raised before the put) writes nothing, and a concurrent write is an
    OperatorCommandError, never a VersionConflict."""
    try:
        return update_book(store, change)
    except VersionConflict:
        raise OperatorCommandError("the kit 7 book changed concurrently; run again") from None
    except LedgerError as exc:
        raise OperatorCommandError(str(exc)) from None


def _store(ctx: OperatorContext) -> KitStore:
    if ctx.platform_store.get(BOOK_KEY) is None:
        raise OperatorCommandError("no portfolio: run open_portfolio first")
    return ctx.platform_store


OPERATOR_COMMANDS: tuple[OperatorCommand, ...] = (
    OperatorCommand(
        "open_portfolio",
        "Create the kit 7 portfolio book in k7#platform and mint its book id (replace keeps it).",
        _open_portfolio,
        once=True,
    ),
    OperatorCommand(
        "add_loan",
        "Load one poc-i7- borrower's loan terms, tenant and checked connected account.",
        _add_loan,
    ),
    OperatorCommand(
        "fund_capital",
        "Earmark a checked platform-wallet deposit as portfolio capital (ledger only).",
        _fund_capital,
    ),
)
