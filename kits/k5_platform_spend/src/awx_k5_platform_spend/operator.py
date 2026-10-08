"""Kit 5 operator commands: the only way deploy-time state reaches a deployed kit.

- `seed_platform_setup` (once): the platform setup document (bridge policy, card programs,
  tenant map, obligations, fees, repayment record) in the platform store. Every connected
  account is checked through `ctx.read`: it exists, is ACTIVE and its nickname carries
  `poc-i5-`. Two tenants may not share an account. A replace keeps the capital ledger and its
  book id untouched. It refuses to drop a tenant, or move its account, while it has capital
  held, owed or in flight (a `sent/` attempt neither posted nor failed); and to drop an
  obligation, or change its tenant or amount, while it has a bridge held, queued or posted. A
  dropped tenant with only queued shortfalls loses those queue places in the same command.
- `fund_capital` (feed, idempotent per key): records platform capital in the ledger. Ledger
  state only: it moves no money. The matching sandbox deposit into the platform wallet is a
  separate step under the sandbox flock. The first funding mints the platform book id (M2).

Neither is a tool, a workflow action or a function; the agent can never reach them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from awx_common import Money, ReadRequest
from awx_common.errors import AwxError, NotFound
from awx_common.operator_commands import (
    OperatorCommand,
    OperatorCommandError,
    OperatorContext,
    require_fresh_or_replace,
)
from awx_common.store import KitStore, VersionConflict

from .book import (
    SETUP_KEY,
    Obligation,
    PlatformBook,
    PlatformSetup,
    decode_setup,
    encode_setup,
    load_platform_setup,
    queue_key,
)
from .config import CardProgram, PlatformConfig
from .ledger import CapitalLedger, LedgerError
from .operations import attempt_step, business_key, request_id_for
from .tenants import TenantMap, TenantRecord
from .workflow import CARD_PENDING_PREFIX, SENT_PREFIX
from .workflow import _card_ref_key as card_ref_key

ACCOUNT_LABEL = "poc-i5-"
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_ACCT = re.compile(r"^acct_[A-Za-z0-9_-]{6,40}$")
# Template placeholders ("acct_placeholder...", "acct_example...", "acct_poc-i5-..."): a real
# Airwallex id is random, so only these whole-word prefixes are refused.
_PLACEHOLDER = re.compile(r"^acct_(placeholder|example|changeme|todo|poc[-_])")


def _fail(message: str) -> OperatorCommandError:
    return OperatorCommandError(message)


def _keys(obj: Any, what: str, required: set[str], optional: set[str] = frozenset()) -> Mapping:
    if not isinstance(obj, Mapping):
        raise _fail(f"{what} must be an object")
    unknown = set(obj) - required - optional
    if unknown:
        raise _fail(f"{what} has unknown fields {sorted(unknown)}")
    missing = required - set(obj)
    if missing:
        raise _fail(f"{what} is missing {sorted(missing)}")
    return obj


def _str(obj: Mapping, key: str, what: str, pattern: re.Pattern[str] | None = None) -> str:
    value = obj[key]
    if not isinstance(value, str) or (pattern is not None and not pattern.match(value)):
        raise _fail(f"{what}.{key} is malformed")
    return value


def _money(value: Any, currency: str, what: str) -> Money:
    if not isinstance(value, str):
        raise _fail(f"{what} must be a decimal string (never a JSON number)")
    try:
        return Money.of(Decimal(value), currency)
    except (InvalidOperation, ValueError) as exc:
        raise _fail(f"{what} is not a {currency} amount: {exc}") from None


def _day(value: Any, what: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise _fail(f"{what} must be an ISO date") from None


def _check_account(ctx: OperatorContext, account_id: str, tenant_id: str) -> None:
    if _PLACEHOLDER.search(account_id):
        raise _fail(f"{tenant_id}: {account_id} looks like a template placeholder")
    if ctx.read is None:
        raise _fail("connected accounts must be checked through read; none is available")
    try:
        body = ctx.read(ReadRequest(path=f"/api/v1/accounts/{account_id}"))
    except NotFound:
        raise _fail(f"{tenant_id}: connected account {account_id} does not exist") from None
    except AwxError as exc:
        raise _fail(f"{tenant_id}: could not check {account_id}: {type(exc).__name__}") from None
    if body.get("id") != account_id:
        raise _fail(f"{tenant_id}: the read returned another account")
    if body.get("status") != "ACTIVE":
        raise _fail(f"{tenant_id}: connected account {account_id} is not ACTIVE")
    nickname = body.get("nickname") or ""
    if not isinstance(nickname, str) or not nickname.startswith(ACCOUNT_LABEL):
        raise _fail(f"{tenant_id}: connected account {account_id} is not labelled {ACCOUNT_LABEL}")


def parse_setup(args: Mapping[str, Any]) -> PlatformSetup:
    """The JSON args of seed_platform_setup -> PlatformSetup. Strict: amounts are strings."""
    a = _keys(
        args,
        "args",
        {
            "currency",
            "reserve_floor",
            "high_risk_bridge",
            "max_bridge_per_tenant",
            "max_bridge_days",
            "card_programs",
            "tenants",
        },
        {"obligations", "monthly_fees", "late_repayments", "replace"},
    )
    ccy = _str(a, "currency", "args", re.compile(r"^[A-Z]{3}$"))
    days = a["max_bridge_days"]
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 31:
        raise _fail("args.max_bridge_days must be an integer from 0 to 31")
    programs: dict[str, CardProgram] = {}
    if not isinstance(a["card_programs"], Mapping):
        raise _fail("args.card_programs must be an object")
    for name, raw in a["card_programs"].items():
        if not _REF.match(str(name)):
            raise _fail(f"card program name {name!r} is malformed")
        p = _keys(
            raw,
            f"card_programs.{name}",
            {"per_transaction_limit", "monthly_limit", "allowed_currencies"},
            {"allowed_merchant_categories"},
        )
        try:
            programs[name] = CardProgram(
                name,
                ccy,
                _money(p["per_transaction_limit"], ccy, f"{name}.per_transaction_limit"),
                _money(p["monthly_limit"], ccy, f"{name}.monthly_limit"),
                tuple(p["allowed_currencies"]),
                tuple(p.get("allowed_merchant_categories", ())),
            )
        except (TypeError, ValueError) as exc:
            raise _fail(f"card program {name}: {exc}") from None
    try:
        config = PlatformConfig(
            currency=ccy,
            reserve_floor=_money(a["reserve_floor"], ccy, "reserve_floor"),
            high_risk_bridge=_money(a["high_risk_bridge"], ccy, "high_risk_bridge"),
            max_bridge_per_tenant=_money(a["max_bridge_per_tenant"], ccy, "max_bridge_per_tenant"),
            max_bridge_days=days,
            card_programs=programs,
        )
    except ValueError as exc:
        raise _fail(f"bridge policy: {exc}") from None
    if not isinstance(a["tenants"], list) or not a["tenants"]:
        raise _fail("args.tenants must be a non-empty list")
    records = []
    for i, raw in enumerate(a["tenants"]):
        t = _keys(
            raw, f"tenants[{i}]", {"tenant_id", "connected_account_id", "display_name"}, {"active"}
        )
        active = t.get("active", True)
        if not isinstance(active, bool):
            raise _fail(f"tenants[{i}].active must be true or false")
        try:
            records.append(
                TenantRecord(
                    _str(t, "tenant_id", f"tenants[{i}]", _REF),
                    _str(t, "connected_account_id", f"tenants[{i}]", _ACCT),
                    _str(t, "display_name", f"tenants[{i}]"),
                    active,
                )
            )
        except ValueError as exc:
            raise _fail(f"tenants[{i}]: {exc}") from None
    try:
        tenants = TenantMap(records)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    obligations = []
    for i, raw in enumerate(a.get("obligations", [])):
        o = _keys(
            raw,
            f"obligations[{i}]",
            {"obligation_id", "tenant_id", "amount_due", "due_on", "expected_deposit_on"},
            {"description"},
        )
        obligations.append(
            Obligation(
                _str(o, "obligation_id", f"obligations[{i}]", _REF),
                _str(o, "tenant_id", f"obligations[{i}]", _REF),
                _money(o["amount_due"], ccy, f"obligations[{i}].amount_due"),
                _day(o["due_on"], f"obligations[{i}].due_on"),
                _day(o["expected_deposit_on"], f"obligations[{i}].expected_deposit_on"),
                str(o.get("description", "")),
            )
        )
    fees_raw = a.get("monthly_fees", {})
    late_raw = a.get("late_repayments", {})
    if not isinstance(fees_raw, Mapping) or not isinstance(late_raw, Mapping):
        raise _fail("monthly_fees and late_repayments must be objects")
    known = set(tenants.tenant_ids())
    for tid in (*fees_raw, *late_raw):
        if tid not in known:
            raise _fail(f"{tid} is not a tenant in this setup")
    late: dict[str, int] = {}
    for tid, n in late_raw.items():
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise _fail(f"late_repayments.{tid} must be a non-negative integer")
        late[tid] = n
    try:
        return PlatformSetup.create(
            config,
            tenants,
            obligations=obligations,
            monthly_fees={t: _money(v, ccy, f"monthly_fees.{t}") for t, v in fees_raw.items()},
            late_repayments=late,
        )
    except (ValueError, AwxError) as exc:
        raise _fail(f"setup: {exc}") from None


def _sent_in_flight(store: KitStore, ledger: CapitalLedger) -> dict[str, list[tuple[str, str]]]:
    """tenant_id -> (request_id, kind) of bridge or charge attempts that were sent (or may have
    been) and are neither posted in the ledger nor recorded as a definitive failure."""
    out: dict[str, list[tuple[str, str]]] = {}
    for item in store.query(SENT_PREFIX):
        rid = item.key[len(SENT_PREFIX) :]
        if ledger.entry(rid) is None and not ledger.failed(rid):
            tid = str(item.value.get("tenant_id"))
            out.setdefault(tid, []).append((rid, str(item.value.get("kind"))))
    return out


def _obligation_attempts(old: PlatformSetup, ledger: CapitalLedger, o: Obligation) -> list[str]:
    """Every bridge request_id this obligation has had (attempt 0 up to the current one)."""
    book_id = ledger.book_id
    if book_id is None:
        return []
    tenant = old.tenants.resolve(o.tenant_id)
    bk = business_key(tenant, book_id, "bridge", o.obligation_id)
    return [
        request_id_for(tenant, book_id, "bridge", o.obligation_id, attempt_step(n))
        for n in range(ledger.failures(bk) + 1)
    ]


def _check_replace(
    ctx: OperatorContext, old: PlatformSetup, new: PlatformSetup, ledger: CapitalLedger
) -> list[tuple[str, str]]:
    """Refuse a replace that would orphan money held, owed or in flight.
    Returns the queue places of dropped tenants to remove in the same write."""
    book = PlatformBook(old, ctx.platform_store)
    new_ids = set(new.tenants.tenant_ids())
    in_flight = _sent_in_flight(ctx.platform_store, ledger)
    # Tenants: capital held or owed, or any attempt still in flight, pins the tenant and account.
    pinned = (
        {h.tenant_id for h in ledger.holds()}
        | {t for t, r in ledger.receivables().items() if r.amount > 0}
        | set(in_flight)
    )
    for tid in sorted(pinned):
        if tid not in old.tenants.tenant_ids():
            continue
        if tid not in new_ids:
            raise _fail(f"{tid} has capital held, owed or in flight; it cannot be dropped")
        before = old.tenants.resolve(tid).connected_account_id
        if new.tenants.resolve(tid).connected_account_id != before:
            raise _fail(f"{tid} has capital held, owed or in flight; its account cannot change")
    # Fees: an in-flight fee charge pins the tenant's fee, which the re-proposal re-emits on the
    # same request_id.
    for tid, attempts in sorted(in_flight.items()):
        if any(kind == "fee" for _, kind in attempts) and old.monthly_fees.get(
            tid
        ) != new.monthly_fees.get(tid):
            raise _fail(
                f"{tid} has a fee charge in flight; its monthly fee cannot be dropped or changed"
            )
    # Card programs: a card issue past its first create but not yet recorded pins its
    # program's terms, which that request_id's body carries.
    for item in ctx.platform_store.query(CARD_PENDING_PREFIX):
        if item.value.get("resolved") == "failed":
            continue  # definitively refused at its first send: nothing was created
        tid, name = str(item.value["tenant_id"]), str(item.value["program"])
        ref = item.key[len(CARD_PENDING_PREFIX) + len(tid) + 1 :]
        if ctx.store_for(tid).get(card_ref_key(ref)) is not None:
            continue  # recorded: resolved
        if old.config.card_programs.get(name) != new.config.card_programs.get(name):
            raise _fail(f"card program {name} has a card issue unresolved; its terms cannot change")
    # Obligations: a hold, a queued place or a ledger entry pins the obligation's terms.
    for oid, o in old.obligations.items():
        n = new.obligations.get(oid)
        if n is not None and (n.tenant_id, n.amount_due) == (o.tenant_id, o.amount_due):
            continue
        if n is None and o.tenant_id not in new_ids and o.tenant_id not in pinned:
            continue  # dropped with its tenant; only its queue place is left (removed below)
        rids = _obligation_attempts(old, ledger, o)
        busy = any(ledger.hold(r) or ledger.entry(r) for r in rids)
        if busy or book.queue_entry((o.tenant_id, oid)) is not None:
            what = "dropped" if n is None else "given another tenant or amount"
            raise _fail(
                f"obligation {oid} has a bridge held, queued or posted; it cannot be {what}"
            )
    dropped = sorted(set(old.tenants.tenant_ids()) - new_ids)
    return [
        (item.value["tenant_id"], item.value["obligation_id"])
        for tid in dropped
        for item in ctx.platform_store.query(queue_key(tid, ""))
        if not item.value.get("removed")
    ]


def _seed_setup(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    setup = parse_setup(args)
    old_item = ctx.platform_store.get(SETUP_KEY)
    old_version = None if old_item is None else old_item.version
    old = None if old_item is None else decode_setup(old_item.value)
    replacing = require_fresh_or_replace(old is not None, args, "the kit 5 platform setup")
    for record in setup.tenants.records():
        _check_account(ctx, record.connected_account_id, record.tenant_id)
    ledger = CapitalLedger(ctx.platform_store, setup.config.reserve_floor)
    book_id = ledger.book_id
    dropped_queue: list[tuple[str, str]] = []
    if replacing and old is not None:
        if old.config.currency != setup.config.currency:
            raise _fail("a replace may not change the platform currency")
        dropped_queue = _check_replace(ctx, old, setup, ledger)
    # Everything is validated above; the setup is one item written with a versioned put.
    # A concurrent writer is a refusal, never a raw VersionConflict.
    try:
        ctx.platform_store.put(SETUP_KEY, encode_setup(setup), expected_version=old_version)
    except VersionConflict:
        raise _fail("the platform setup changed while this command ran; run it again") from None
    book = PlatformBook(setup, ctx.platform_store)
    left = 0
    for key in dropped_queue:  # a dropped tenant's queued shortfalls go with it (no money held)
        try:
            book.drop_candidate(key)
        except RuntimeError:
            # Harmless if left: the ranking pool treats a tenant missing from the setup as
            # inactive. Reported so the operator can run the replace again.
            left += 1
    return {
        "written": SETUP_KEY,
        "queue_places_dropped": len(dropped_queue) - left,
        "queue_places_left": left,
        "replaced": replacing,
        "tenants": list(setup.tenants.tenant_ids()),
        "obligations": len(setup.obligations),
        "card_programs": sorted(setup.config.card_programs),
        "book_id": ledger.book_id,
        "book_id_kept": book_id == ledger.book_id,
    }


def _fund_capital(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    a = _keys(args, "args", {"key", "amount"})
    key = _str(a, "key", "args", _REF)
    setup = load_platform_setup(ctx.platform_store)
    if setup is None:
        raise _fail("seed the platform setup first (seed_platform_setup)")
    amount = _money(a["amount"], setup.config.currency, "amount")
    ledger = CapitalLedger(ctx.platform_store, setup.config.reserve_floor)
    already = ledger.entry(f"capital:{key}") is not None  # decided before the call
    try:
        entry = ledger.fund(f"capital:{key}", amount, at=ctx.now)
    except LedgerError as exc:
        raise _fail(f"capital {key}: {exc}") from None
    return {
        "key": key,
        "amount": str(entry.amount),
        "already_recorded": already,
        "ledger_cash": str(ledger.cash),
        "book_id": ledger.book_id,
        "note": "ledger only: deposit the same amount into the platform wallet separately",
    }


OPERATOR_COMMANDS: tuple[OperatorCommand, ...] = (
    OperatorCommand(
        "seed_platform_setup",
        "Write kit 5's platform setup (policy, programs, tenant map, obligations, fees); "
        "checks each connected account",
        _seed_setup,
        once=True,
    ),
    OperatorCommand(
        "fund_capital",
        "Record platform bridge capital in the ledger under a key (ledger only; no money moves)",
        _fund_capital,
    ),
)
