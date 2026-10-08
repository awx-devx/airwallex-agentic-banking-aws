"""Kit 8 operator commands: how seller state reaches a deployed kit.

A deployed kit runs no setup code, and every kit 8 action needs state the model never
supplies: each seller's book and profile, each cycle's figures, the order book and the carrier
evidence. These commands write that state into the seller's own store. They are not tools,
workflow actions or FUNCTIONS; the agent never reaches them. They move no money and never call
TOOLS or SPECS.

- `seed_seller` (once): a seller's book (book id minted once, rule M2) and profile, after
  checking through a platform-scope `GET /api/v1/accounts/{id}` that the connected account
  exists, is ACTIVE and is labelled `poc-i8-`. One account binds to one seller: the platform
  store's `seller-accounts` item (account to seller map) is updated before the seller's
  records, and a replace never moves a seller to another account.
- `load_cycle` (feed, key: cycle_id): entitlement, refunds and trailing volume for one cycle.
- `load_orders` (feed, key: order_id and as_of): order rows for the seller's order book. A newer
  `as_of` updates a row (an order delivered or refunded later); the same `as_of` with other
  content, or an older `as_of`, is refused.
- `load_carrier_events` (feed, key: event_id): carrier evidence, stored raw as untrusted data.

Every feed is idempotent: the same input again is a no-op, different content under a loaded key
is refused. A refused command writes nothing: every batch is validated whole
first, and every refusal is an OperatorCommandError.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import fields
from decimal import Decimal, InvalidOperation
from typing import Any

from awx_common import Money, ReadRequest, canonical_json
from awx_common.errors import AwxError, NotFound
from awx_common.operator_commands import (
    OperatorCommand,
    OperatorCommandError,
    OperatorContext,
    require_fresh_or_replace,
)
from awx_common.store import KitStore, VersionConflict

from . import store as st
from .evidence import Order, parse_carrier_event

LABEL = "poc-i8-"
_SELLER = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ACCOUNT = re.compile(r"^acct_[A-Za-z0-9_-]{6,60}$")
_PLACEHOLDER = re.compile(r"(?i)(placeholder|example|changeme|<|>|^poc-)")
_CCY = re.compile(r"^[A-Z]{3}$")


def _strict(args: Mapping[str, Any], required: set[str], optional: set[str] = frozenset()):
    extra = set(args) - required - optional
    missing = required - set(args)
    if extra or missing:
        raise OperatorCommandError(
            f"arguments: missing {sorted(missing)}, unexpected {sorted(extra)}"
        )


def _seller(args: Mapping[str, Any]) -> str:
    seller = args.get("seller")
    if not isinstance(seller, str) or not _SELLER.match(seller):
        raise OperatorCommandError("seller must be the seller's tenant id (snake_case)")
    return seller


def _money(value: Any, what: str, currency: str) -> Money:
    if not isinstance(value, Mapping) or set(value) != {"amount", "currency"}:
        raise OperatorCommandError(f"{what} must be {{amount, currency}}")
    if value["currency"] != currency:
        raise OperatorCommandError(f"{what} is {value['currency']}, the seller uses {currency}")
    if not isinstance(value["amount"], str):
        raise OperatorCommandError(f"{what}.amount must be a decimal string, never a number")
    try:
        money = Money.of(value["amount"], currency)
    except (ValueError, InvalidOperation) as exc:
        raise OperatorCommandError(f"{what}.amount is not a valid amount: {exc}") from None
    if money.is_negative():
        raise OperatorCommandError(f"{what} must not be negative")
    return money


def _seeded(kstore: KitStore) -> st.SellerProfile:
    try:
        st.book_id(kstore)
        return st.profile(kstore)
    except (AwxError, ValueError, KeyError) as exc:  # NotFound, or a store missing a piece
        raise OperatorCommandError(f"seed the seller first (seed_seller): {exc}") from None


def _check_account(ctx: OperatorContext, account: str) -> str:
    """The account exists, is ACTIVE and carries the kit's label. Returns its label."""
    if not _ACCOUNT.match(account) or _PLACEHOLDER.search(account.removeprefix("acct_")):
        raise OperatorCommandError(f"{account!r} is not a real connected account id")
    if ctx.read is None:
        raise OperatorCommandError("seed_seller checks the account at Airwallex; run it online")
    try:
        body = ctx.read(ReadRequest(path=f"/api/v1/accounts/{account}"))
    except NotFound:
        raise OperatorCommandError(f"Airwallex has no account {account}") from None
    except AwxError as exc:
        raise OperatorCommandError(f"cannot read account {account}: {exc}") from None
    if body.get("id") != account:
        raise OperatorCommandError(f"Airwallex returned another object for {account}")
    business = (body.get("account_details") or {}).get("business_details") or {}
    names = [body.get("nickname"), business.get("business_name")]
    label = next((n for n in names if isinstance(n, str) and n.startswith(LABEL)), None)
    if label is None:
        raise OperatorCommandError(f"account {account} is not labelled {LABEL}")
    if body.get("status") != "ACTIVE":
        raise OperatorCommandError(f"account {account} is {body.get('status')}, not ACTIVE")
    return label


