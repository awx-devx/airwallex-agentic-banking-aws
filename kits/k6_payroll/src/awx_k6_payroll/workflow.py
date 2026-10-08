"""Kit 6 workflow specs: one parent run per payroll batch, one child run per employer.

Parent (`run_payroll`) runs as the platform (rule 3). `validate` parses and prices the batch;
`policy` reads nothing, so it can never sum balances across tenants, and its hashed `computed`
lists every child intent with that employer's read-free amounts (payouts, SWIFT fees, buy
amounts, platform fee) and, for each conversion, the stored rate lock (quote_id, exact sell
amount, expiry) read from the platform store. Its tier is SINGLE, or DUAL when any one employer's
payroll in one currency reaches the threshold. A person approves the whole batch once.

`fan_out` yields one child intent per employer; `child_tenant` names that employer's tenant,
which the runner resolves through the TenantResolver (unknown employers are refused).

Child (`employer_payroll`) runs under exactly one employer's tenant. Its policy reads only
that employer's wallet and returns AUTO (funded) or BLOCKED (shortfall, escalate). Its hashed
`computed` is empty and its tier depends only on its own reads, so another employer's deposit
cannot change it, and rule 1's re-run is stable unless this employer's own funding moved.
Steps: snapshot (opening balances and the deposit ids they already include, into ctx.store),
validate and create missing beneficiaries, convert each payout currency (fees included) at
the approved locked quote, pay each contractor (sandbox:
FinalitySpec.sandbox_advance moves each payout SENT then PAID), then collect the platform fee
only once every payout is PAID. The postcondition checks that every payout is PAID and that
every balance delta landed in this employer's wallet and equals what this run did, from the
objects it reads back.

Why the parent binds a stored lock and never a live rate: rule 1 re-runs policy before the
first step and fails closed if the hash moved, and a live rate moves between proposal and
execution. A quote taken by the AUTO `lock_rates` action and kept in the platform store reads
the same at proposal and at rule 1's re-run, so the approval binds every amount that leaves an
account (rule M3) and a market move cannot make the batch STALE. Lock expiry is judged in the
child, so the parent's hash never moves with the clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from awx_common import (
    Money,
    Operation,
    OperationResult,
    OperationStatus,
    ReadRequest,
    RiskTier,
    TenantContext,
    bind_operation,
    bind_platform_operation,
    canonical_json,
)
from awx_common.errors import NotFound, TenantScopeViolation
from awx_common.ops import DEDUP_RESEND_MARGIN
from awx_common.sandbox import make_transfer_advance
from awx_common.workflow import (
    ApproverScope,
    FinalitySpec,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    ReconcileUndetermined,
    StepContext,
    StepSpec,
)

from . import ids
from . import operations as ops
from .books import (
    Keys,
    ensure_book,
    needs_person,
    read_book,
    read_claim,
    read_lock,
    record_failure,
    record_needs_person,
    record_sent,
    release_claim,
    sent,
    take_claim,
    write_lock,
)
from .model import (
    KIT,
    PayrollInputError,
    employer_tenant_id,
    parse_batch,
    parse_employer,
    parse_money,
    parse_pay_period,
)
from .model import _code as parse_employer_code
from .pricing import (
    DUAL_THRESHOLDS,
    RateUnusable,
    available_balance,
    payout_cost,
    plan_funding,
    price_employer,
)

APPROVAL_TTL = timedelta(hours=8)
# A failed transfer always READS as CANCELLED (FAKE-FIDELITY); FAILED is only ever a
# transition reply, kept here so an unexpected read still ends the poll.
TRANSFER_TERMINAL = frozenset({"PAID", "CANCELLED", "FAILED"})  # SENT is in flight, never final
TRANSFER_SUCCESS = frozenset({"PAID"})
DEPOSIT_WINDOW = timedelta(days=1)
SNAPSHOT_TRIES = 3
# Rule M1: Airwallex deduplicates a request_id for about 7 days. An in-flight money operation
# is re-emitted only inside this margin; past it a person decides and nothing is sent.
DEDUP_SAFE_MARGIN = DEDUP_RESEND_MARGIN  # the spine's own deduplication margin, 6 days
MAX_BENEFICIARY_PAGES = 20
MAX_TXN_PAGES = 20
TXN_PAGE_SIZE = 100

_ID = "[A-Za-z0-9_-]+"
CHILD_READS = (
    r"^/api/v1/balances/current$",
    r"^/api/v1/fx/rates/current$",
    r"^/api/v1/beneficiaries$",
    r"^/api/v1/transfers$",
    rf"^/api/v1/transfers/{_ID}$",
    r"^/api/v1/fx/conversions$",
    rf"^/api/v1/fx/conversions/{_ID}$",
    r"^/api/v1/financial_transactions$",
)
# Charges are platform objects named by `source` (FAKE-FIDELITY row 103): read headerless.
PLATFORM_READS = (r"^/api/v1/charges$", rf"^/api/v1/charges/{_ID}$")
# A lock must have at least this long left when the batch is proposed (rule M3).
LOCK_MIN_REMAINING = timedelta(minutes=2)
LOCK_TTL = timedelta(minutes=15)

Read = Callable[[ReadRequest], Mapping[str, Any]]


class QuoteAboveApprovedCap(Exception):
    """The FX quote would sell more than the cap. Nothing is converted."""


class QuoteUnusable(Exception):
    """The quote is expired, for other amounts or currencies, or has no sell amount."""


class NeedsPerson(Exception):
    """An in-flight money operation is past the deduplication margin: a person decides."""


class MissingPrerequisite(Exception):
    """A step needs an object an earlier step should have produced."""


class RequestIdBoundElsewhere(MissingPrerequisite):
    """An object under one of our request_ids does not match our operation.
    Money in flight: no new attempt; a person decides."""


class RunClaimed(Exception):
    """Another proposal's run of this employer and pay period is in flight; nothing sent."""


class SnapshotUnstable(Exception):
    """Transactions kept landing during the opening balance read; refuse rather than guess."""


# ---------------------------------------------------------------- tenant scope


# The tenants that may run kit 6's platform-level actions (batch, lock, claim release) and its
# read tools: the platform itself (local runs) and the payroll operator tenant that the tool
# plane's callers carry (kit 6 is a connected-account kit, so a caller needs a tenant claim;
# the tenants table row for it has no connected_account_id). An employer tenant never can.
OPERATOR_TENANTS = frozenset({"platform", "k6:operator"})


def require_operator(tenant: TenantContext) -> None:
    if tenant.connected_account_id is not None or tenant.tenant_id not in OPERATOR_TENANTS:
        raise TenantScopeViolation(
            "kit 6 batch, lock, release and read tools run as the payroll operator, not an employer"
        )


def child_tenant(child_intent: Mapping[str, Any]) -> str:
    return employer_tenant_id(child_intent["employer"])


def require_employer_tenant(tenant: TenantContext, intent: Mapping[str, Any]) -> str:
    """Defence in depth: the resolved tenant must be this intent's employer."""
    if tenant.tenant_id != child_tenant(intent):
        raise TenantScopeViolation("child run tenant is not the employer the intent names")
    acct = tenant.connected_account_id
    if not acct or not acct.startswith("acct_"):
        raise TenantScopeViolation("employer tenant has no connected account")
    return acct


