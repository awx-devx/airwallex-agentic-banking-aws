"""Watches on `not_landed` decisions.

A person's `not_landed` decision (resolve_in_flight_money) moves a payout to payable, or a
charge attempt to FAILED, on Airwallex's word rather than Airwallex's own failure state. If the
original request lands after all, the seller would be paid or charged twice.

- The watch is the resolution record itself, written BEFORE the ledger move (resolve.py). A
  crash between the two writes leaves the record and the money still in flight; nothing can
  lose the watch.
- reconcile_seller_money reads every open watch back, read-only, with no window limit, and
  maps what it finds through the kit's own read_outcome:
  - SETTLED: post the late landing and close the watch as `found`, flagging a person.
  - FAILED (including CANCELLED with a failure block): close it as `confirmed_failed`.
  - PENDING, SUSPENDED, CANCELLED without failure, two objects, a mismatch: keep it open,
    post nothing, and flag a person.
- Prevention, not just detection: while a watch is not yet `confirmed`, settle_seller_cycle
  (payout watches) and recover_shortfall (charge watches) BLOCK, and settlement netting
  excludes a watched charge's amount. A watch becomes `confirmed` only when a reconcile at
  least CONFIRM_AFTER past the decision reads it empty AND a scan of movements to or from the
  seller since the first send finds no unexplained one of the same amount. Confirmed watches
  stay open and are still read on every reconcile.
- The counterparty scan pages the list endpoint (`page_num`/`page_size`) and matches
  `destination`/`source` client-side, because neither the fake nor (as far as the spine's
  sandbox probes go) the sandbox filters these lists by counterparty. It shares the list's
  visibility: an object still invisible to `?request_id=` is invisible to the scan too, which
  is why the second read waits 24 h.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from awx_common import Money, ReadRequest, TenantContext
from awx_common.errors import AwxError
from awx_common.store import KitStore
from awx_common.workflow import ReconcileUndetermined

from . import store as st
from .operations import (
    CHARGES,
    DEFINITIVE_FAILURES,
    TRANSFERS,
    Outcome,
    build_payout_op,
    build_recovery_op,
    find_by_request_id,
    payout_request_id,
    read_outcome,
    recovery_request_id,
)

Read = Callable[[ReadRequest], Mapping[str, Any]]
CONFIRM_AFTER = timedelta(hours=24)
SCAN_PAGE_SIZE = 100
SCAN_MAX_PAGES = 50


def watches(kstore: KitStore) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        i.value for i in kstore.query("resolution/") if i.value["resolution"] == "not_landed"
    )


def open_watches(kstore: KitStore) -> tuple[Mapping[str, Any], ...]:
    return tuple(w for w in watches(kstore) if w.get("watch", "open") == "open")


def unconfirmed(kstore: KitStore, kind: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(w for w in open_watches(kstore) if w["kind"] == kind and not w.get("confirmed_at"))


def decision_applied(kstore: KitStore, w: Mapping[str, Any]) -> str:
    """'missing' (crashed before the ledger move: the money is still in flight), 'applied'
    (the not_landed move is on the ledger) or 'superseded' (the ledger shows it settled)."""
    state = st.ledger_state(kstore, st.profile(kstore).seller, st.profile(kstore).currency)
    if w["kind"] == "payout":
        prior = st.payout_outcome(state, w["ref"])
        if prior is None:
            return "missing"
        return "applied" if prior in DEFINITIVE_FAILURES else "superseded"
    found = [a for a in st.attempts_in(state) if a.attempt == int(w["ref"])]
    if not found or found[0].outcome in st.PENDING_RECOVERY:
        return "missing"
    return "superseded" if found[0].outcome == "SETTLED" else "applied"


def _op(kstore: KitStore, tenant: TenantContext, watch: Mapping[str, Any]):
    book = st.book_id(kstore)
    if watch["kind"] == "payout":
        state = st.ledger_state(kstore, tenant.tenant_id, st.profile(kstore).currency)
        settlement = st.settlement(state, watch["ref"])
        op = build_payout_op(settlement, tenant, book_id=book, intent_hash="0" * 64)
    else:
        amount: Money = watch["amount"]
        op = build_recovery_op(
            int(watch["ref"]), amount, tenant, book_id=book, intent_hash="0" * 64
        )
    if op.request_id != watch["request_id"]:  # never query the wrong id
        raise ReconcileUndetermined("rebuilt request_id differs from the watched one")
    return op


def look(
    kstore: KitStore, tenant: TenantContext, read: Read | None, watch: Mapping[str, Any]
) -> tuple[str, str]:
    """(state, provider status). state is 'empty', 'settled', 'failed', 'open' (found but
    not final, or needs a person) or 'undetermined'."""
    if read is None:
        return "undetermined", "no platform read"
    try:
        op = _op(kstore, tenant, watch)
        found = find_by_request_id(op, read)
    except (ReconcileUndetermined, AwxError) as exc:
        return "undetermined", str(exc)[:80]
    if found is None:
        return "empty", ""
    status = str(found.get("status"))
    outcome = read_outcome(found)
    if outcome is Outcome.SETTLED:
        return "settled", status
    if outcome is Outcome.FAILED:
        return "failed", status
    return "open", status  # IN_FLIGHT, NEEDS_PERSON, UNKNOWN: not definitive (M1)


def _known_request_ids(kstore: KitStore, seller: str, skip: str) -> set[str]:
    book = st.book_id(kstore)
    state = st.ledger_state(kstore, seller, st.profile(kstore).currency)
    rids = {
        payout_request_id(book, seller, key.removeprefix("settle:"))
        for key in state.postings
        if key.startswith("settle:")
    }
    rids |= {recovery_request_id(book, seller, a.attempt) for a in st.attempts_in(state)}
    rids.discard(skip)
    return rids


def _created(obj: Mapping[str, Any]) -> datetime | None:
    try:
        return datetime.strptime(str(obj.get("created_at")), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None  # caller treats None as a possible match


def counterparty_match(
    kstore: KitStore,
    tenant: TenantContext,
    read: Read | None,
    watch: Mapping[str, Any],
    now: datetime,
) -> str | None:
    """A reason to BLOCK, or None. Scans movements to (payout) or from (charge) the seller
    since the watched first send for one that is not FAILED/CANCELLED, has the watched amount
    and is not one of this book's own other request_ids."""
    if read is None:
        return "no platform read for the counterparty scan"
    path, party = (TRANSFERS, "destination") if watch["kind"] == "payout" else (CHARGES, "source")
    known = _known_request_ids(kstore, tenant.tenant_id, watch["request_id"])
    amount: Money = watch["amount"]
    since: datetime = watch["first_sent_at"]
    for page in range(SCAN_MAX_PAGES):
        try:
            body = read(
                ReadRequest(
                    path=path, query={"page_num": str(page), "page_size": str(SCAN_PAGE_SIZE)}
                )
            )
        except AwxError as exc:
            return f"counterparty scan failed ({type(exc).__name__})"
        items = body.get("items") if isinstance(body, Mapping) else None
        if not isinstance(items, list):
            return "counterparty scan reply has no items list"
        for o in items:
            if not isinstance(o, Mapping) or o.get(party) != tenant.connected_account_id:
                continue
            if o.get("status") in ("FAILED", "CANCELLED") or o.get("request_id") in known:
                continue
            created = _created(o)
            # An unreadable or missing created_at is treated as
            # inside the window (a possible match), never as clean.
            if created is not None and not since - timedelta(minutes=5) <= created <= now:
                continue
            try:
                same = Decimal(str(o.get("amount"))) == amount.amount
            except ArithmeticError:
                same = True  # unreadable amount: treat as a match, never as clean
            if same and o.get("currency") == amount.currency:
                return (
                    f"Airwallex lists {o.get('id')} ({o.get('status')}, {amount}) to this "
                    f"seller since {watch['request_id']} was first sent"
                )
        more = body.get("has_more")
        if more is False:
            return None
        if not isinstance(more, bool) and len(items) < SCAN_PAGE_SIZE:
            return None  # no readable has_more, but a short page: it was the last one
        # has_more True, or unreadable on a full page: keep paging (fail closed at the cap)
    return "counterparty scan did not finish"