INDEX = "seller-accounts"  # one platform item: {"accounts": {acct: seller}}


def bound_accounts(platform_store: KitStore) -> dict[str, str]:
    item = platform_store.get(INDEX)
    return {} if item is None else dict(item.value["accounts"])


def _bind_account(ctx: OperatorContext, seller: str, account: str) -> None:
    """One account binds to one seller. The whole account->seller map is one platform-store
    item, changed by one versioned read-modify-write as seed_seller's first contested write,
    before any seller record. Two concurrent seeds conflict on that item, and the loser has
    written nothing. A bound account is refused to anyone else; a seller is never bound to a
    second account; the same seller on the same account is not a conflict (so a re-run after a
    crash between this write and the seller records completes)."""
    item = ctx.platform_store.get(INDEX)
    accounts = {} if item is None else dict(item.value["accounts"])
    bound = accounts.get(account)
    if bound is not None and bound != seller:
        raise OperatorCommandError(f"account {account} is already bound to seller {bound}")
    other = next((a for a, s in accounts.items() if s == seller and a != account), None)
    if other is not None:
        raise OperatorCommandError(
            f"seller {seller} is bound to account {other}; a seller never moves account"
        )
    if bound == seller:
        return
    accounts[account] = seller
    try:
        ctx.platform_store.put(
            INDEX,
            {"accounts": dict(sorted(accounts.items()))},
            expected_version=None if item is None else item.version,
        )
    except VersionConflict:
        raise OperatorCommandError(
            "another seed changed the account index at the same time; nothing was written, retry"
        ) from None


# seed_seller -------------------------------------------------------------------------------


