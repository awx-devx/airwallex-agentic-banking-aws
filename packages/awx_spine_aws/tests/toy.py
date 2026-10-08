"""A toy kit and a toy Airwallex port shared by the awx_spine_aws tests.

The port follows the sandbox evidence in packages/awx_spine/FAKE-FIDELITY.md ("Transfers"):
transfers/create needs a body request_id and returns status SCHEDULED with a UUID id; a
second create with the same request_id is 400 duplicate_request_id naming the original; the
object is listable at GET /api/v1/transfers?request_id=...; it moves SCHEDULED -> SENT -> PAID
only through POST /api/v1/simulation/transfers/{id}/transition (unless `auto_move` stands in
for the outside world after `reads_until_final` reads). Faults per op name: OUTCOME_UNKNOWN
after the write landed, a crash (an exception) after it landed, a rejection, a 429. Faults
for uncertain sends:
`hide_list_reads` makes the next N `?request_id=` list reads miss every object (list lag after
a landed-but-invisible write) and `refuse_duplicates` makes a resend of an already-created
request_id answer 400 balance_insufficient instead of duplicate_request_id (the balance the
first send consumed); `rate_limit_duplicates` answers such a resend with a 429.

toy_spec records every kit callback it receives in `calls`, so tests can check which rule
called what, with which reason and at which PolicyContext.now.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from awx_common import Money, RiskTier, bind_operation, derive_request_id
from awx_common.approvals import ApprovalDecision, Decision, Proposal
from awx_common.ops import Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.workflow import (
    FinalitySpec,
    KitWorkflowSpec,
    PolicyDecision,
    PostconditionResult,
    StepContext,
    StepSpec,
)

T0 = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
READS = (r"^/api/v1/transfers/[A-Za-z0-9_-]+$", r"^/api/v1/balances/current$")
# A connected-account kit's platform list. The toy runs as the platform tenant too, so it lists
# everything the platform tenant reads; TENANT_ONLY is readable only inside a tenant.
PLATFORM_READS = (*READS, r"^/api/v1/transfers$")
TENANT_ONLY = r"^/api/v1/beneficiaries/[A-Za-z0-9_-]+$"
TRANSITION = "/api/v1/simulation/transfers/{id}/transition"


class Crash(RuntimeError):
    """Stands in for the adapter Lambda dying after its request left."""


class Clock:
    """now() and sleep() for one test: sleeping advances the clock, and so does each now()
    call by one second, so per-call timestamps (rule 6) are distinguishable."""

    def __init__(self, start: datetime = T0, tick: timedelta = timedelta(seconds=1)) -> None:
        self.t = start
        self.tick = tick
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        self.t += self.tick
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


def _fail(op: Operation, error_type: str, code: str, http: int) -> OperationResult:
    return OperationResult(
        request_id=op.request_id,
        status=OperationStatus.FAILED,
        http_status=http,
        error_type=error_type,
        error_code=code,
        error_message=code.replace("_", " "),
    )


@dataclass
class ToyPort:
    reads_until_final: int = 1
    final_status: str = "PAID"
    auto_move: bool = True  # False: only simulation transitions move a transfer (the sandbox)
    unknown_times: Counter = field(default_factory=Counter)  # op name -> faults left
    crash_times: Counter = field(default_factory=Counter)
    rate_limit_times: Counter = field(default_factory=Counter)
    fail_ops: set[str] = field(default_factory=set)
    hide_list_reads: int = 0
    refuse_duplicates: Counter = field(default_factory=Counter)
    rate_limit_duplicates: Counter = field(default_factory=Counter)  # 429 on resends only
    transition_fails: int = 0  # simulation transitions that 500 without moving
    created: dict[str, dict[str, Any]] = field(default_factory=dict)  # request_id -> object
    sends: list[Operation] = field(default_factory=list)
    reads: list[ReadRequest] = field(default_factory=list)
    balance: Money = field(default_factory=lambda: Money.of("1000.00", "AUD"))

    def execute(self, op: Operation) -> OperationResult:
        self.sends.append(op)
        if op.path.startswith("/api/v1/simulation/transfers/"):
            return self._transition(op)
        if self.rate_limit_times[op.name] > 0:
            self.rate_limit_times[op.name] -= 1
            return _fail(op, "RateLimited", "too_many_requests", 429)
        if op.name in self.fail_ops:
            return _fail(op, "InsufficientFunds", "balance_insufficient", 400)
        rid = op.body.get("request_id") or op.request_id
        if rid in self.created and self.rate_limit_duplicates[op.name] > 0:
            self.rate_limit_duplicates[op.name] -= 1
            return _fail(op, "RateLimited", "too_many_requests", 429)
        if rid in self.created and self.refuse_duplicates[op.name] > 0:
            self.refuse_duplicates[op.name] -= 1
            return _fail(op, "InsufficientFunds", "balance_insufficient", 400)
        if rid in self.created:
            obj = self.created[rid]
            if op.body.get("request_id"):  # the provider deduplicates on body request_id
                return OperationResult(
                    request_id=op.request_id,
                    status=OperationStatus.FAILED,
                    http_status=400,
                    provider_id=obj["id"],
                    error_type="DuplicateRequest",
                    error_code="duplicate_request_id",
                    error_message="request ID used before",
                )
            obj = self._create(op, key=f"{rid}#{len(self.created)}")  # ignores request_id
        else:
            obj = self._create(op, key=rid)
        if self.crash_times[op.name] > 0:
            self.crash_times[op.name] -= 1
            raise Crash("adapter Lambda timed out after the request left")
        if self.unknown_times[op.name] > 0:
            self.unknown_times[op.name] -= 1
            return OperationResult(
                request_id=op.request_id,
                status=OperationStatus.OUTCOME_UNKNOWN,
                error_type="OutcomeUnknown",
                error_message="timeout after submit",
            )
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.SUCCEEDED,
            http_status=201,
            provider_id=obj["id"],
            provider_status=obj["status"],
            body=self._public(obj),
        )

    def _create(self, op: Operation, *, key: str) -> dict[str, Any]:
        obj = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
            "request_id": op.body.get("request_id"),
            "status": "SCHEDULED",
            "transfer_amount": op.body.get("transfer_amount"),
            "transfer_currency": op.body.get("transfer_currency"),
            "beneficiary_id": op.body.get("beneficiary_id"),
            "transfer_method": op.body.get("transfer_method"),
            "reference": op.body.get("reference"),
            "_reads": 0,
            "_obo": op.on_behalf_of,
        }
        self.created[key] = obj
        return obj

    def _transition(self, op: Operation) -> OperationResult:
        tid = op.path.split("/")[-2]
        if self.transition_fails > 0:
            self.transition_fails -= 1
            return _fail(op, "TransientServerError", "operation_failed", 500)
        for obj in self.created.values():
            if obj["id"] == tid:
                nxt = op.body["next_status"]
                ok = (nxt == "SENT" and obj["status"] in ("SCHEDULED", "PROCESSING")) or (
                    nxt == "PAID" and obj["status"] == "SENT"
                )
                if not ok:
                    return _fail(op, "TransientServerError", "operation_failed", 500)
                obj["status"] = nxt
                return OperationResult(
                    request_id=op.request_id,
                    status=OperationStatus.SUCCEEDED,
                    http_status=200,
                    provider_id=tid,
                    provider_status=nxt,
                    body=self._public(obj),
                )
        return _fail(op, "ValidationFailed", "bad_request", 400)

    @staticmethod
    def _public(obj: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in obj.items() if not k.startswith("_")}

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        self.reads.append(req)
        if req.path == "/api/v1/balances/current":
            return {"items": [{"currency": "AUD", "available_amount": self.balance.amount}]}
        if req.path == "/api/v1/transfers":
            if self.hide_list_reads > 0:
                self.hide_list_reads -= 1
                return {"items": []}
            want = dict(req.query)
            items = [
                self._public(o)
                for o in self.created.values()
                if all(o.get(k) == v for k, v in want.items())
            ]
            return {"items": items}
        if req.path.startswith("/api/v1/beneficiaries/"):
            return {"id": req.path.rsplit("/", 1)[1]}
        tid = req.path.rsplit("/", 1)[1]
        for obj in self.created.values():
            if obj["id"] == tid:
                obj["_reads"] += 1
                if self.auto_move and obj["_reads"] >= self.reads_until_final:
                    obj["status"] = self.final_status
                return self._public(obj)
        raise KeyError(tid)


def transfer_finality(timeout_s: int = 60, *, sandbox: bool = False, max_advances: int = 6):
    def advance(op, result, body, now, attempt):
        nxt = {"SCHEDULED": "SENT", "PROCESSING": "SENT", "SENT": "PAID"}.get(body["status"])
        if nxt is None:
            return None
        return Operation(
            kit=op.kit,
            name="transfers.simulate_transition",
            method="POST",
            path=TRANSITION.format(id=result.provider_id),
            body={"next_status": nxt},
            request_id=derive_request_id(op.kit, op.business_key, f"advance-{attempt}"),
            business_key=op.business_key,
            intent_hash=op.intent_hash,
            on_behalf_of=op.on_behalf_of,
        )

    return FinalitySpec(
        read_request=lambda r: ReadRequest(f"/api/v1/transfers/{r.provider_id}"),
        status_of=lambda body: body["status"],
        terminal=frozenset({"PAID", "CANCELLED", "FAILED"}),
        success=frozenset({"PAID"}),
        interval_s=5,
        timeout_s=timeout_s,
        sandbox_advance=advance if sandbox else None,
        max_sandbox_advances=max_advances,
    )


def reconcile_by_request_id(op: Operation, read) -> Mapping[str, Any] | None:
    items = read(ReadRequest("/api/v1/transfers", {"request_id": op.request_id}))["items"]
    for item in items:  # check the object against the operation before trusting it
        if (
            item.get("transfer_amount") == op.body["transfer_amount"]
            and item.get("transfer_currency") == op.body["transfer_currency"]
        ):
            return item
    return None


def _validate(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    amount = raw["amount"]
    if not isinstance(amount, str):
        raise TypeError("amount must be a decimal string")
    return {"payee": str(raw["payee"]), "amount": Money.of(amount, "AUD")}


def _available(body: Mapping[str, Any]) -> Money:
    for item in body["items"]:
        if item["currency"] == "AUD":
            return Money.of(item["available_amount"], "AUD")
    return Money.of("0.00", "AUD")


def _policy(tier: RiskTier, calls: list):
    def policy(intent, ctx) -> PolicyDecision:
        calls.append(("policy", ctx.now))
        balance = _available(ctx.read(ReadRequest("/api/v1/balances/current")))
        fee = Money.of("1.50", "AUD")
        if intent["amount"] + fee > balance:
            return PolicyDecision(RiskTier.BLOCKED, ("insufficient balance",), {"fee": fee})
        return PolicyDecision(tier, ("toy",), {"fee": fee, "total": intent["amount"] + fee})

    return policy


def _endpoint(request_id_in_body: bool) -> tuple[str, str]:
    """request_id_in_body=False stands for an endpoint that ignores request_id. The sandbox
    rejects transfers/create without one, so use an endpoint awx_spine's table records as
    not deduplicating (a replayed request_id created a second beneficiary, FAKE-FIDELITY.md)."""
    if request_id_in_body:
        return "transfers.create", "/api/v1/transfers/create"
    return "beneficiaries.create", "/api/v1/beneficiaries/create"


def transfer_ops(
    step: str,
    *,
    bind: bool = True,
    on_behalf_of: str | None = None,
    intent_hash: str | None = None,
    calls: list | None = None,
    request_id_in_body: bool = True,
):
    def build(ctx: StepContext) -> tuple[Operation, ...]:
        if calls is not None:
            calls.append((f"build:{step}", ctx.policy.now))
        # The business key names the object being paid, never an intent hash.
        key = f"supplier-invoice:{ctx.intent['payee']}"
        amount = ctx.decision.computed["total"] if step == "pay" else ctx.decision.computed["fee"]
        rid = derive_request_id("k3", key, step)
        body: dict[str, Any] = {
            "transfer_amount": amount.amount,
            "transfer_currency": "AUD",
            "transfer_method": "LOCAL",
            "beneficiary_id": f"ben-{ctx.intent['payee']}",
            "reference": f"poc-s2-{step}",
        }
        if request_id_in_body:
            body["request_id"] = rid
        name, path = _endpoint(request_id_in_body)
        op = Operation(
            kit="k3",
            name=name,
            method="POST",
            path=path,
            body=body,
            request_id=rid,
            business_key=key,
            intent_hash=intent_hash or ctx.intent_hash,
            on_behalf_of=on_behalf_of,
        )
        if bind and ctx.policy.tenant.connected_account_id is not None:
            op = bind_operation(op, ctx.policy.tenant)  # rule 4: the kit binds, not the runner
        return (op,)

    return build


def toy_spec(
    tier: RiskTier = RiskTier.AUTO,
    *,
    calls: list | None = None,
    postcondition_ok: bool = True,
    finality_timeout_s: int = 60,
    fee_build=None,
    pay_build=None,
    ttl: timedelta = timedelta(hours=1),
    action: str = "pay_supplier",
    reconcile: bool | Any = False,  # True: reconcile_by_request_id; or a reconcile callable
    sandbox_finality: bool = False,
    agent_min_tier: RiskTier = RiskTier.SINGLE,
) -> KitWorkflowSpec:
    calls = [] if calls is None else calls
    hook = reconcile if callable(reconcile) else (reconcile_by_request_id if reconcile else None)

    def post(ctx: StepContext) -> PostconditionResult:
        calls.append(("postcondition", ctx.policy.now))
        if ctx.policy.store is not None:
            item = ctx.policy.store.get("runs")
            count = 0 if item is None else item.value["count"]
            ctx.policy.store.put(
                "runs",
                {"count": count + 1},
                expected_version=None if item is None else item.version,
            )
        pay = ctx.prior.get("pay") or ()
        paid = bool(pay) and pay[0].provider_status == "PAID"
        return PostconditionResult(paid and postcondition_ok, "pay leg is PAID")

    def on_abandon(ctx: StepContext, reason: str) -> None:
        calls.append((f"abandon:{reason}", ctx.policy.now))

    return KitWorkflowSpec(
        kit="k3",
        action=action,
        validate=_validate,
        policy=_policy(tier, calls),
        steps=(
            StepSpec(
                "fee",
                fee_build or transfer_ops("fee", calls=calls),
                reconcile=hook,
            ),
            StepSpec(
                "pay",
                pay_build or transfer_ops("pay", calls=calls),
                transfer_finality(finality_timeout_s, sandbox=sandbox_finality),
                reconcile=hook,
            ),
        ),
        approval_ttl=ttl,
        read_allowlist=(*READS, r"^/api/v1/transfers$", TENANT_ONLY),
        platform_read_allowlist=PLATFORM_READS,
        postcondition=post,
        on_abandon=on_abandon,
        agent_min_tier=agent_min_tier,
    )


def fan_out_spec(
    child_tier: RiskTier = RiskTier.AUTO,
    *,
    child_tenant=None,
    calls: list | None = None,
    child_tiers: Mapping[str, RiskTier] | None = None,
    list_children: bool = True,
) -> KitWorkflowSpec:
    calls = [] if calls is None else calls
    child = toy_spec(child_tier, action="pay_one", calls=calls)
    if child_tiers:
        base = child.policy

        def child_policy(intent, ctx):
            d = base(intent, ctx)
            tier = child_tiers.get(intent["payee"], d.risk_tier)
            return PolicyDecision(tier, d.reasons, d.computed)

        child = type(child)(**{**child.__dict__, "policy": child_policy})

    def children_of(intent):
        return [{"payee": p, "amount": intent["amount"]} for p in intent["payees"]]

    def parent_policy(intent, ctx):
        calls.append(("parent_policy", ctx.read_tenant is not None))
        listed = children_of(intent) if list_children else []
        return PolicyDecision(RiskTier.SINGLE, ("batch",), {"children": listed})

    return KitWorkflowSpec(
        kit="k3",
        action="pay_many",
        validate=lambda raw: {"payees": list(raw["payees"]), "amount": raw["amount"]},
        policy=parent_policy,
        steps=(),
        approval_ttl=timedelta(hours=1),
        read_allowlist=READS,
        fan_out=lambda intent, d: children_of(intent),
        child=child,
        child_tenant=child_tenant,
    )


def approve_by(*approvers: str, at: datetime | None = None, decision: Decision = Decision.APPROVE):
    def decide(p: Proposal) -> list[ApprovalDecision]:
        when = at or p.created_at + timedelta(minutes=1)
        return [
            ApprovalDecision(p.proposal_id, p.intent_hash, a, decision, when) for a in approvers
        ]

    return decide


def no_decision(p: Proposal) -> list[ApprovalDecision]:
    return []


RAW = {"payee": "ben_1", "amount": "100.00"}
DEC = Decimal


def fee_op(payee: str = "ben_1", *, request_id_in_body: bool = True) -> Operation:
    """The Operation transfer_ops("fee") builds for `payee`, as an earlier run emitted it.
    intent_hash differs per proposal but is not part of the body hash, so a later run's
    operation is a re-emission of this one (kit rule M1)."""
    key = f"supplier-invoice:{payee}"
    rid = derive_request_id("k3", key, "fee")
    body: dict[str, Any] = {
        "transfer_amount": Money.of("1.50", "AUD").amount,
        "transfer_currency": "AUD",
        "transfer_method": "LOCAL",
        "beneficiary_id": f"ben-{payee}",
        "reference": "poc-s2-fee",
    }
    if request_id_in_body:
        body["request_id"] = rid
    name, path = _endpoint(request_id_in_body)
    return Operation(
        kit="k3",
        name=name,
        method="POST",
        path=path,
        body=body,
        request_id=rid,
        business_key=key,
        intent_hash="e" * 64,
    )


def seed_unknown(store, port: ToyPort, *, landed: bool, request_id_in_body: bool = True):
    """An earlier run's operation record left at OUTCOME_UNKNOWN; `landed` says whether the
    write in fact reached the provider."""
    op = fee_op(request_id_in_body=request_id_in_body)
    store.begin(op)
    store.record_attempt(op.request_id)
    store.complete(
        op.request_id,
        OperationResult(
            request_id=op.request_id,
            status=OperationStatus.OUTCOME_UNKNOWN,
            error_type="OutcomeUnknown",
            error_message="earlier run: timeout after submit",
        ),
    )
    if landed:
        port._create(op, key=op.body.get("request_id") or op.request_id)
    return op


def reconcile_wrong_amount(op: Operation, read) -> Mapping[str, Any] | None:
    """A reconcile that finds an object holding this request_id but for another amount (a
    reused or adopted request_id): it must never count as this payment."""
    items = read(ReadRequest("/api/v1/transfers", {"request_id": op.request_id}))["items"]
    if not items:
        return None
    return {**items[0], "transfer_amount": op.body["transfer_amount"] + Decimal("1.00")}
