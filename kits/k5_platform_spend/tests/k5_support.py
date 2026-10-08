"""Kit 5 fixtures: the real runner (`awx_spine_aws.local_runner.run_kit_workflow`) against
`awx_spine.FakeAirwallex`, with one fake clock shared by the runner, the executor and the stores.

Only kit-specific setup lives here: three connected accounts, the platform book (capital
ledger over the platform KitStore), the tenant map, approval decisions and a few helpers the
sandbox cannot provoke itself (a movement ending FAILED or SUSPENDED, an adapter crash after the
call left). Validation, policy, approval, the rule-1 re-run, tenant scope, the executor,
reconcile, finality, postcondition and on_abandon all run in the spine's own code.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from awx_common import Money, Operation, OperationResult, ReadRequest, TenantContext
from awx_common.approvals import ApprovalDecision, Decision, Origin, Proposal, RiskTier
from awx_common.store import InMemoryKitStore, KitStore
from awx_common.workflow import KitWorkflowSpec, PolicyDecision, StepContext
from awx_k5_platform_spend import (
    CardProgram,
    Obligation,
    PlatformBook,
    PlatformConfig,
    PlatformSetup,
    TenantMap,
    TenantRecord,
    build_specs,
)
from awx_spine.fake import EPOCH, FakeAirwallex
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.local_runner import WorkflowOutcome, run_kit_workflow

USD = "USD"
TODAY = EPOCH.date()
TENANT_IDS = ("t-alpha", "t-bravo", "t-charlie")
LABEL = "poc-i5-"


def usd(x: str | int) -> Money:
    return Money.of(x, USD)


def obligation(tenant: str, oid: str, due: str, *, deposit_days: int = 2) -> Obligation:
    return Obligation(oid, tenant, usd(due), TODAY, TODAY + timedelta(days=deposit_days), "due")


def config(*, floor: str = "600", high_risk: str = "1000") -> PlatformConfig:
    return PlatformConfig(
        currency=USD,
        reserve_floor=usd(floor),
        high_risk_bridge=usd(high_risk),
        max_bridge_per_tenant=usd("800"),
        max_bridge_days=7,
        card_programs={
            "standard": CardProgram("standard", USD, usd("100"), usd("250"), (USD,), ("5734",)),
        },
    )


Decide = Callable[[Proposal], Sequence[ApprovalDecision]]


def approve(p: Proposal) -> list[ApprovalDecision]:
    n = 2 if p.risk_tier is RiskTier.DUAL else 1
    return [
        ApprovalDecision(
            p.proposal_id, p.intent_hash, f"approver-{i}", Decision.APPROVE, p.created_at
        )
        for i in range(n)
    ]


def reject(p: Proposal) -> list[ApprovalDecision]:
    return [
        ApprovalDecision(p.proposal_id, p.intent_hash, "approver-0", Decision.REJECT, p.created_at)
    ]


def expire(p: Proposal) -> list[ApprovalDecision]:
    return []


class CrashAfterSend:
    """An AirwallexPort whose next `times` sends of `op_name` reach the fake and then crash, as
    an adapter Lambda that dies after its call left (rule 9)."""

    def __init__(self, inner: FakeAirwallex, op_name: str, times: int = 1) -> None:
        self.inner, self.op_name, self.times = inner, op_name, times

    def execute(self, op: Operation) -> OperationResult:
        result = self.inner.execute(op)
        if op.name == self.op_name and self.times > 0:
            self.times -= 1
            raise RuntimeError("adapter crashed after the call left")
        return result

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        return self.inner.read(req)


class FailingReads:
    """An AirwallexPort whose GETs of `path` fail with a non-transient ValidationFailed, as a
    read the spine must not retry through (finality polls retry only transient errors)."""

    def __init__(self, inner: FakeAirwallex, path: str) -> None:
        self.inner, self.path = inner, path

    def execute(self, op: Operation) -> OperationResult:
        return self.inner.execute(op)

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        if req.path == self.path:
            from awx_common.errors import ValidationFailed

            raise ValidationFailed("injected non-transient read failure", http_status=400)
        return self.inner.read(req)


@dataclass
class _Env:
    """engine.Env for building contexts outside a run (step-level tests)."""

    platform: Platform
    tenant_store: KitStore
    sandbox: bool = True

    def now(self) -> datetime:
        return self.platform.now()

    def read(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return self.platform.fake.read(req)

    def read_platform(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return self.platform.fake.read(req)

    def store(self, kit: str, tenant: TenantContext) -> KitStore:
        if tenant.tenant_id == engine.PLATFORM_TENANT_ID:
            return self.platform.platform_store
        return self.tenant_store

    def resolve_tenant(self, tenant_id: str) -> TenantContext:
        return self.platform.book.tenants.resolve(tenant_id)


class Platform:
    """One kit 5 platform on one fake sandbox."""

    def __init__(
        self,
        *,
        platform_usd: str = "1000",
        capital: str = "1000",
        floor: str = "600",
        high_risk: str = "1000",
        wallets: Mapping[str, str] | None = None,
        obligations: Sequence[Obligation] = (),
        late: Mapping[str, int] | None = None,
        inactive: Sequence[str] = (),
        platform_store: KitStore | None = None,
        **fake_kw: Any,
    ) -> None:
        self.fake = FakeAirwallex(balances={USD: usd(platform_usd)}, **fake_kw)
        start = {"t-alpha": "200", "t-bravo": "150", "t-charlie": "900", **(wallets or {})}
        self.acct = {
            t: self.fake.add_connected_account(f"{LABEL}{t}", {USD: usd(start[t])})
            for t in TENANT_IDS
        }
        tenants = TenantMap(
            TenantRecord(t, self.acct[t], t, active=t not in inactive) for t in TENANT_IDS
        )
        self.setup = PlatformSetup.create(
            config(floor=floor, high_risk=high_risk),
            tenants,
            obligations=obligations,
            monthly_fees={t: usd("25") for t in TENANT_IDS},
            late_repayments=late,
        )
        self.platform_store = platform_store if platform_store is not None else InMemoryKitStore()
        self.book = PlatformBook(self.setup, self.platform_store)
        if capital != "0":
            self.book.ledger.fund("capital-1", usd(capital), at=self.now())
        self.specs = build_specs(lambda ctx: self.setup)
        self.ops = InMemoryOperationStore()
        self.tenant_stores: dict[str, InMemoryKitStore] = defaultdict(InMemoryKitStore)
        self.audit: list[Mapping[str, Any]] = []

    # ---- clock and tenants ------------------------------------------------------------

    def now(self) -> datetime:
        return EPOCH + timedelta(seconds=self.fake.clock())

    def advance(self, **delta: float) -> None:
        self.fake.advance(timedelta(**delta).total_seconds())

    def tenant(self, tenant_id: str) -> TenantContext:
        return self.book.tenants.resolve(tenant_id)

    def wallet(self, tenant_id: str | None) -> Money:
        return self.fake.balance(USD, self.acct[tenant_id] if tenant_id else None)

    def deposit(self, tenant_id: str, amount: str) -> None:
        """A customer deposit landing, through a Global Account (the guide's flow)."""
        acct = self.acct[tenant_id]
        if not hasattr(self, "_ga"):
            self._ga: dict[str, str] = {}
        if tenant_id not in self._ga:
            op = Operation(
                kit="k5",
                name="global_accounts.create",
                method="POST",
                path="/api/v1/global_accounts/create",
                body={
                    "request_id": f"{LABEL}ga-{tenant_id}-{self.fake.clock()}",
                    "country_code": "US",
                    "required_features": [{"currency": USD, "transfer_method": "LOCAL"}],
                },
                request_id=f"{LABEL}ga-{tenant_id}-{self.fake.clock()}",
                business_key=f"fixture-ga-{tenant_id}",
                intent_hash="0" * 64,
                on_behalf_of=acct,
            )
            self._ga[tenant_id] = str(self.fake.execute(op).provider_id)
        self.fake.simulate(
            "deposit", global_account_id=self._ga[tenant_id], amount=amount, on_behalf_of=acct
        )

    def spend(self, tenant_id: str, amount: str) -> None:
        """The customer's payment due goes out of its wallet (outside this kit)."""
        self.fake.credit(USD, f"-{usd(amount).amount}", account=self.acct[tenant_id])

    def force_movement_status(self, kind: str, request_id: str, status: str) -> None:
        """FAILED and SUSPENDED cannot be provoked in the sandbox (FAKE-FIDELITY (d))."""
        self.fake.set_movement_status(request_id, status)

    def movements(self, kind: str) -> list[dict[str, Any]]:
        return [
            o for o in self.fake.objects(kind) if str(o.get("reference", "")).startswith("poc-k5-")
        ]

    def sent(self, op_name: str) -> list[tuple[str, str, str | None]]:
        path = engine_path(op_name)
        return [r for r in self.fake.requests if r[0] == "POST" and r[1] == path]

    # ---- running ----------------------------------------------------------------------

    def run(
        self,
        action: str,
        raw: Mapping[str, Any],
        *,
        tenant: str,
        decide: Decide = approve,
        origin: Origin = Origin.AGENT,
        port: Any = None,
        proposed_by: str = "requester",
    ) -> WorkflowOutcome:
        return run_kit_workflow(
            self.specs[action],
            raw,
            proposed_by=proposed_by,
            tenant=self.tenant(tenant),
            port=port or self.fake,
            store=self.ops,
            decide=decide,
            now=self.now,
            sleep=self.fake.advance,
            audit=self.audit.append,
            kit_store=self.tenant_stores[tenant],
            platform_store=self.platform_store,
            tenants=self.book.tenants,
            origin=origin,
        )

    def step_context(
        self,
        action: str,
        raw: Mapping[str, Any],
        *,
        tenant: str,
        proposal_id: str,
        prior: Mapping[str, Sequence[OperationResult]] | None = None,
        decision: PolicyDecision | None = None,
    ) -> StepContext:
        """A StepContext built by the engine's own policy_context (for step-level tests)."""
        spec: KitWorkflowSpec = self.specs[action]
        env = _Env(self, self.tenant_stores[tenant])
        t = self.tenant(tenant)
        intent = spec.validate(raw)
        if decision is None:
            decision = spec.policy(intent, engine.policy_context(spec, env, t, pure=True))
        return StepContext(
            intent=intent,
            decision=decision,
            policy=engine.policy_context(spec, env, t),
            intent_hash="0" * 64,
            prior=dict(prior or {}),
            proposal_id=proposal_id,
        )


def engine_path(op_name: str) -> str:
    return {
        "connected_account_transfers.create": "/api/v1/connected_account_transfers/create",
        "charges.create": "/api/v1/charges/create",
        "issuing.simulate_authorization": "/api/v1/simulation/issuing/create",
        "issuing.cardholders.create": "/api/v1/issuing/cardholders/create",
        "issuing.cards.create": "/api/v1/issuing/cards/create",
    }[op_name]


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=EPOCH.tzinfo)