def get(read: Read, path: str, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
    return read(ReadRequest(path=path, query=dict(query or {})))


def list_items(body: Any) -> list[Mapping[str, Any]]:
    items = body.get("items") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        raise MissingPrerequisite("list read did not return items")
    return [i for i in items if isinstance(i, Mapping)]


def one_by_request_id(read: Read, path: str, rid: str) -> Mapping[str, Any] | None:
    rows = [
        i for i in list_items(get(read, path, {"request_id": rid})) if i.get("request_id") == rid
    ]
    if len(rows) > 1:
        raise MissingPrerequisite(f"{path} lists request_id {rid} twice")
    return rows[0] if rows else None


def _object_id(row: Mapping[str, Any], *keys: str) -> str:
    for k in (*keys, "id"):
        if row.get(k):
            return str(row[k])
    raise MissingPrerequisite("object has no id")


def money_field(body: Mapping[str, Any], key: str, currency: str) -> Money:
    value = body.get(key)
    if isinstance(value, float | bool) or value is None:
        raise MissingPrerequisite(f"{key} missing or inexact")
    return Money.of(str(value), currency)


# ---------------------------------------------------------------- one employer's state


def keys_for(store: Any, intent: Mapping[str, Any], *, mint: bool = False) -> Keys | None:
    """This employer's request ids in kit 6's book. None means no book yet, so nothing has
    ever been sent under it. Only step builds mint (rule 0: policy never writes)."""
    book = ensure_book(store) if mint else read_book(store)
    if book is None:
        return None
    return Keys(book, store, intent["employer"], intent["pay_period"])


def beneficiaries_named(read: Read, nick: str) -> list[Mapping[str, Any]]:
    """Match the nickname client-side across every page: the live `nickname` filter may not
    filter (FAKE-FIDELITY). Bounded; fails closed past the bound."""
    rows: list[Mapping[str, Any]] = []
    for page in range(MAX_BENEFICIARY_PAGES):
        body = get(
            read,
            "/api/v1/beneficiaries",
            {"nickname": nick, "page_num": str(page), "page_size": "100"},
        )
        rows += [i for i in list_items(body) if i.get("nickname") == nick]
        if not body.get("has_more"):
            return rows
    raise MissingPrerequisite(f"beneficiaries has more than {MAX_BENEFICIARY_PAGES} pages")


def find_beneficiary(read: Read, k: Keys, contractor: str) -> str | None:
    nick = k.nickname(contractor)
    rows = beneficiaries_named(read, nick)
    if len(rows) > 1:
        raise MissingPrerequisite(f"two beneficiaries carry nickname {nick}; resolve by hand")
    return _object_id(rows[0], "beneficiary_id") if rows else None


# Every field kit 6 checks on an object found under one of its request_ids before reconciling
# against it. Each list covers the spine endpoint's match_fields that the kit's body carries
# (awx_spine.endpoints; tests/test_match_fields.py pins it), plus fields the kit owns: the
# reference, and a conversion's sell_amount and quote_id from its sent record. Kit-owned fields
# outside the spine's lists (`*_OPTIONAL`) are compared only when the found object carries
# them, since live echoes of reason and reference are unverified.
TRANSFER_MATCH = (
    "transfer_amount",
    "transfer_currency",
    "beneficiary_id",
    "source_currency",
    "transfer_method",
    "reason",
    "reference",
)
CONVERSION_MATCH = ("buy_currency", "sell_currency", "buy_amount", "sell_amount", "quote_id")
FEE_MATCH = ("amount", "currency", "source", "reason", "reference")
TRANSFER_OPTIONAL = frozenset({"reason", "reference"})
FEE_OPTIONAL = frozenset({"reference"})


def _same(want: Any, got: Any) -> bool:
    if got is None:
        return False
    if isinstance(want, Money):
        try:
            return Money.of(str(got), want.currency) == want
        except (ArithmeticError, ValueError):
            return False
    if isinstance(want, Decimal) or isinstance(got, Decimal):
        try:
            return Decimal(str(want)) == Decimal(str(got))
        except ArithmeticError:
            return False
    return want == got


def _check_match(
    row: Mapping[str, Any],
    rid: str,
    expected: Mapping[str, Any],
    fields: Sequence[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    """An object found under our request_id must be ours on every listed field. A field we
    cannot state (None) counts as a mismatch, so an object we cannot account for fails
    closed and its status is never adopted. An `optional` field is compared only when the
    object carries it."""
    bad = [
        f
        for f in fields
        if not (f in optional and f not in row)
        and (expected.get(f) is None or not _same(expected[f], row.get(f)))
    ]
    if bad:
        raise RequestIdBoundElsewhere(
            f"request_id {rid} names an object that does not match this payroll ({bad}); "
            "money stays in flight; a person decides"
        )


def expected_transfer(k: Keys, c: Mapping[str, Any], beneficiary_id: str | None) -> dict[str, Any]:
    """What our payout body for this contractor carries (operations.create_transfer)."""
    body = dict(ops.create_transfer(k, c, beneficiary_id or "", "0" * 64).body)
    body["beneficiary_id"] = beneficiary_id  # None when ours does not exist: a mismatch
    body["transfer_amount"] = c["amount"]
    return body


def existing_transfers(
    read: Read, k: Keys | None, intent: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    """Each contractor's payout under its CURRENT attempt's request_id, checked as ours."""
    out: dict[str, Mapping[str, Any]] = {}
    if k is None:
        return out
    for c in intent["contractors"]:
        rid = k.payout_rid(c["contractor"])
        row = one_by_request_id(read, "/api/v1/transfers", rid)
        if row is not None:
            bid = find_beneficiary(read, k, c["contractor"])
            _check_match(row, rid, expected_transfer(k, c, bid), TRANSFER_MATCH, TRANSFER_OPTIONAL)
            out[c["contractor"]] = row
    return out


def existing_conversions(
    read: Read, k: Keys | None, intent: Mapping[str, Any], store: Any = None
) -> dict[str, Mapping[str, Any]]:
    """Conversions under the current attempt's request_id, checked against what we sent. A
    conversion we have no sent record for is not ours (we record before sending)."""
    out: dict[str, Mapping[str, Any]] = {}
    if k is None:
        return out
    funding = intent["funding_currency"]
    for ccy in intent["pricing"]["by_currency"]:
        if ccy != funding:
            rid = k.conversion_rid(ccy, funding)
            row = one_by_request_id(read, "/api/v1/fx/conversions", rid)
            if row is not None:
                rec = sent(store if store is not None else k.store, rid) or {}
                need = intent["pricing"]["by_currency"][ccy]["need"]
                _check_match(
                    row,
                    rid,
                    {
                        "buy_currency": ccy,
                        "sell_currency": funding,
                        "buy_amount": need,
                        "sell_amount": rec.get("sell"),
                        "quote_id": rec.get("quote_id"),
                    },
                    CONVERSION_MATCH,
                )
                out[ccy] = row
    return out


def existing_fee(
    read_platform: Read | None,
    k: Keys | None,
    fee: Money | None = None,
    source: str | None = None,
) -> Mapping[str, Any] | None:
    """The platform fee charge, checked as ours. Policy and builds always pass `source` (the
    employer's own account) and get the full check. The read tool, in the platform context,
    cannot state `source`: it checks every other field and reports the charge as found with
    its source unchecked; it moves no money."""
    if read_platform is None:
        raise MissingPrerequisite("charges are platform objects; no platform read available")
    if k is None:
        return None
    rid = k.fee_rid()
    row = one_by_request_id(read_platform, "/api/v1/charges", rid)
    if row is not None and fee is not None:
        body = dict(ops.create_fee_charge(k, fee, "0" * 64).body)
        body.update(amount=fee, source=source)
        fields = FEE_MATCH if source is not None else tuple(f for f in FEE_MATCH if f != "source")
        _check_match(row, rid, body, fields, FEE_OPTIONAL)
    return row


def indicative_rate(read: Read, buy: Money, sell: str) -> Mapping[str, Any]:
    return get(
        read,
        "/api/v1/fx/rates/current",
        {
            "buy_currency": buy.currency,
            "sell_currency": sell,
            "buy_amount": format(buy.amount, "f"),
        },
    )


def indicative_rates(read: Read, intent: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    funding = intent["funding_currency"]
    return {
        ccy: indicative_rate(read, need["need"], funding)
        for ccy, need in sorted(intent["pricing"]["by_currency"].items())
        if ccy != funding
    }


def assess_employer(
    intent: Mapping[str, Any],
    read: Read,
    read_platform: Read | None,
    store: Any,
    source: str | None = None,
) -> dict[str, Any]:
    """Price against this employer's own wallet only. Used by the child policy and the read
    tool; `read` is already bound to the one employer. A child intent carries the parent's
    approved maximum sell per conversion (rule M3) and reads no rate; the read tool, with no
    approved maximum, prices on the indicative rate."""
    funding = intent["funding_currency"]
    pricing = intent["pricing"]
    k = keys_for(store, intent)
    balances = get(read, "/api/v1/balances/current")
    done_transfers = existing_transfers(read, k, intent)
    done_conversions = existing_conversions(read, k, intent, store)
    fee = intent["pricing"]["platform_fee"]
    fee_done = existing_fee(read_platform, k, fee, source) is not None
    done_funding = [
        payout_cost(c)
        for c in intent["contractors"]
        if c["contractor"] in done_transfers and c["amount"].currency == funding
    ]
    approved = max_sell(intent)
    plan = plan_funding(
        pricing,
        funding,
        available=available_balance(balances, funding),
        rates=None if approved is not None else indicative_rates(read, intent),
        max_sell=approved,
        done_conversions=frozenset(done_conversions),
        done_funding_payouts=done_funding,
        fee_done=fee_done,
    )
    return {
        "plan": plan,
        "done_transfers": sorted(done_transfers),
        "done_conversions": sorted(done_conversions),
        "fee_done": fee_done,
        "fee_check": (
            "none found"
            if not fee_done
            else "charge found; source checked"
            if source is not None
            else "charge found; source not checked by this read"
        ),
    }


def _age_line(k: Keys, rid: str, first: datetime, now: datetime) -> str:
    return (
        f"NEEDS_PERSON: employer {k.employer}, pay period {k.period}, request_id {rid} has "
        f"been in flight since {first.isoformat()} ({now - first} ago), past the "
        f"{DEDUP_SAFE_MARGIN.days}-day deduplication margin; nothing is sent until a person "
        "confirms whether it landed"
    )


def in_flight_needing_person(intent: Mapping[str, Any], ctx: PolicyContext) -> list[str]:
    """Rule M1's window clause, in policy so the run is BLOCKED before anything is sent.

    Money of the current attempt that was sent but has no terminal object (a conversion or
    fee not found, a transfer not found or not terminal) and whose first send is older than
    DEDUP_SAFE_MARGIN, plus anything the build gate already routed to a person. The attempt
    never advances for it, so this employer cannot fund or pay that money another way.
    """
    store = ctx.platform_store
    k = keys_for(store, intent)
    if k is None:
        return []
    out = [
        f"NEEDS_PERSON: employer {k.employer}, pay period {k.period}, request_id "
        f"{r['request_id']}: {r['why']}"
        for r in needs_person(store, k)
    ]
    funding = intent["funding_currency"]
    money: list[tuple[str, str, Read | None]] = [
        (k.payout_rid(c["contractor"]), "/api/v1/transfers", ctx.read)
        for c in intent["contractors"]
    ]
    money += [
        (k.conversion_rid(ccy, funding), "/api/v1/fx/conversions", ctx.read)
        for ccy in intent["pricing"]["by_currency"]
        if ccy != funding
    ]
    money.append((k.fee_rid(), "/api/v1/charges", ctx.read_platform))
    for rid, path, read in money:
        rec = sent(store, rid)
        if rec is None or ctx.now - rec["first_sent_at"] <= DEDUP_SAFE_MARGIN:
            continue
        if read is None:
            out.append(_age_line(k, rid, rec["first_sent_at"], ctx.now))
            continue
        row = one_by_request_id(read, path, rid)
        landed = row is not None and (
            path != "/api/v1/transfers" or row.get("status") in TRANSFER_TERMINAL
        )
        if not landed:
            out.append(_age_line(k, rid, rec["first_sent_at"], ctx.now))
    return out


def gate_reemit(sctx: StepContext, k: Keys, rid: str) -> None:
    """Defence in depth at build time: refuse to re-emit money past the margin."""
    store = sctx.policy.platform_store
    rec = sent(store, rid)
    if rec is not None and sctx.policy.now - rec["first_sent_at"] > DEDUP_SAFE_MARGIN:
        line = _age_line(k, rid, rec["first_sent_at"], sctx.policy.now)
        record_needs_person(store, k, rid, "in flight past the deduplication margin")
        raise NeedsPerson(line)


# ---------------------------------------------------------------- child policy


def validate_employer(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical child intent. Idempotent on its own output; refuses tampered pricing."""
    if not isinstance(raw, Mapping) or "pay_period" not in raw:
        raise PayrollInputError("employer payroll needs a pay_period")
    body = {k: v for k, v in raw.items() if k not in ("pay_period", "pricing", "fx_lock")}
    employer = parse_employer(body, raw["pay_period"])
    pricing = price_employer(employer)
    if "pricing" in raw and canonical_json(raw["pricing"]) != canonical_json(pricing):
        raise PayrollInputError("supplied pricing does not match the payroll; refusing")
    out = {**employer, "pricing": pricing}
    if "fx_lock" in raw:
        out["fx_lock"] = _parse_fx_lock(raw["fx_lock"], out)
    return out


def _parse_fx_lock(raw: Any, child: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The parent's approved FX per conversion (rule M3): the locked quote and its exact sell
    amount, one per non-funding currency."""
    funding = child["funding_currency"]
    wanted = {c for c in child["pricing"]["by_currency"] if c != funding}
    if not isinstance(raw, Mapping) or set(raw) != wanted:
        raise PayrollInputError("fx_lock must name exactly the currencies to convert")
    out = {}
    for ccy in sorted(raw):
        lock = raw[ccy]
        if not isinstance(lock, Mapping) or set(lock) != {"quote_id", "sell", "valid_to_at"}:
            raise PayrollInputError(f"fx_lock.{ccy} needs quote_id, sell and valid_to_at")
        sell = parse_money(lock["sell"], f"fx_lock.{ccy}.sell")
        if sell.currency != funding:
            raise PayrollInputError("a locked sell must be in the funding currency")
        valid_to = lock["valid_to_at"]
        if not isinstance(valid_to, datetime) or valid_to.tzinfo is None:
            raise PayrollInputError(f"fx_lock.{ccy}.valid_to_at must be an aware datetime")
        out[ccy] = {"quote_id": _str_id(lock["quote_id"]), "sell": sell, "valid_to_at": valid_to}
    return out


def _str_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise PayrollInputError("quote_id must be a provider id")
    return value


def max_sell(intent: Mapping[str, Any]) -> dict[str, Money] | None:
    lock = intent.get("fx_lock")
    return None if lock is None else {ccy: v["sell"] for ccy, v in lock.items()}


def _locks_expiring(intent: Mapping[str, Any], ctx: PolicyContext) -> list[str]:
    """Lock expiry is judged per child, at child time, so the parent's hash stays stable. A
    conversion already sent is re-emitted as sent, whatever its lock."""
    k = keys_for(ctx.platform_store, intent)
    funding = intent["funding_currency"]
    out = []
    for ccy, lock in (intent.get("fx_lock") or {}).items():
        if k is not None and sent(ctx.platform_store, k.conversion_rid(ccy, funding)):
            continue
        if lock["valid_to_at"] <= ctx.now + LOCK_MIN_REMAINING:
            out.append(
                f"employer {intent['employer']}: the {ccy} rate lock expires at "
                f"{lock['valid_to_at'].isoformat()}; lock rates again and re-propose"
            )
    return out


def employer_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    """AUTO (run it) or BLOCKED (escalate). Reads only this employer's wallet. `computed` is
    empty: every live figure goes in the reasons, so rule 1's re-run hash is stable."""
    require_employer_tenant(ctx.tenant, intent)
    employer, period = intent["employer"], intent["pay_period"]
    pricing = intent["pricing"]
    if pricing["blockers"]:
        return PolicyDecision(RiskTier.BLOCKED, tuple(pricing["blockers"]))
    held = in_flight_needing_person(intent, ctx)
    if held:
        return PolicyDecision(RiskTier.BLOCKED, tuple(held))
    claim = read_claim(ctx.platform_store, keys_for(ctx.platform_store, intent))
    # A redelivered or resumed child of the claim's owner sees its own proposal_id and
    # passes; every other run is blocked.
    if claim is not None and claim["owner"] != ctx.proposal_id:
        return PolicyDecision(
            RiskTier.BLOCKED,
            (
                f"employer {employer}, pay period {period}: another run of this period is in "
                f"flight (proposal {claim['owner']}); propose again once it has finished",
            ),
        )
    expiring = _locks_expiring(intent, ctx)
    if expiring:
        return PolicyDecision(RiskTier.BLOCKED, tuple(expiring))
    funding = intent["funding_currency"]
    if any(c != funding for c in pricing["by_currency"]) and "fx_lock" not in intent:
        return PolicyDecision(
            RiskTier.BLOCKED,
            (
                f"employer {employer}: its FX is not rate-locked (or the lock expired or was "
                "used); run propose_lock_rates, then propose the payroll again",
            ),
        )
    try:
        a = assess_employer(
            intent,
            ctx.read,
            ctx.read_platform,
            ctx.platform_store,
            ctx.tenant.connected_account_id,
        )
    except (MissingPrerequisite, RateUnusable) as exc:
        return PolicyDecision(RiskTier.BLOCKED, (f"employer {employer}: {exc}",))
    plan = a["plan"]
    lines = [
        f"employer {employer}, pay period {period}, {len(intent['contractors'])} contractors",
        f"funding {intent['funding_currency']}: required {plan['required']}, "
        f"available {plan['available']}",
        f"platform fee {pricing['platform_fee']}",
        *(
            f"convert to {cv['buy_amount']} selling at most {cv['sell_cap']} (approved)"
            for cv in plan["conversions"]
        ),
    ]
    if plan["shortfall"].amount > 0:
        return PolicyDecision(
            RiskTier.BLOCKED,
            (
                *lines,
                f"SHORTFALL {plan['shortfall']}: employer {employer} cannot fund this payroll "
                "from its own wallet; no other employer's funds may be used; escalate",
            ),
        )
    if len(a["done_transfers"]) == len(intent["contractors"]) and a["fee_done"]:
        lines.append("every payout and the fee of this period already exist")
    return PolicyDecision(RiskTier.AUTO, tuple(lines))


# ---------------------------------------------------------------- child steps


def _scoped(op: Operation, sctx: StepContext) -> Operation:
    require_employer_tenant(sctx.policy.tenant, sctx.intent)
    return bind_operation(op, sctx.policy.tenant)


def _keys(sctx: StepContext) -> Keys:
    k = keys_for(sctx.policy.platform_store, sctx.intent)
    if k is None:
        raise MissingPrerequisite("no book; the snapshot step mints it")
    return k


def snapshot_key(proposal_id: str) -> str:
    """Owned by this run (rule M4), never by intent_hash, which identical proposals share."""
    return f"snapshot/{proposal_id}"


def list_transactions(read: Read, since: datetime) -> list[Mapping[str, Any]]:
    """Every financial transaction since `since`, page by page, bounded.

    Unverified live: the `from_created_at` filter, `page_num` from 0, `page_size`
    and `has_more`. Past MAX_TXN_PAGES it fails closed rather than guess.
    """
    rows: list[Mapping[str, Any]] = []
    for page in range(MAX_TXN_PAGES):
        body = get(
            read,
            "/api/v1/financial_transactions",
            {
                "from_created_at": since.isoformat().replace("+00:00", "Z"),
                "page_num": str(page),
                "page_size": str(TXN_PAGE_SIZE),
            },
        )
        rows += list_items(body)
        if not body.get("has_more"):
            return rows
    raise SnapshotUnstable(f"financial_transactions has more than {MAX_TXN_PAGES} pages")


def _txn_ids(read: Read, since: datetime) -> tuple[frozenset[str], list[Mapping[str, Any]]]:
    rows = list_transactions(read, since)
    return frozenset(_object_id(r) for r in rows), rows


def build_snapshot(sctx: StepContext) -> Sequence[Operation]:
    """Mint kit 6's book if this is its first run (rule M2), then record the opening state.

    List transactions, read balances, list again. If both lists agree, every listed
    transaction is in the opening balance and nothing else is, so the postcondition can count
    later deposits by id instead of by time (no double count). Emits no operations.
    """
    require_employer_tenant(sctx.policy.tenant, sctx.intent)
    store = sctx.policy.store
    if store is None:
        raise MissingPrerequisite("the snapshot needs ctx.store")
    k = keys_for(sctx.policy.platform_store, sctx.intent, mint=True)
    assert k is not None
    other = take_claim(
        sctx.policy.platform_store,
        k,
        sctx.proposal_id,
        sctx.policy.now,
        root_proposal_id=sctx.root_proposal_id,
        root_action=sctx.root_action,
    )
    if other is not None:
        raise RunClaimed(
            f"employer {k.employer}, pay period {k.period}: proposal {other} is running; "
            "nothing sent"
        )
    try:
        since = sctx.policy.now - DEPOSIT_WINDOW
        currencies = sorted(
            {sctx.intent["funding_currency"], *sctx.intent["pricing"]["by_currency"]}
        )
        for _ in range(SNAPSHOT_TRIES):
            before, _rows = _txn_ids(sctx.policy.read, since)
            balances = get(sctx.policy.read, "/api/v1/balances/current")
            after, _rows = _txn_ids(sctx.policy.read, since)
            if before == after:
                # Payouts an earlier run already created were debited before this opening
                # balance; the postcondition must not charge them to this run again.
                earlier = existing_transfers(sctx.policy.read, k, sctx.intent)
                value = {
                    "since": since,
                    "opening": {c: available_balance(balances, c) for c in currencies},
                    "txn_ids": sorted(after),
                    "earlier_transfer_ids": sorted(_object_id(t) for t in earlier.values()),
                    # Names this execution of the steps: a resumed or repeated run (even under the
                    # same proposal_id) gets a new token, so a re-emission is never mistaken for
                    # a first send (rule M1).
                    "run_token": uuid.uuid4().hex,
                    "finished": False,
                }
                key = snapshot_key(sctx.proposal_id)
                current = store.get(key)
                if current is not None and not current.value.get("finished"):
                    # A redelivered snapshot task of the SAME attempt (the execution name is the
                    # proposal_id, so no second attempt can run while this one is unfinished):
                    # keep its opening state and token.
                    return ()
                store.put(key, value, expected_version=None if current is None else current.version)
                return ()
        raise SnapshotUnstable("transactions kept landing during the opening balance read")
    except SnapshotUnstable:
        # Nothing was sent: release the claim this run just took, then fail closed.
        release_claim(sctx.policy.platform_store, k, sctx.proposal_id)
        raise


def _finish(sctx: StepContext) -> None:
    """This attempt is over: a later run of the same proposal_id is a new attempt."""
    store = sctx.policy.store
    item = store.get(snapshot_key(sctx.proposal_id)) if store is not None else None
    if item is not None and not item.value.get("finished"):
        store.put(item.key, {**item.value, "finished": True}, expected_version=item.version)
    k = keys_for(sctx.policy.platform_store, sctx.intent)
    if k is not None and item is not None:  # only a run that took the claim releases it
        release_claim(sctx.policy.platform_store, k, sctx.proposal_id)


def run_token(sctx: StepContext) -> str:
    store = sctx.policy.store
    item = store.get(snapshot_key(sctx.proposal_id)) if store is not None else None
    if item is None:
        raise MissingPrerequisite("no snapshot for this run")
    return str(item.value["run_token"])


def _pending_contractors(sctx: StepContext, k: Keys) -> list[Mapping[str, Any]]:
    done = existing_transfers(sctx.policy.read, k, sctx.intent)
    return [c for c in sctx.intent["contractors"] if c["contractor"] not in done]


def _needs_beneficiary(sctx: StepContext, k: Keys) -> list[Mapping[str, Any]]:
    return [
        c
        for c in _pending_contractors(sctx, k)
        if find_beneficiary(sctx.policy.read, k, c["contractor"]) is None
    ]


def build_validate_beneficiaries(sctx: StepContext) -> Sequence[Operation]:
    k = _keys(sctx)
    return [
        _scoped(ops.validate_beneficiary(k, c, sctx.intent_hash), sctx)
        for c in _needs_beneficiary(sctx, k)
    ]


def build_create_beneficiaries(sctx: StepContext) -> Sequence[Operation]:
    k = _keys(sctx)
    return [
        _scoped(ops.create_beneficiary(k, c, sctx.intent_hash), sctx)
        for c in _needs_beneficiary(sctx, k)
    ]


def reconcile_beneficiary(op: Operation, read: Read) -> Mapping[str, Any] | None:
    """After OUTCOME_UNKNOWN: find the beneficiary by its nickname, never by a new id."""
    nick = op.body["nickname"]
    try:
        rows = beneficiaries_named(read, nick)
    except MissingPrerequisite as exc:
        raise ReconcileUndetermined(str(exc)) from exc
    if len(rows) > 1:
        raise ReconcileUndetermined(f"two beneficiaries carry nickname {nick}")
    return rows[0] if rows else None


def _pending_conversions(sctx: StepContext, k: Keys) -> list[tuple[str, Money, str]]:
    """(currency, buy, request_id) for each conversion this period has not yet landed."""
    i = sctx.intent
    done = existing_conversions(sctx.policy.read, k, i, sctx.policy.platform_store)
    funding = i["funding_currency"]
    return [
        (ccy, need["need"], k.conversion_rid(ccy, funding))
        for ccy, need in sorted(i["pricing"]["by_currency"].items())
        if ccy != funding and ccy not in done
    ]


def _approved_lock(intent: Mapping[str, Any], ccy: str) -> Mapping[str, Any]:
    lock = (intent.get("fx_lock") or {}).get(ccy)
    if lock is None:
        raise QuoteAboveApprovedCap(f"no approved rate lock for {ccy}; nothing sent")
    return lock


def _quote_sell(quote: Mapping[str, Any], buy: Money, sell_ccy: str, now: datetime) -> Money:
    valid_to = quote.get("valid_to_at")
    if not isinstance(valid_to, str):
        raise QuoteUnusable("quote has no valid_to_at")
    if datetime.fromisoformat(valid_to.replace("Z", "+00:00")) <= now:
        raise QuoteUnusable("quote has expired")
    if Money.of(str(quote.get("buy_amount")), buy.currency) != buy:
        raise QuoteUnusable("quote is for a different buy amount")
    if quote.get("currency_pair") not in (f"{buy.currency}{sell_ccy}", f"{sell_ccy}{buy.currency}"):
        raise QuoteUnusable("quote is for a different currency pair")
    if "sell_amount" not in quote:
        raise QuoteUnusable("quote has no sell_amount")
    return money_field(quote, "sell_amount", sell_ccy)


def build_conversions(sctx: StepContext) -> Sequence[Operation]:
    """Rule M3: the live quote's sell must not exceed the approved maximum, else this child
    fails closed with nothing sent. Rule M1: a conversion already sent under its current
    request_id is re-emitted with the same quote_id, so the body never changes."""
    i = sctx.intent
    k = _keys(sctx)
    store = sctx.policy.platform_store
    funding = i["funding_currency"]
    planned = []
    for ccy, buy, rid in _pending_conversions(sctx, k):
        lock = _approved_lock(i, ccy)
        ceiling = lock["sell"]
        rec = sent(store, rid)
        if rec is not None:
            gate_reemit(sctx, k, rid)
            quote_id, sell = str(rec["quote_id"]), rec["sell"]
            if (quote_id, sell) != (lock["quote_id"], lock["sell"]):
                # M3: a re-emission must carry exactly what the approval binds.
                raise QuoteAboveApprovedCap(
                    f"the in-flight {ccy} conversion carries quote {quote_id} selling {sell}, "
                    f"but the approval binds {lock['quote_id']} selling {lock['sell']}; "
                    "nothing sent"
                )
        else:
            if lock["valid_to_at"] <= sctx.policy.now:
                raise QuoteUnusable(
                    f"the {ccy} rate lock expired at {lock['valid_to_at'].isoformat()}; "
                    "nothing sent; lock rates again and re-propose"
                )
            quote_id, sell = lock["quote_id"], lock["sell"]
        if sell > ceiling:
            raise QuoteAboveApprovedCap(
                f"quote sells {sell} for {buy}, above the approved maximum {ceiling}; nothing sent"
            )
        planned.append((ccy, rid, buy, quote_id, sell))
    # Every currency is checked before any is recorded, so a refusal on one never leaves
    # another recorded but unsent.
    out = []
    for ccy, rid, buy, quote_id, sell in planned:
        record_sent(
            store,
            rid,
            sctx.policy.now,
            run_token(sctx),
            {
                "quote_id": quote_id,
                "sell": sell,
                "valid_to_at": _approved_lock(i, ccy)["valid_to_at"],
            },
        )
        op = ops.create_conversion(rid, k, buy, funding, quote_id, sctx.intent_hash)
        out.append(_scoped(op, sctx))
    return out


def build_payouts(sctx: StepContext) -> Sequence[Operation]:
    """New payouts, plus the same Operation again for a payout an earlier run created but did
    not see to a terminal status, or whose create ended OUTCOME_UNKNOWN with an empty
    read-back (rule M1). The body is deterministic (amount, beneficiary found by nickname),
    so the store or the provider returns the original; nothing is paid twice."""
    i = sctx.intent
    k = _keys(sctx)
    done = existing_transfers(sctx.policy.read, k, i)
    store = sctx.policy.platform_store
    built = []
    new: list[Mapping[str, Any]] = []
    for c in i["contractors"]:
        row = done.get(c["contractor"])
        if row is not None and row.get("status") in TRANSFER_TERMINAL:
            continue
        bid = find_beneficiary(sctx.policy.read, k, c["contractor"])
        if bid is None:
            raise MissingPrerequisite(f"no beneficiary for contractor {c['contractor']}")
        op = _scoped(ops.create_transfer(k, c, bid, sctx.intent_hash), sctx)
        gate_reemit(sctx, k, op.request_id)
        built.append(op)
        if row is None:  # not yet debited: it needs its money in the wallet now
            new.append(c)
    _funds_cover(sctx, k, new)
    for op in built:
        record_sent(store, op.request_id, sctx.policy.now, run_token(sctx))
    return built


class FundsShort(MissingPrerequisite):
    """The employer's own wallet does not cover every payout of this child at once."""


def _funds_cover(sctx: StepContext, k: Keys, new: Sequence[Mapping[str, Any]]) -> None:
    """Before the first payout leaves, this employer's wallet must cover, per currency,
    every payout still to send plus its SWIFT fee (pricing.payout_cost, the same fee table the
    conversion was sized from), and the platform fee in the funding currency unless it is
    already collected. Otherwise nothing is sent, so no employer is left part-paid by a
    shortfall the kit could see."""
    if not new:
        return
    i = sctx.intent
    funding = i["funding_currency"]
    need: dict[str, Money] = {}
    for c in new:
        cost = payout_cost(c)
        need[cost.currency] = need.get(cost.currency, Money.zero(cost.currency)) + cost
    acct = sctx.policy.tenant.connected_account_id
    fee = i["pricing"]["platform_fee"]
    if existing_fee(sctx.policy.read_platform, k, fee, acct) is None:
        need[funding] = need.get(funding, Money.zero(funding)) + fee
    balances = get(sctx.policy.read, "/api/v1/balances/current")
    short = []
    for ccy in sorted(need):
        have = available_balance(balances, ccy)
        if have < need[ccy]:
            short.append(f"{ccy} needs {need[ccy]}, wallet holds {have}")
    if short:
        raise FundsShort(
            f"employer {i['employer']}: wallet does not cover every payout at once ("
            + "; ".join(short)
            + "); nothing sent"
        )


def is_definitive_failure(row: Mapping[str, Any] | None) -> bool:
    """The provider's own failure state: a transfer reading CANCELLED with its failure block."""
    return (
        row is not None
        and row.get("status") == "CANCELLED"
        and isinstance(row.get("failure"), Mapping)
    )


def status_line(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "NOT_SUBMITTED"
    status = str(row.get("status"))
    failure = row.get("failure")
    if status == "CANCELLED" and isinstance(failure, Mapping):
        details = failure.get("details") if isinstance(failure.get("details"), Mapping) else {}
        return (
            f"CANCELLED (failure.code {failure.get('code')}, type {details.get('type')}, "
            f"{failure.get('message')})"
        )
    if status not in TRANSFER_TERMINAL:
        return f"in flight ({status})"
    return status


def build_fee(sctx: StepContext) -> Sequence[Operation]:
    """Collect the fee only when every contractor is PAID; otherwise withhold it. The body is
    deterministic, so after an unknown outcome a later run re-emits the same Operation."""
    i = sctx.intent
    require_employer_tenant(sctx.policy.tenant, i)
    k = _keys(sctx)
    acct = sctx.policy.tenant.connected_account_id
    if existing_fee(sctx.policy.read_platform, k, i["pricing"]["platform_fee"], acct) is not None:
        return ()
    transfers = existing_transfers(sctx.policy.read, k, i)
    if len(transfers) != len(i["contractors"]) or any(
        t.get("status") not in TRANSFER_SUCCESS for t in transfers.values()
    ):
        return ()
    op = ops.create_fee_charge(k, i["pricing"]["platform_fee"], sctx.intent_hash)
    gate_reemit(sctx, k, op.request_id)
    record_sent(sctx.policy.platform_store, op.request_id, sctx.policy.now, run_token(sctx))
    return [bind_platform_operation(op, sctx.policy.tenant)]


def _finality(
    path: str,
    success: frozenset[str],
    terminal: frozenset[str],
    advance: Any = None,
) -> FinalitySpec:
    return FinalitySpec(
        read_request=lambda r: ReadRequest(path=f"{path}/{r.provider_id}"),
        status_of=lambda b: str(b.get("status")),
        terminal=terminal,
        success=success,
        sandbox_advance=advance,
    )


CONVERSION_FINALITY = _finality(
    "/api/v1/fx/conversions", frozenset({"SETTLED"}), frozenset({"SETTLED", "CANCELLED"})
)
# Sandbox mode (rule 10): awx_common.sandbox's driver moves each payout PROCESSING -> SENT, then
# SENT -> PAID after hold_s, never PROCESSING -> PAID (refused with 500 live).
TRANSFER_FINALITY = _finality(
    "/api/v1/transfers", TRANSFER_SUCCESS, TRANSFER_TERMINAL, make_transfer_advance(KIT)
)
# Headerless read (the op has no on_behalf_of), so rule 5 routes it as a platform read.
CHARGE_FINALITY = _finality(
    "/api/v1/charges", frozenset({"SETTLED"}), frozenset({"SETTLED", "FAILED", "SUSPENDED"})
)


# ---------------------------------------------------------------- postcondition


def _payout_cost_read(t: Mapping[str, Any], currency: str) -> Money:
    if t.get("amount_payer_pays") is not None:
        return money_field(t, "amount_payer_pays", currency)
    fee = money_field(t, "fee_amount", currency) if t.get("fee_amount") is not None else None
    base = money_field(t, "transfer_amount", currency)
    return base + fee if fee is not None else base


def definitive_create_failure(r: OperationResult, *, first_send: bool) -> bool:
    """Rule M1: is this create result proof the money never moved?

    Only a FAILED, non-retryable refusal of a FIRST send. Never: OUTCOME_UNKNOWN (including
    the spine's refused resend in body["refused"] and its multiple_objects case);
    DuplicateRequest; a retryable FAILED such as RateLimited or TransientServerError (a resend
    after an unknown first send that only ever got a 429 or a not-sent error); or any refusal
    of a RE-emission (the original may still land).
    """
    if r.status is not OperationStatus.FAILED or r.error_type == "DuplicateRequest":
        return False
    err = r.error()
    if err is None or err.retryable or err.outcome_unknown:
        return False
    return first_send


def record_definitive_failures(
    sctx: StepContext, k: Keys, transfers: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Rule M1: only a definitive failure advances that money to its next attempt.

    Definitive: an operation FAILED at create (a 4xx; DuplicateRequest excluded), or a
    transfer reading CANCELLED with its failure block. OUTCOME_UNKNOWN, an empty read-back
    and a timed-out finality poll leave the money in flight on its request_id.
    """
    i = sctx.intent
    funding = i["funding_currency"]
    money: dict[str, str] = {}
    for c in i["contractors"]:
        money[k.payout_rid(c["contractor"])] = k.payout_key(c["contractor"])
    for ccy in i["pricing"]["by_currency"]:
        if ccy != funding:
            money[k.conversion_rid(ccy, funding)] = k.conversion_key(ccy, funding)
    money[k.fee_rid()] = k.fee_key()
    failed: list[tuple[str, str, str]] = []
    store = sctx.policy.platform_store
    for step in ("fx_conversions", "payouts", "platform_fee"):
        for r in sctx.prior.get(step, ()):
            if r.request_id not in money:
                continue
            rec = sent(store, r.request_id)
            first = rec is not None and rec.get("first_sent_by") == run_token(sctx)
            if definitive_create_failure(r, first_send=first):
                failed.append((money[r.request_id], r.request_id, str(r.error_code)))
    for c in i["contractors"]:
        row = transfers.get(c["contractor"])
        if is_definitive_failure(row):
            rid = k.payout_rid(c["contractor"])
            failed.append((money[rid], rid, status_line(row)))
    for money_key, rid, why in failed:
        record_failure(sctx.policy.platform_store, money_key, rid, why)
    return [rid for _m, rid, _w in failed]


def wallet_delta_postcondition(sctx: StepContext) -> PostconditionResult:
    """Runs the check, and always releases this run's claim, whatever the check does.
    release_claim acts only when this proposal_id owns the claim (rule M4). Money in
    flight stays on its request_id: the next run re-emits the same Operation, reconciles it,
    or routes it to a person past the margin, so releasing the claim cannot pay twice."""
    try:
        return _wallet_delta(sctx)
    finally:
        k = keys_for(sctx.policy.platform_store, sctx.intent)
        if k is not None and sctx.policy.platform_store is not None:
            release_claim(sctx.policy.platform_store, k, sctx.proposal_id)


def _wallet_delta(sctx: StepContext) -> PostconditionResult:
    """Every payout PAID, and every balance delta in this employer's own wallet, exactly.
    Money with an unknown outcome is reported as in flight, never as failed (rule M1)."""
    i = sctx.intent
    require_employer_tenant(sctx.policy.tenant, i)
    read = sctx.policy.read
    funding = i["funding_currency"]
    problems: list[str] = []
    store = sctx.policy.store
    snap_item = store.get(snapshot_key(sctx.proposal_id)) if store is not None else None
    k = keys_for(sctx.policy.platform_store, i)
    if snap_item is None or k is None:
        return PostconditionResult(False, f"employer {i['employer']}: no opening snapshot")
    snap = snap_item.value
    opening: Mapping[str, Money] = snap["opening"]

    transfers = existing_transfers(read, k, i)
    unknown = {
        r.request_id
        for step in ("fx_conversions", "payouts", "platform_fee")
        for r in sctx.prior.get(step, ())
        if r.status is OperationStatus.OUTCOME_UNKNOWN
    }
    for c in i["contractors"]:
        row = transfers.get(c["contractor"])
        rid = k.payout_rid(c["contractor"])
        if row is None and rid in unknown:
            problems.append(
                f"contractor {c['contractor']} payout is in flight (outcome unknown on "
                f"request_id {rid})"
            )
        elif row is None or row.get("status") not in TRANSFER_SUCCESS:
            problems.append(f"contractor {c['contractor']} payout is {status_line(row)}")

    expected = {ccy: Money.zero(ccy) for ccy in opening}
    planned = {
        k.conversion_rid(ccy, funding): need["need"]
        for ccy, need in i["pricing"]["by_currency"].items()
        if ccy != funding
    }
    for r in sctx.prior.get("fx_conversions", ()):
        if r.status is OperationStatus.OUTCOME_UNKNOWN:
            problems.append(f"conversion {r.request_id} is in flight (outcome unknown)")
            continue
        if r.status is not OperationStatus.SUCCEEDED or not r.provider_id:
            rec = sent(sctx.policy.platform_store, r.request_id)
            if rec is not None and rec.get("first_sent_by") != run_token(sctx):
                problems.append(
                    f"conversion {r.request_id} re-emission refused ({r.error_code}); the "
                    "original stays in flight"
                )
            else:
                problems.append(f"conversion {r.request_id} failed ({r.error_code})")
            continue
        try:
            body = get(read, f"/api/v1/fx/conversions/{r.provider_id}")
        except NotFound:
            # A NotFound after a send that may have landed means unknown, never absent.
            problems.append(
                f"conversion {r.provider_id} is not in this employer's account (outcome "
                "unknown); it may have converted another wallet"
            )
            buy = planned.get(r.request_id)
            if buy is not None:  # this wallet should have gained it; the delta shows it did not
                expected[buy.currency] = expected[buy.currency] + buy
            continue
        buy_ccy, sell_ccy = str(body.get("buy_currency")), str(body.get("sell_currency"))
        if sell_ccy != funding or buy_ccy not in expected:
            problems.append(f"conversion {r.provider_id} has unexpected currencies")
            continue
        expected[buy_ccy] = expected[buy_ccy] + money_field(body, "buy_amount", buy_ccy)
        expected[sell_ccy] = expected[sell_ccy] - money_field(body, "sell_amount", sell_ccy)
    earlier = set(snap["earlier_transfer_ids"])
    for r in sctx.prior.get("payouts", ()):
        if r.status is not OperationStatus.SUCCEEDED or not r.provider_id:
            continue
        if r.provider_id in earlier:
            continue  # created and debited by an earlier run, before this snapshot
        t = get(read, f"/api/v1/transfers/{r.provider_id}")
        if t.get("status") == "CANCELLED":
            continue  # a cancelled transfer is refunded
        ccy = str(t.get("transfer_currency"))
        if ccy not in expected:
            problems.append(f"transfer {r.provider_id} paid from an unexpected currency")
            continue
        expected[ccy] = expected[ccy] - _payout_cost_read(t, ccy)
    for r in sctx.prior.get("platform_fee", ()):
        if r.status is OperationStatus.SUCCEEDED and r.provider_id:
            if sctx.policy.read_platform is None:
                problems.append("cannot read the fee charge back")
                continue
            ch = get(sctx.policy.read_platform, f"/api/v1/charges/{r.provider_id}")
            if ch.get("source") not in (None, sctx.policy.tenant.connected_account_id):
                problems.append(f"charge {r.provider_id} names another source")
            ccy = str(ch.get("currency"))
            expected[ccy] = expected[ccy] - money_field(ch, "amount", ccy)
        elif r.status is OperationStatus.OUTCOME_UNKNOWN:
            problems.append(f"fee charge {r.request_id} is in flight (outcome unknown)")

    paid = sorted(
        c["contractor"]
        for c in i["contractors"]
        if (transfers.get(c["contractor"]) or {}).get("status") in TRANSFER_SUCCESS
    )
    if paid and len(paid) < len(i["contractors"]):
        unpaid = sorted(c["contractor"] for c in i["contractors"] if c["contractor"] not in paid)
        problems.insert(
            0,
            f"PART-PAID, a person decides: paid {paid}, unpaid {unpaid}; the unpaid payouts "
            "are not retried in this run",
        )
    retry = record_definitive_failures(sctx, k, transfers)
    if retry:
        problems.append(f"definitive failures recorded; the next approved run retries {retry}")

    try:
        _ids, rows = _txn_ids(read, snap["since"])
    except SnapshotUnstable as exc:
        _finish(sctx)
        return PostconditionResult(False, f"employer {i['employer']}: {exc}")
    deposits = {ccy: Money.zero(ccy) for ccy in expected}
    seen = set(snap["txn_ids"])
    for t in rows:
        ccy = t.get("currency")
        if t.get("transaction_type") == "DEPOSIT" and ccy in deposits and str(t["id"]) not in seen:
            deposits[ccy] = deposits[ccy] + money_field(t, "amount", str(ccy))
    closing_body = get(read, "/api/v1/balances/current")
    for ccy in sorted(expected):
        actual = available_balance(closing_body, ccy) - opening[ccy] - deposits[ccy]
        if actual != expected[ccy]:
            problems.append(
                f"{ccy} wallet moved {actual} but this run accounts for {expected[ccy]}"
            )
    _finish(sctx)
    if problems:
        return PostconditionResult(False, f"employer {i['employer']}: " + "; ".join(problems))
    return PostconditionResult(
        True,
        f"employer {i['employer']}: every payout PAID and every delta in its own wallet "
        + ", ".join(f"{ccy} {expected[ccy]}" for ccy in sorted(expected)),
    )


EMPLOYER_SPEC = KitWorkflowSpec(
    approver=ApproverScope.OWN_TENANT,  # the proposing tenant's own approvers
    kit=KIT,
    action="employer_payroll",
    validate=validate_employer,
    policy=employer_policy,
    steps=(
        StepSpec("snapshot", build_snapshot),
        StepSpec("validate_beneficiaries", build_validate_beneficiaries),
        StepSpec("create_beneficiaries", build_create_beneficiaries, None, reconcile_beneficiary),
        StepSpec("fx_conversions", build_conversions, CONVERSION_FINALITY),
        StepSpec("payouts", build_payouts, TRANSFER_FINALITY),
        StepSpec("platform_fee", build_fee, CHARGE_FINALITY),
    ),
    approval_ttl=APPROVAL_TTL,
    read_allowlist=CHILD_READS,
    platform_read_allowlist=PLATFORM_READS,
    postcondition=wallet_delta_postcondition,
)


# ---------------------------------------------------------------- parent


def validate_batch(raw: Mapping[str, Any]) -> dict[str, Any]:
    batch = parse_batch(raw)
    return {
        "pay_period": batch["pay_period"],
        "employers": [
            validate_employer({k: v for k, v in e.items() if k != "fx_lock"})
            for e in batch["employers"]
        ],
    }


def employer_summary(child: Mapping[str, Any], blockers: Sequence[str]) -> dict[str, Any]:
    """Read-free amounts the approver sees for one employer. Never summed across employers."""
    pricing = child["pricing"]
    return {
        "employer": child["employer"],
        "funding_currency": child["funding_currency"],
        "contractors": len(child["contractors"]),
        "by_currency": {
            ccy: {"payouts": n["payouts"], "swift_fees": n["swift_fees"], "need": n["need"]}
            for ccy, n in sorted(pricing["by_currency"].items())
        },
        "platform_fee": pricing["platform_fee"],
        "fx_lock": dict(child.get("fx_lock") or {}),
        "blockers": [*pricing["blockers"], *blockers],
    }


def _needs_dual(child: Mapping[str, Any]) -> bool:
    for ccy, need in child["pricing"]["by_currency"].items():
        threshold = DUAL_THRESHOLDS.get(ccy)
        if threshold is None or need["need"] >= threshold:
            return True
    return False


def _with_fx_lock(child: Mapping[str, Any], ctx: PolicyContext) -> tuple[dict[str, Any], list[str]]:
    """Rule M3 from stored state, never from a live rate, so rule 1's re-run reads the same.

    - A conversion already sent under its current attempt binds exactly what it was sent with
      (quote, sell, expiry), whatever the stored lock now says: the
      re-emission and the approval can never differ.
    - Otherwise the stored lock for this attempt binds. A lock that is missing, for another
      buy amount, or for an earlier attempt leaves the employer without fx_lock, so its child
      is BLOCKED and the other employers run. Expiry is judged in the child.
    """
    base = {k: v for k, v in child.items() if k != "fx_lock"}
    if base["pricing"]["blockers"]:
        return base, []
    funding = base["funding_currency"]
    fx = {c: n["need"] for c, n in sorted(base["pricing"]["by_currency"].items()) if c != funding}
    if not fx:
        return base, []
    store = ctx.platform_store
    k = keys_for(store, base)
    out: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for ccy, buy in fx.items():
        if k is None:
            problems.append(f"{ccy} rate not locked")
            continue
        rid = k.conversion_rid(ccy, funding)
        rec = sent(store, rid)
        if rec is not None:
            out[ccy] = {
                "quote_id": str(rec["quote_id"]),
                "sell": rec["sell"],
                "valid_to_at": rec["valid_to_at"],
            }
            continue
        lock = read_lock(store, k, ccy)
        if lock is None:
            problems.append(f"{ccy} rate not locked")
        elif lock["buy"] != buy or lock["sell"].currency != funding:
            problems.append(f"{ccy} lock is for another amount; lock again")
        elif lock["for_request_id"] != rid:
            problems.append(f"{ccy} lock was taken for an earlier attempt; lock again")
        else:
            out[ccy] = {
                "quote_id": lock["quote_id"],
                "sell": lock["sell"],
                "valid_to_at": lock["valid_to_at"],
            }
    if problems:
        return base, problems
    return {**base, "fx_lock": out}, []


def batch_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    """Reads no balance and no rate; funding is decided per employer, on its own wallet alone.

    Each conversion's exact sell comes from the rate lock in the platform store (rule M3).
    SINGLE, or DUAL when one employer's payroll in one currency reaches its threshold, or
    BLOCKED when no employer can run. Never AUTO.
    """
    require_operator(ctx.tenant)
    pairs = [_with_fx_lock(c, ctx) for c in intent["employers"]]
    children = [c for c, _p in pairs]
    summaries = [employer_summary(c, p) for c, p in pairs]
    if all(s["blockers"] for s in summaries):
        tier = RiskTier.BLOCKED
    elif any(_needs_dual(c) for c in children):
        tier = RiskTier.DUAL
    else:
        tier = RiskTier.SINGLE
    reasons = [
        f"pay period {intent['pay_period']}: {len(children)} employers, one child run each, "
        "each funded only from its own wallet",
        "each FX conversion uses its locked quote and sells exactly the amount shown",
    ]
    for s_ in summaries:
        parts = ", ".join(f"{ccy} {n['need']}" for ccy, n in s_["by_currency"].items())
        fx = ", ".join(
            f"{ccy} for {v['sell']} (quote valid to {v['valid_to_at'].isoformat()})"
            for ccy, v in s_["fx_lock"].items()
        )
        reasons.append(
            f"employer {s_['employer']}: {s_['contractors']} contractors, {parts}, "
            f"fee {s_['platform_fee']}"
            + (f", buys {fx}" if fx else "")
            + (f", BLOCKED: {s_['blockers']}" if s_["blockers"] else "")
        )
    computed = {"children": children, "summaries": summaries}
    return PolicyDecision(tier, tuple(reasons), computed)


def fan_out(intent: Mapping[str, Any], decision: PolicyDecision) -> list[dict[str, Any]]:
    """One child intent per employer, exactly as the approved computed lists them (the spine's
    engine refuses any child not canonically equal to an item of a computed list)."""
    approved = decision.computed.get("children")
    if not isinstance(approved, list):
        raise PayrollInputError("the approved batch lists no children")
    base = [validate_employer(e) for e in intent["employers"]]
    children = [validate_employer(c) for c in approved]
    stripped = [{k: v for k, v in c.items() if k != "fx_lock"} for c in children]
    if canonical_json(stripped) != canonical_json(base):
        raise PayrollInputError("child intents differ from the approved batch")
    return children


PAYROLL_SPEC = KitWorkflowSpec(
    approver=ApproverScope.OWN_TENANT,  # the proposing tenant's own approvers
    kit=KIT,
    action="run_payroll",
    validate=validate_batch,
    policy=batch_policy,
    steps=(),
    approval_ttl=APPROVAL_TTL,
    read_allowlist=CHILD_READS,  # read tools reach one employer through ctx.read_tenant
    platform_read_allowlist=PLATFORM_READS,
    fan_out=fan_out,
    child=EMPLOYER_SPEC,
    child_tenant=child_tenant,
)


# ---------------------------------------------------------------- rate lock (AUTO)


def validate_lock(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One employer's conversions to lock: pay period, employer, funding currency, buys."""
    if not isinstance(raw, Mapping) or set(raw) != {
        "pay_period",
        "employer",
        "funding_currency",
        "buys",
    }:
        raise PayrollInputError("a lock needs pay_period, employer, funding_currency and buys")
    buys = raw["buys"]
    if not isinstance(buys, Mapping) or not buys:
        raise PayrollInputError("a lock needs at least one currency to buy")
    out_buys = {}
    for ccy in sorted(buys):
        m = parse_money(buys[ccy], f"buys.{ccy}")
        if m.currency != ccy or ccy == raw["funding_currency"]:
            raise PayrollInputError("each buy is in its own, non-funding currency")
        out_buys[ccy] = m
    return {
        "pay_period": parse_pay_period(raw["pay_period"]),
        "employer": _code_of(raw["employer"]),
        "funding_currency": raw["funding_currency"],
        "buys": out_buys,
    }


def _code_of(value: Any) -> str:
    return parse_employer_code(value, "employer")


def lock_children(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in batch["employers"]:
        if e["pricing"]["blockers"]:
            continue
        funding = e["funding_currency"]
        buys = {c: n["need"] for c, n in e["pricing"]["by_currency"].items() if c != funding}
        if buys:
            out.append(
                validate_lock(
                    {
                        "pay_period": e["pay_period"],
                        "employer": e["employer"],
                        "funding_currency": funding,
                        "buys": buys,
                    }
                )
            )
    return out


def lock_batch_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    """AUTO: a quote moves no money and binds nothing until a person approves the payroll
    that uses it, so this action alone sets agent_min_tier=AUTO (DESIGN.md)."""
    require_operator(ctx.tenant)
    children = lock_children(intent)
    if not children:
        return PolicyDecision(RiskTier.BLOCKED, ("no employer in this batch needs FX",))
    reasons = [
        f"lock {', '.join(f'{c} {m}' for c, m in ch['buys'].items())} for employer {ch['employer']}"
        for ch in children
    ]
    return PolicyDecision(RiskTier.AUTO, tuple(reasons), {"children": children})


def lock_fan_out(intent: Mapping[str, Any], decision: PolicyDecision) -> list[dict[str, Any]]:
    approved = decision.computed.get("children")
    if not isinstance(approved, list) or canonical_json(approved) != canonical_json(
        lock_children(intent)
    ):
        raise PayrollInputError("lock children differ from the approved batch")
    return [validate_lock(c) for c in approved]


def _lock_keys(store: Any, intent: Mapping[str, Any], *, mint: bool = False) -> Keys | None:
    return keys_for(store, intent, mint=mint)


def _to_lock(intent: Mapping[str, Any], store: Any) -> list[tuple[str, Money]]:
    """Currencies whose current conversion attempt has not been sent (nothing to lock else)."""
    k = _lock_keys(store, intent)
    funding = intent["funding_currency"]
    return [
        (ccy, buy)
        for ccy, buy in intent["buys"].items()
        if k is None or sent(store, k.conversion_rid(ccy, funding)) is None
    ]


def lock_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    require_employer_tenant(ctx.tenant, intent)
    todo = _to_lock(intent, ctx.platform_store)
    if not todo:
        return PolicyDecision(
            RiskTier.BLOCKED, (f"employer {intent['employer']}: every conversion is already sent",)
        )
    return PolicyDecision(
        RiskTier.AUTO,
        tuple(f"quote {buy} against {intent['funding_currency']}" for _c, buy in todo),
    )


def build_lock_quotes(sctx: StepContext) -> Sequence[Operation]:
    i = sctx.intent
    require_employer_tenant(sctx.policy.tenant, i)
    store = sctx.policy.platform_store
    k = _lock_keys(store, i, mint=True)
    assert k is not None
    funding = i["funding_currency"]
    out = []
    for ccy, buy in _to_lock(i, store):
        rid = k.conversion_rid(ccy, funding)
        qrid = ids.quote_request_id(rid, k.conversion_key(ccy, funding), sctx.policy.now)
        out.append(_scoped(ops.create_quote(qrid, k, buy, funding, sctx.intent_hash), sctx))
    return out


def parse_ts(value: Any) -> datetime:
    """Airwallex timestamps: ...Z, ...+00:00 or ...+0000."""
    if not isinstance(value, str) or not value:
        raise QuoteUnusable("timestamp missing")
    if len(value) > 5 and value[-5] in "+-" and value[-4:].isdigit():
        value = value[:-2] + ":" + value[-2:]
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise QuoteUnusable("timestamp has no zone")
    return parsed


def lock_postcondition(sctx: StepContext) -> PostconditionResult:
    """Store each quote that came back as the lock for its conversion attempt."""
    i = sctx.intent
    store = sctx.policy.platform_store
    k = _lock_keys(store, i)
    if k is None:
        return PostconditionResult(False, "no book")
    funding = i["funding_currency"]
    locked, problems = [], []
    results = list(sctx.prior.get("quotes", ()))
    for ccy, buy in i["buys"].items():
        mine = [
            r
            for r in results
            if r.status is OperationStatus.SUCCEEDED
            and r.body
            and str(r.body.get("buy_currency")) == ccy
        ]
        if len(mine) != 1:
            problems.append(f"{ccy}: no quote")
            continue
        q = mine[0].body or {}
        try:
            sell = _quote_sell(q, buy, funding, sctx.policy.now)
            valid_to = parse_ts(q.get("valid_to_at"))
        except (QuoteUnusable, MissingPrerequisite, ValueError) as exc:
            problems.append(f"{ccy}: {exc}")
            continue
        kept = not write_lock(
            store,
            k,
            ccy,
            {
                "quote_id": str(q["quote_id"]),
                "buy": buy,
                "sell": sell,
                "valid_to_at": valid_to,
                "for_request_id": k.conversion_rid(ccy, funding),
                "locked_at": sctx.policy.now,
            },
            keep_until=sctx.policy.now,  # keep until it has actually expired
        )
        if kept:
            locked.append(f"{ccy} kept the existing unexpired lock (it may be under approval)")
        else:
            locked.append(f"{ccy} {buy} for {sell} until {valid_to.isoformat()}")
    if problems:
        return PostconditionResult(False, f"employer {i['employer']}: " + "; ".join(problems))
    return PostconditionResult(True, f"employer {i['employer']}: locked " + ", ".join(locked))


EMPLOYER_LOCK_SPEC = KitWorkflowSpec(
    approver=ApproverScope.OWN_TENANT,  # the proposing tenant's own approvers
    kit=KIT,
    action="employer_rate_lock",
    validate=validate_lock,
    policy=lock_policy,
    steps=(StepSpec("quotes", build_lock_quotes),),
    approval_ttl=LOCK_TTL,
    read_allowlist=CHILD_READS,
    postcondition=lock_postcondition,
)

LOCK_SPEC = KitWorkflowSpec(
    approver=ApproverScope.OWN_TENANT,  # the proposing tenant's own approvers
    kit=KIT,
    action="lock_rates",
    validate=validate_batch,
    policy=lock_batch_policy,
    steps=(),
    approval_ttl=LOCK_TTL,
    read_allowlist=CHILD_READS,
    fan_out=lock_fan_out,
    child=EMPLOYER_LOCK_SPEC,
    child_tenant=child_tenant,
    agent_min_tier=RiskTier.AUTO,
)


def _specs() -> dict[str, KitWorkflowSpec]:
    from .release import RELEASE_SPEC

    return {
        PAYROLL_SPEC.action: PAYROLL_SPEC,
        LOCK_SPEC.action: LOCK_SPEC,
        RELEASE_SPEC.action: RELEASE_SPEC,
    }


SPECS: dict[str, KitWorkflowSpec] = _specs()
