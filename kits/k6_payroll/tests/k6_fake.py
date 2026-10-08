"""Kit 6 fixtures on the real spine: FakeAirwallex plus awx_spine_aws.local_runner.

PayrollFake adds only what kit 6 needs and the fake does not model yet:

- employers: a connected account with a USD global account, so deposits can be simulated;
- `txns`: a test-side record of the deposits made through `deposit()` (the fake's own
  financial_transactions list serves them to the kit);
- test hooks: before_read / after_write callbacks, a provider that drops x-on-behalf-of on
  named ops (the guide's FX trap), a contractor whose payout fails at SENT -> PAID, and a
  crash after a write lands.

Everything else is FakeAirwallex as it is, with its faults (inject) and simulated clock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from awx_common import (
    ApprovalDecision,
    Decision,
    Money,
    Operation,
    OperationResult,
    Origin,
    Proposal,
    ReadRequest,
    TenantContext,
)
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import StaticTenantResolver, TenantRow
from awx_k6_payroll import SPECS
from awx_k6_payroll.model import employer_tenant_id
from awx_spine.fake import EPOCH, FakeAirwallex
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import RunRegistry, WorkflowOutcome, run_kit_workflow

PLATFORM = TenantContext("platform")
# Deployed roots (batch, lock, release) run as the payroll operator tenant: a
# tenants row with no connected account. Employer children run as k6:employer:<code>.
OPERATOR = TenantContext("k6:operator")


class PayrollFake(FakeAirwallex):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.txns: list[dict[str, Any]] = []
        self.global_accounts: dict[str, str] = {}
        self.before_read: dict[str, Callable[[PayrollFake, ReadRequest], None]] = {}
        self.after_write: dict[str, Callable[[PayrollFake, Operation], None]] = {}
        self.drop_obo: set[str] = set()
        self.fail_paid: set[str] = set()  # contractor codes whose SENT -> PAID becomes FAILED
        self.crash_after: set[str] = set()  # op names: apply, then raise (adapter crash)
        self.sent_ops: list[Operation] = []

    # ---- fixtures ------------------------------------------------------------------------
    def now(self) -> datetime:
        return EPOCH + timedelta(seconds=self.clock())

    def employer(self, code: str, usd: str | Decimal) -> str:
        acct = self.add_connected_account(f"poc-i6-{code}", {"USD": Money.of(usd, "USD")})
        op = Operation(
            kit="k6",
            name="global_accounts.create",
            method="POST",
            path="/api/v1/global_accounts/create",
            body={
                "request_id": f"poc-i6-ga-{acct}"[:50],
                "country_code": "US",
                "required_features": [{"currency": "USD", "transfer_method": "LOCAL"}],
            },
            request_id=f"poc-i6-ga-{acct}"[:50],
            business_key=f"ga:{acct}",
            intent_hash="0" * 64,
            on_behalf_of=acct,
        )
        res = super().execute(op)
        assert res.provider_id, res
        self.global_accounts[acct] = res.provider_id
        return acct

    def deposit(self, acct: str, amount: str) -> None:
        dep = self.simulate(
            "deposit",
            global_account_id=self.global_accounts[acct],
            amount=Decimal(amount),
            payer_name="poc-i6-payer",
            on_behalf_of=acct,
        )
        self.txns.append(
            {
                "id": str(dep["id"]),
                "account": acct,
                "transaction_type": "DEPOSIT",
                "currency": "USD",
                "amount": Decimal(amount),
                "created_at": self.now(),
            }
        )

    # ---- port ----------------------------------------------------------------------------
    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        hook = self.before_read.get(req.path)
        if hook is not None:
            hook(self, req)
        return super().read(req)

    def execute(self, op: Operation) -> OperationResult:
        if op.name in self.drop_obo:
            op = replace(op, on_behalf_of=None)
        if op.name == "transfers.simulate_transition" and op.body.get("next_status") == "PAID":
            tid = op.path.split("/")[-2]
            ref = next((t["reference"] for t in self.objects("transfers") if t["id"] == tid), "")
            if any(str(ref).endswith(f"-{c}") for c in self.fail_paid):
                op = replace(op, body={"next_status": "FAILED"})
        self.sent_ops.append(op)
        result = super().execute(op)
        hook = self.after_write.get(op.name)
        if hook is not None:
            hook(self, op)
        if op.name in self.crash_after:
            self.crash_after.discard(op.name)
            raise RuntimeError(f"adapter crashed after {op.name} left")
        return result


@dataclass
class Env:
    fake: PayrollFake
    accts: dict[str, str]
    ops: InMemoryOperationStore = field(default_factory=InMemoryOperationStore)
    kit_store: InMemoryKitStore = field(default_factory=InMemoryKitStore)
    platform_store: InMemoryKitStore = field(default_factory=InMemoryKitStore)
    audit: list[Mapping[str, Any]] = field(default_factory=list)
    runs: RunRegistry = field(default_factory=RunRegistry)  # run_status for every run

    extra_rows: dict[str, TenantRow] = field(default_factory=dict)  # e.g. another kit's rows

    @property
    def rows(self) -> dict[str, TenantRow]:
        """The tenants table as deployed: kit-bound rows."""
        return {
            OPERATOR.tenant_id: TenantRow("k6", None, "operator"),
            **{
                employer_tenant_id(c): TenantRow("k6", a, "customer") for c, a in self.accts.items()
            },
            **self.extra_rows,
        }

    @property
    def resolver(self) -> StaticTenantResolver:
        return StaticTenantResolver(self.rows)

    def money_ops(self, name: str | None = None, acct: str | None = None) -> list[Operation]:
        money = {"transfers.create", "fx.conversions.create", "charges.create"}
        return [
            op
            for op in self.fake.sent_ops
            if op.name in money
            and (name is None or op.name == name)
            and (acct is None or op.on_behalf_of == acct or op.body.get("source") == acct)
        ]


def make_env(**usd: str) -> Env:
    """make_env(a="20000", b="10"): one connected account per employer, funded in USD."""
    fake = PayrollFake()
    return Env(fake, {code: fake.employer(code, amount) for code, amount in usd.items()})


def approve_all(approvers: Sequence[str], env: Env) -> Callable[[Proposal], list[ApprovalDecision]]:
    def decide(p: Proposal) -> list[ApprovalDecision]:
        return [
            ApprovalDecision(p.proposal_id, p.intent_hash, a, Decision.APPROVE, env.fake.now())
            for a in approvers
        ]

    return decide


def run(
    spec: Any,
    raw: Mapping[str, Any],
    env: Env,
    *,
    origin: Origin = Origin.SCHEDULE,
    proposed_by: str = "scheduler",
    approvers: Sequence[str] = ("approver-1", "approver-2"),
    on_decide: Callable[[Proposal], None] | None = None,
    tenant: TenantContext = OPERATOR,
) -> WorkflowOutcome:
    """One proposal through local_runner, as the propose tool (AGENT) or the weekly scheduler
    (SCHEDULE) starts it. on_decide runs between the approval and the execution."""
    decide = approve_all(approvers, env)
    if tenant == OPERATOR:  # the deployed caller: the k6:operator row, resolved in kit k6
        tenant = env.resolver.resolve(OPERATOR.tenant_id, kit="k6")

    def decide_and_wait(p: Proposal) -> list[ApprovalDecision]:
        out = decide(p)
        if on_decide is not None:
            on_decide(p)
        return out

    return run_kit_workflow(
        spec,
        raw,
        proposed_by=proposed_by,
        tenant=tenant,
        port=env.fake,
        store=env.ops,
        decide=decide_and_wait,
        now=env.fake.now,
        sleep=env.fake.advance,
        audit=env.audit.append,
        kit_store=env.kit_store,
        platform_store=env.platform_store,
        tenants=env.resolver,
        origin=origin,
        runs=env.runs,
        actions=tuple(SPECS),
    )


def children(out: WorkflowOutcome) -> dict[str, WorkflowOutcome]:
    return {c.proposal.intent["employer"]: c for c in out.child_outcomes}


def lock_then_pay(spec_lock: Any, spec_pay: Any, raw: Mapping[str, Any], env: Env, **kw: Any):
    """The two-step flow: AUTO rate lock, then the approved payroll."""
    lock = run(spec_lock, raw, env, origin=kw.get("origin", Origin.SCHEDULE))
    return lock, run(spec_pay, raw, env, **kw)