# ---- static fixtures for pure unit tests (no port) ----------------------------------------

NOW = EPOCH
ACCT = {"t-alpha": "acct_alpha0001", "t-bravo": "acct_bravo0002", "t-charlie": "acct_charlie003"}


def make_book(*, capital: str = "1000", floor: str = "600") -> PlatformBook:
    """A book over a fixed tenant map, for operation-builder, ledger and allocator unit tests."""
    setup = PlatformSetup.create(
        config(floor=floor),
        TenantMap(TenantRecord(t, a, t) for t, a in ACCT.items()),
        monthly_fees={t: usd("25") for t in ACCT},
    )
    book = PlatformBook(setup, InMemoryKitStore())
    if capital != "0":
        book.ledger.fund("capital-1", usd(capital), at=NOW)
    return book


# ---- operator commands ----------------------------------------------------------------------


def setup_args(p: Platform, **overrides: Any) -> dict[str, Any]:
    """The JSON args an operator passes to seed_platform_setup for this platform."""
    cfg = p.setup.config
    args: dict[str, Any] = {
        "currency": cfg.currency,
        "reserve_floor": str(cfg.reserve_floor.amount),
        "high_risk_bridge": str(cfg.high_risk_bridge.amount),
        "max_bridge_per_tenant": str(cfg.max_bridge_per_tenant.amount),
        "max_bridge_days": cfg.max_bridge_days,
        "card_programs": {
            n: {
                "per_transaction_limit": str(c.per_transaction_limit.amount),
                "monthly_limit": str(c.monthly_limit.amount),
                "allowed_currencies": list(c.allowed_currencies),
                "allowed_merchant_categories": list(c.allowed_merchant_categories),
            }
            for n, c in cfg.card_programs.items()
        },
        "tenants": [
            {
                "tenant_id": r.tenant_id,
                "connected_account_id": r.connected_account_id,
                "display_name": r.display_name,
                "active": r.active,
            }
            for r in p.setup.tenants.records()
        ],
        "obligations": [
            {
                "obligation_id": o.obligation_id,
                "tenant_id": o.tenant_id,
                "amount_due": str(o.amount_due.amount),
                "due_on": o.due_on.isoformat(),
                "expected_deposit_on": o.expected_deposit_on.isoformat(),
            }
            for o in p.setup.obligations.values()
        ],
        "monthly_fees": {t: str(m.amount) for t, m in p.setup.monthly_fees.items()},
        "late_repayments": dict(p.setup.late_repayments),
    }
    args.update(overrides)
    return args