def seed_seller(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    _strict(
        args,
        {"seller", "connected_account_id", "currency", "tenure_days", "trailing_refund_rate"},
        {"replace"},
    )
    seller = _seller(args)
    currency, days, rate = args["currency"], args["tenure_days"], args["trailing_refund_rate"]
    if not isinstance(currency, str) or not _CCY.match(currency):
        raise OperatorCommandError("currency must be a 3-letter code")
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise OperatorCommandError("tenure_days must be a non-negative integer")
    if not isinstance(rate, str):
        raise OperatorCommandError("trailing_refund_rate must be a decimal string")
    try:
        rate_d = Decimal(rate)
    except InvalidOperation:
        raise OperatorCommandError("trailing_refund_rate is not a decimal") from None
    if not Decimal(0) <= rate_d <= Decimal(1):
        raise OperatorCommandError("trailing_refund_rate must be within [0, 1]")
    account = args["connected_account_id"]
    if not isinstance(account, str):
        raise OperatorCommandError("connected_account_id must be a string")
    label = _check_account(ctx, account)
    kstore = ctx.store_for(seller)
    existing = kstore.get("profile")
    replacing = require_fresh_or_replace(existing is not None, args, f"seller {seller}")
    if replacing and existing.value["currency"] != currency:
        raise OperatorCommandError("a replace cannot change a seeded seller's currency")
    acct = kstore.get("account")
    if acct is not None and acct.value["connected_account_id"] != account:
        raise OperatorCommandError(
            f"seller {seller} is seeded on {acct.value['connected_account_id']}; a replace "
            "never moves a seller to another account (seed a new seller instead)"
        )
    _bind_account(ctx, seller, account)  # before any seller record
    try:
        book = kstore.get("book")
        book_id = st.create_book(kstore) if book is None else book.value["book_id"]  # M2
        prof = st.SellerProfile(seller, currency, days, rate_d)
        value = {f.name: getattr(prof, f.name) for f in fields(prof)}
        kstore.put(
            "profile", value, expected_version=None if existing is None else existing.version
        )
        if acct is None:
            kstore.put(
                "account",
                {"connected_account_id": account, "label": label},
                expected_version=None,
            )
    except VersionConflict:
        raise OperatorCommandError(f"seller {seller} changed while seeding; retry") from None
    return {"seller": seller, "book_id": book_id, "replaced": replacing, "account": account}


# feeds --------------------------------------------------------------------------------------


def _keyed_status(kstore: KitStore, key: str, value: Mapping[str, Any]) -> str:
    """Idempotent per key: 'loaded' (to write), 'unchanged', or refused on different content."""
    existing = kstore.get(key)
    if existing is not None:
        if canonical_json(existing.value) == canonical_json(dict(value)):
            return "unchanged"
        raise OperatorCommandError(f"{key} is already loaded with different content")
    return "loaded"


def _create(kstore: KitStore, key: str, value: Mapping[str, Any]) -> None:
    try:
        kstore.put(key, dict(value), expected_version=None)
    except VersionConflict:
        raise OperatorCommandError(f"{key} was written concurrently; retry") from None


def load_cycle(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    _strict(args, {"seller", "cycle_id", "entitlement", "refunds", "volume_base"})
    seller = _seller(args)
    cycle_id = args["cycle_id"]
    if not isinstance(cycle_id, str) or not _ID.match(cycle_id):
        raise OperatorCommandError("cycle_id is missing or malformed")
    kstore = ctx.store_for(seller)
    ccy = _seeded(kstore).currency
    data = st.CycleData(
        cycle_id,
        _money(args["entitlement"], "entitlement", ccy),
        _money(args["refunds"], "refunds", ccy),
        _money(args["volume_base"], "volume_base", ccy),
    )
    value = {f.name: getattr(data, f.name) for f in fields(data)}
    status = _keyed_status(kstore, f"cycle/{cycle_id}", value)
    if status == "loaded":
        _create(kstore, f"cycle/{cycle_id}", value)
    return {"seller": seller, "cycle_id": cycle_id, "status": status}


def load_orders(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    """The whole batch is validated (every row, duplicate order_ids, every as_of comparison)
    before one versioned put of the order book, which carries each row's as_of."""
    _strict(args, {"seller", "as_of", "orders"})
    seller = _seller(args)
    as_of = args["as_of"]
    if not isinstance(as_of, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", as_of):
        raise OperatorCommandError("as_of must be an ISO UTC timestamp like 2026-10-06T00:00:00Z")
    rows = args["orders"]
    if not isinstance(rows, list) or not rows or len(rows) > 5000:
        raise OperatorCommandError("orders must be a non-empty list (at most 5000)")
    kstore = ctx.store_for(seller)
    ccy = _seeded(kstore).currency
    item = kstore.get("orders")
    book = {r["order_id"]: dict(r) for r in (item.value["orders"] if item else ())}
    batch: dict[str, Order] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "order_id",
            "amount",
            "refunded",
            "delivered",
        }:
            raise OperatorCommandError("each order is {order_id, amount, refunded, delivered}")
        oid = row["order_id"]
        if not isinstance(oid, str) or not _ID.match(oid) or not isinstance(row["delivered"], bool):
            raise OperatorCommandError("order_id malformed or delivered not a bool")
        if oid in batch:
            raise OperatorCommandError(f"order {oid} appears twice in one batch")
        try:
            batch[oid] = Order(
                oid,
                seller,
                _money(row["amount"], "amount", ccy),
                _money(row["refunded"], "refunded", ccy),
                row["delivered"],
            )
        except ValueError as exc:
            raise OperatorCommandError(f"order {oid}: {exc}") from None
    counts = {"loaded": 0, "updated": 0, "unchanged": 0}
    for oid, order in batch.items():  # every comparison before any write
        record = {**st._record(order), "as_of": as_of}
        seen = book.get(oid)
        if seen is None:
            counts["loaded"] += 1
        elif seen.get("as_of") == as_of:
            if canonical_json(seen) != canonical_json(record):
                raise OperatorCommandError(
                    f"order {oid} at {as_of} is already loaded with different content"
                )
            counts["unchanged"] += 1
            continue
        elif seen.get("as_of") is None or seen["as_of"] > as_of:
            raise OperatorCommandError(f"order {oid} already has a newer row ({seen.get('as_of')})")
        else:
            counts["updated"] += 1
        book[oid] = record
    if counts["loaded"] or counts["updated"]:
        try:
            kstore.put(
                "orders",
                {"orders": [book[k] for k in sorted(book)]},
                expected_version=None if item is None else item.version,
            )
        except VersionConflict:
            raise OperatorCommandError("the order book changed while loading; retry") from None
    return {"seller": seller, "as_of": as_of, **counts}


def load_carrier_events(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    _strict(args, {"seller", "events"})
    seller = _seller(args)
    events = args["events"]
    if not isinstance(events, list) or not events or len(events) > 100:
        raise OperatorCommandError("events must be a non-empty list (at most 100)")
    kstore = ctx.store_for(seller)
    _seeded(kstore)
    out: dict[str, list[str]] = {"loaded": [], "unchanged": []}
    writes: list[tuple[str, Mapping[str, Any]]] = []
    for raw in events:  # validate the whole batch first
        try:
            event = parse_carrier_event(raw)  # strict parse; raw text stays untrusted data
        except AwxError as exc:
            raise OperatorCommandError(f"carrier event refused: {exc}") from None
        if event.seller != seller:
            raise OperatorCommandError(
                f"event {event.event_id} names seller {event.seller}, "
                f"not {seller}; file it under its own seller"
            )
        if event.event_id in out["loaded"] + out["unchanged"]:
            raise OperatorCommandError(f"event {event.event_id} appears twice in one batch")
        key = f"evidence/{event.event_id}"
        status = _keyed_status(kstore, key, raw)
        out[status].append(event.event_id)
        if status == "loaded":
            writes.append((key, raw))
    for key, raw in writes:
        _create(kstore, key, raw)
    return {"seller": seller, **out}


OPERATOR_COMMANDS: tuple[OperatorCommand, ...] = (
    OperatorCommand(
        "seed_seller",
        "Seed one seller's book (book id minted once) and profile; checks its poc-i8- account",
        seed_seller,
        once=True,
    ),
    OperatorCommand("load_cycle", "Load one cycle's entitlement, refunds and volume", load_cycle),
    OperatorCommand("load_orders", "Load or update order rows by order_id and as_of", load_orders),
    OperatorCommand(
        "load_carrier_events", "Load carrier evidence events by event_id", load_carrier_events
    ),
)
