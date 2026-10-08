"""Shared pieces of the three kit 8 specs: input validation, tiers, reserve targets."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from awx_common import Money, RiskTier
from awx_common.errors import ValidationFailed
from awx_common.store import KitStore
from awx_common.workflow import PolicyContext, PolicyDecision

from . import store as st
from . import watch as wt
from .ledger import CycleInput, CycleSettlement, SellerLedger, settle_cycle
from .reserve import ReservePolicy, rate_target, reserve_rate

_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
APPROVAL_TTL = timedelta(hours=4)

# How long a run can still act after its resolve Build task has started. decided_at is
# written inside that task, so after decided_at + BUILD_RETRY_BUDGET the original run can no
# longer touch its record. The synthesised Build state carries CDK's LambdaInvoke default
# retry: MaxAttempts 6 RETRIES (7 attempts), IntervalSeconds 2, BackoffRate 2, service errors
# only; each attempt is a 60 s step Lambda. 7 x 60 s + (2+4+8+16+32+64) s = 546 s (about
# 9 min). tests/test_synth_k8.py reads these off the synthesised state machines. The resolve
# spec has no step after record_decision. The adoption wait (ADOPTION_WAIT) must stay longer.
# PolicyContext.run_status could replace this budget; the kit does not use it yet.
STEP_LAMBDA_TIMEOUT_S = 60
BUILD_TASK_RETRIES = 6
BUILD_RETRY_INTERVAL_S = 2
BUILD_RETRY_BACKOFF = 2
BUILD_TASK_ATTEMPTS = BUILD_TASK_RETRIES + 1
BUILD_RETRY_BUDGET = timedelta(
    seconds=STEP_LAMBDA_TIMEOUT_S * BUILD_TASK_ATTEMPTS
    + sum(BUILD_RETRY_INTERVAL_S * BUILD_RETRY_BACKOFF**i for i in range(BUILD_TASK_RETRIES))
)
ADOPTION_WAIT = APPROVAL_TTL


log = logging.getLogger(__name__)


def strict(raw: Mapping[str, Any], required: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ValidationFailed("tool input must be an object")
    extra = set(raw) - set(required)
    if extra:
        raise ValidationFailed(f"unexpected fields {sorted(extra)}")
    out: dict[str, str] = {}
    for name in required:
        value = raw.get(name)
        if not isinstance(value, str) or not _ID.match(value):
            raise ValidationFailed(f"{name} is missing or malformed")
        out[name] = value
    return out


def blocked(*reasons: str) -> PolicyDecision:
    return PolicyDecision(RiskTier.BLOCKED, tuple(reasons))


def tier(material: bool, dual: bool) -> RiskTier:
    if dual:
        return RiskTier.DUAL
    return RiskTier.SINGLE if material else RiskTier.AUTO


def mmax(a: Money, b: Money) -> Money:
    return a if a >= b else b


def mmin(a: Money, b: Money) -> Money:
    return a if a <= b else b


def scope_or_block(ctx: PolicyContext) -> PolicyDecision | None:
    if not ctx.tenant.connected_account_id:
        return blocked("tenant has no connected account")
    if ctx.store is None:
        return blocked("no kit store bound to this context")
    return None


MISMATCH = "the tenants table and the seeded account disagree"
NOT_SEEDED = "this seller has no seeded connected account; run seed_seller first"


def account_problem(ctx: PolicyContext) -> str | None:
    """The run tenant's connected account (the tenants table) must be the account
    seed_seller checked and bound to this seller. The reason the tenant sees is fixed text with
    no account id; both ids go to the operator log only."""
    if ctx.store is None:
        return "no kit store bound to this context"
    seeded = st.seeded_account(ctx.store)
    if seeded is None:
        return NOT_SEEDED
    if seeded != ctx.tenant.connected_account_id:
        log.warning(
            "kit k8 tenant %s: tenants row account %s, seeded account %s",
            ctx.tenant.tenant_id,
            ctx.tenant.connected_account_id,
            seeded,
        )
        return MISMATCH
    return None


def account_or_block(ctx: PolicyContext) -> PolicyDecision | None:
    """Every kit 8 policy runs this first: nothing is proposed, read or sent for a seller whose
    tenants row and seeded account disagree."""
    if (bad := scope_or_block(ctx)) is not None:
        return bad
    if (problem := account_problem(ctx)) is not None:
        return blocked(problem)
    return None


def open_exposure(kstore: KitStore, seller: str, currency: str, order_ids: Iterable[str]) -> Money:
    book = {o.order_id: o for o in st.orders(kstore, seller)}
    total = Money.zero(currency)
    for oid in order_ids:
        o = book.get(oid)
        if o is not None and not o.delivered:
            total = total + o.open_value
    return total


def planned_target(
    kstore: KitStore,
    policy: ReservePolicy,
    seller: str,
    cycle_id: str,
    *,
    exclude_event: str | None = None,
) -> tuple[Money, Money]:
    """(rate-based target, effective target after carried-forward evidence holds).

    An approved evidence hold applies to every later cycle, but only for the value of its orders
    that are still undelivered and unrefunded, capped at the approved target. So the hold falls
    away by itself as the carrier delivers or the refunds are paid.
    """
    prof = st.profile(kstore)
    cyc = st.cycle(kstore, cycle_id)
    rate = reserve_rate(policy, prof.trailing_refund_rate, prof.tenure_days)
    base = rate_target(cyc.volume_base, rate)
    effective = base
    for o in st.overrides(kstore):
        if o.event_id == exclude_event:
            continue
        hold = mmin(o.target, open_exposure(kstore, seller, prof.currency, o.order_ids))
        effective = mmax(effective, hold)
    return base, effective


def compute_settlement(
    kstore: KitStore, state: st.LedgerState, policy: ReservePolicy, seller: str, cycle_id: str
) -> tuple[CycleSettlement, SellerLedger]:
    cyc = st.cycle(kstore, cycle_id)
    _, target = planned_target(kstore, policy, seller, cycle_id)
    return settle_cycle(
        state.ledger,
        CycleInput(cycle_id, cyc.entitlement, cyc.refunds, target),
        net_receivables=policy.net_receivables,
        # A charge in flight, or ruled not_landed but not yet confirmed, may already have
        # collected this receivable: never net it off a payout (M1).
        receivable_in_flight=_sum(
            st.charges_in_flight(state), wt.unconfirmed_charge_amount(kstore)
        ),
    )


def _sum(*parts):
    vals = [p for p in parts if p is not None]
    if not vals:
        return None
    total = vals[0]
    for v in vals[1:]:
        total = total + v
    return total


def identity_problem(state: st.LedgerState, s: CycleSettlement | None) -> str | None:
    if s is not None and not s.residual().is_zero():
        return f"cycle identity off by {s.residual()}"
    if not state.ledger.residual().is_zero():
        return f"seller identity off by {state.ledger.residual()}"
    return None