def gate(
    kstore: KitStore, tenant: TenantContext, read: Read | None, kind: str, now: datetime
) -> str | None:
    """Why settle (payout watches) or recover (charge watches) must not move money now."""
    for w in open_watches(kstore):
        if w["kind"] != kind:
            continue
        seen, status = look(kstore, tenant, read, w)
        who = f"{kind} {w['ref']} ruled not_landed by proposal {w['owner_proposal_id']}"
        if seen in ("settled", "open", "undetermined"):
            shown = status or seen
            return f"{who} shows at Airwallex ({shown}); run reconcile_seller_money first"
        if not w.get("confirmed_at"):
            if now - w["decided_at"] < CONFIRM_AFTER:
                if reason := counterparty_match(kstore, tenant, read, w, now):
                    return f"{who}: {reason}"
            return (
                f"{who} is not confirmed yet: a reconcile at least 24 h after "
                f"{w['decided_at']:%Y-%m-%d %H:%M}Z must read it empty first"
            )
    return None


def shows_hold(kstore: KitStore, tenant: TenantContext, read: Read | None, kind: str) -> str | None:
    """The look half of gate, for every open watch of `kind`, confirmed or not: BLOCK while
    the watched original shows at Airwallex as anything but empty or its own failure."""
    for w in open_watches(kstore):
        if w["kind"] != kind:
            continue
        seen, status = look(kstore, tenant, read, w)
        if seen in ("settled", "open", "undetermined"):
            return (
                f"{kind} {w['ref']} ruled not_landed by proposal {w['owner_proposal_id']} "
                f"shows at Airwallex ({status or seen}); run reconcile_seller_money first"
            )
    return None


def unconfirmed_charge_amount(kstore: KitStore) -> Money | None:
    total = None
    for w in unconfirmed(kstore, "charge"):
        total = w["amount"] if total is None else total + w["amount"]
    return total


def describe(ws: Iterable[Mapping[str, Any]]) -> str:
    return "; ".join(
        f"{w['kind']} {w['ref']} ({w['amount']}, request_id {w['request_id']}) was ruled "
        f"not_landed by proposal {w['owner_proposal_id']}"
        f"{'' if w.get('confirmed_at') else ', not yet confirmed'}"
        for w in ws
    )


def update(kstore: KitStore, w: Mapping[str, Any], **changes: Any) -> None:
    key = f"resolution/{w['kind']}/{w['ref']}"
    item = kstore.get(key)
    kstore.put(key, {**item.value, **changes}, expected_version=item.version)


def close(kstore: KitStore, w: Mapping[str, Any], outcome: str) -> None:
    update(kstore, w, watch=outcome)