def operator_context(p: Platform, platform_store: KitStore, *, online: bool = True):
    from awx_common.operator_commands import OperatorContext

    return OperatorContext(
        kit="k5",
        platform_store=platform_store,
        store_for=lambda tid: p.tenant_stores[tid],
        now=p.now(),
        read=p.fake.read if online else None,
    )


def seed_by_operator(p: Platform, platform_store: KitStore, capital: str = "1000") -> None:
    """Deploy-time state through the shipped operator commands only."""
    from awx_common.operator_commands import run_operator_command
    from awx_k5_platform_spend import OPERATOR_COMMANDS

    ctx = operator_context(p, platform_store)
    run_operator_command(OPERATOR_COMMANDS, "seed_platform_setup", ctx, setup_args(p))
    run_operator_command(
        OPERATOR_COMMANDS, "fund_capital", ctx, {"key": "initial", "amount": capital}
    )


# ---- inputs for every shipped tool (test_tool_surface, test_operator_commands) ----

PROPOSE_INPUTS: dict[str, dict[str, Any]] = {
    "propose_register_shortfall": {"obligation_ref": "ob-a1"},
    "propose_bridge_shortfall": {"obligation_ref": "ob-a1"},
    "propose_reconcile_bridge": {"obligation_ref": "ob-a1"},
    "propose_collect_fee": {"period": "2026-10"},
    "propose_collect_repayment": {},
    "propose_issue_card": {"cardholder_ref": "ops-lead", "program": "standard"},
    "propose_simulate_card_spend": {
        "scenario_ref": "s-1",
        "card_id": "card-1",
        "amount": "5.00",
        "currency": "USD",
        "merchant_category_code": "5734",
    },
}
READ_INPUTS: dict[str, dict[str, Any]] = {
    "read_tenant_summary": {},
    "read_cards": {},
    "read_card_transactions": {"card_id": "card-1"},
    "read_bridge_position": {},
}
