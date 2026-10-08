"""propose_lock_rate: lock today's FX rate for the conversions a plan needs (AUTO).

Why: M3 binds every sell amount to the approval. Bound to a live indicative rate, the rule-1
re-run at execution goes STALE whenever the rate moves between approval and execution, which
in a demo is always. So the rate is locked first: this action creates one Airwallex quote per
bought currency (validity `PolicyConfig.lock_validity`) and stores it under ratelock/<CUR>.
execute_plan's policy then reads the STORED quote, so its computed carries the exact sell, and
the approval stays valid until the quote expires. An expired, used or mismatched lock is
BLOCKED: lock again.

AUTO for agent proposals (DESIGN.md): it moves no money and binds nothing; it only fixes the price a
later, separately approved execute_plan may book at. fx/quotes/create ignores request_id
(FAKE-FIDELITY), so a retried lock at worst creates a spare quote, and the latest lock wins.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from awx_common import Operation, OperationStatus, RiskTier, derive_request_id
from awx_common.workflow import (
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    StepContext,
    StepSpec,
)

from .api import FX_QUOTES_CREATE, READ_ALLOWLIST, read_balances, read_rates
from .intake import parse_obligation_ids, require_keys
from .keys import KIT, book_id_of, op
from .locks import need_store, read_ratelock, write_ratelock
from .money_rules import BadProviderData, parse_quote
from .plan import FUNDED, build_plan
from .policy import PolicyConfig

ACTION = "lock_rate"


def ratelock_business_key(book_id: str, currency: str) -> str:
    return f"poc-k1-{book_id}-ratelock-{currency}"


def build_lock_spec(config: PolicyConfig | None = None) -> KitWorkflowSpec:
    from .workflow import book_prelude

    cfg = config or PolicyConfig()

    def validate(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        require_keys(raw, {"obligation_ids"}, "propose_lock_rate input")
        return {"obligation_ids": parse_obligation_ids(raw.get("obligation_ids"))}

    def policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        ids = list(intent["obligation_ids"])
        pre = book_prelude(ids, ctx, cfg)
        if isinstance(pre, PolicyDecision):
            return pre
        book, book_id = pre
        balances = read_balances(ctx.read, book)
        rates = read_rates(ctx.read, book, {book.obligation(i).amount.currency for i in ids})
        plan = build_plan(book, balances, rates, cfg.plan, only=ids)
        unfunded = [d for d in plan.decisions if d.kind not in FUNDED]
        if unfunded:
            return PolicyDecision(
                RiskTier.BLOCKED,
                tuple(f"{d.obligation_id}: {d.kind.value.lower()} ({d.reason})" for d in unfunded),
            )
        if plan.breaches:
            return PolicyDecision(
                RiskTier.BLOCKED, (f"plan breaches the floor in {plan.breaches}",)
            )
        if not plan.conversions:
            return PolicyDecision(RiskTier.BLOCKED, ("no conversion needed; nothing to lock",))
        converted = {d.obligation_id: d.buy for d in plan.decisions if d.buy is not None}
        computed = {
            "book_id": book_id,
            "book_version": book.book_version,
            "validity": cfg.lock_validity,
            # The quote each new lock replaces. A retry of an unchanged lock request is the same
            # proposal (same ID, same execution); a re-lock after the old quote was used or
            # expired is a new one, whose quote request_id differs.
            "replaces": {
                c.buy.currency: (read_ratelock(ctx.store, c.buy.currency) or {}).get("quote_id")
                for c in plan.conversions
            },
            "conversions": [
                {
                    "currency": c.buy.currency,
                    "buy": c.buy,
                    "sell_currency": c.sell.currency,
                    "funds": sorted(
                        o for o, b in converted.items() if b.currency == c.buy.currency
                    ),
                }
                for c in plan.conversions
            ],
        }
        return PolicyDecision(RiskTier.AUTO, ("a rate lock moves no money",), computed)

    def build_quotes(ctx: StepContext) -> Sequence[Operation]:
        computed = ctx.decision.computed
        book_id = book_id_of(computed)
        ops = []
        for c in computed["conversions"]:
            key = ratelock_business_key(book_id, c["currency"])
            # fx/quotes/create ignores request_id, so the body carries none; the Operation's id
            # labels the record, unique per run.
            rid = derive_request_id(KIT, key, f"quote:{ctx.proposal_id}")
            body = {
                "buy_currency": c["buy"].currency,
                "sell_currency": c["sell_currency"],
                "buy_amount": c["buy"].amount,
                "validity": computed["validity"],
            }
            ops.append(op(ctx, "fx.quotes.create", FX_QUOTES_CREATE, body, rid, key))
        return ops

    def postcondition(ctx: StepContext) -> PostconditionResult:
        store = need_store(ctx.policy.store)
        computed = ctx.decision.computed
        wanted = {c["currency"]: c for c in computed["conversions"]}
        locked, problems = [], []
        for r in ctx.prior.get("quote", ()):
            if r.status is not OperationStatus.SUCCEEDED or not r.body:
                problems.append(f"quote {r.status.value}")
                continue
            try:
                q = parse_quote(r.body)
            except BadProviderData as exc:
                problems.append(str(exc))
                continue
            c = wanted.get(q.buy.currency)
            if c is None or q.buy != c["buy"] or q.sell.currency != c["sell_currency"]:
                problems.append(f"quote {q.quote_id} does not match the plan")
                continue
            write_ratelock(
                store,
                q.buy.currency,
                {
                    "quote_id": q.quote_id,
                    "buy": q.buy,
                    "sell": q.sell,
                    "valid_to": q.valid_to,
                    "book_id": book_id_of(computed),
                    "funds": list(c["funds"]),
                    "locked_by": ctx.proposal_id,
                    "locked_at": ctx.policy.now,
                },
            )
            locked.append(f"{q.buy} for {q.sell} until {q.valid_to.isoformat()}")
        if problems or len(locked) != len(wanted):
            return PostconditionResult(False, "; ".join(problems) or "a quote is missing")
        return PostconditionResult(True, "locked: " + "; ".join(locked))

    return KitWorkflowSpec(
        kit=KIT,
        action=ACTION,
        validate=validate,
        policy=policy,
        steps=(StepSpec("quote", build_quotes),),
        approval_ttl=cfg.lock_approval_ttl,
        read_allowlist=READ_ALLOWLIST,
        postcondition=postcondition,
        agent_min_tier=RiskTier.AUTO,  # moves no money (module docstring, DESIGN.md)
    )
