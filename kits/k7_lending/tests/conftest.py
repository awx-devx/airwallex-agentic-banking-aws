"""Kit 7 test fixtures over the real runner.

Every scenario runs through awx_spine_aws.local_runner.run_kit_workflow against
awx_spine.FakeAirwallex, with one clock wired through runner, executor and stores
(sleep=fake.advance, now from fake.clock) and one shared awx_spine InMemoryOperationStore per
test, so a later run re-emitting an Operation meets the record the first run left, as the
adapter Lambda does.

Kit-specific fixtures only (K7Fake), on FakeAirwallex's public controls:
- GET /api/v1/balances/history is the fake's own model. `credits_as_deposits` relabels
  our transfer credits as plain DEPOSIT rows, naming the transfer (`name_credits`) or nothing:
  the pessimistic live shape the revenue filter and backstop must survive.
- `fail_next` / `suspend_next`: Fault.END_AS FAILED or SUSPENDED (FAKE-FIDELITY row 109; the
  sandbox cannot provoke them). `refuse_next`: Fault.REFUSE, a definitive 400 with nothing
  written. `spend`: `credit` with a negative amount. `refuse_after_create_next`: the write
  lands, then the reply is a refusal.
- `lose_next`: the call never reaches Airwallex and the reply is lost (OUTCOME_UNKNOWN, nothing
  created); `land(op)` later delivers it, as a request that was in flight would.
- `crash_after_next`: the next write that lands is followed by an adapter crash before it
  records a result (a 429 before it does not consume the crash).
- Kit-side uncertain-send shape fixtures: `f13_refused_next` answers a send OUTCOME_UNKNOWN
  with the refusal in body["refused"], and `f13_multiple_next` answers OUTCOME_UNKNOWN
  multiple_objects. They test kit 7's reading of those result shapes in isolation. The
  real executor's behaviour is covered by
  test_refused_resend_comes_back_unknown_from_the_real_executor and
  test_two_objects_under_one_request_id_stay_in_flight.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from awx_common import (
    ApprovalDecision,
    Decision,
    Money,
    Operation,
    OperationResult,
    OperationStatus,
    Origin,
    Proposal,
    ReadRequest,
    TenantContext,
    derive_request_id,
)
from awx_common.errors import OutcomeUnknown, TenantScopeViolation
from awx_common.store import InMemoryKitStore
from awx_common.workflow import KitWorkflowSpec, PolicyContext, PostconditionResult
from awx_k7_lending.book import LoanTerms, PortfolioConfig, open_portfolio, update_book
from awx_k7_lending.repayment import RepaymentTerms
from awx_spine.fake import EPOCH, PLATFORM, FakeAirwallex, Fault
from awx_spine.normalise import result_from_error, result_from_response
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import run_kit_workflow

USD = "USD"
PLATFORM_TENANT = TenantContext("platform", None)
HISTORY = "/api/v1/balances/history"


def usd(x: str | int) -> Money:
    return Money.of(x, USD)


class K7Fake(FakeAirwallex):
    """FakeAirwallex plus the kit-7 fixtures listed in the module docstring.

    Everything goes through the fake's public controls: `credit`, `inject` with
    Fault.REFUSE and Fault.END_AS, `find`, `objects` and GET /balances/history. Two edits of
    private state remain, for shapes the fake has no API for: `foreign` (another run's settled
    object already on the shared account) and `duplicate` (a second object under one
    request_id, for multiple_objects).
    """

    def __init__(self, platform_cash: Money, *, dedupe_window_s: float = 7 * 86400) -> None:
        super().__init__(balances={USD: platform_cash}, dedupe_window_s=dedupe_window_s)
        # Pessimistic history shape: show our own transfer credits as
        # plain DEPOSIT rows, naming the transfer (name_credits) or naming nothing.
        self.credits_as_deposits = False
        self.name_credits = True
        self.executed: list[Operation] = []
        self._once: dict[str, list[str]] = {}  # op name -> queued one-shot behaviours
        self._gas: dict[str, str] = {}
        self._foreign: set[str] = set()

    # clock

    def now(self) -> datetime:
        return EPOCH + timedelta(seconds=self.clock())

    def move_to(self, when: datetime) -> None:
        delta = (when - self.now()).total_seconds()
        if delta < 0:
            raise AssertionError(f"test time goes backwards to {when}")
        self.advance(delta)

    # fixtures

    def borrower(self, name: str) -> str:
        return self.add_connected_account(f"poc-i7-{name}", {})

    def deposit(self, acct: str | None, amount: Money) -> str:
        """A simulated deposit through a Global Account, the guide's way: borrower revenue, or
        (acct None) money into the platform wallet. Returns the deposit id."""
        ga = self._gas.get(acct)
        if ga is None:
            rid = derive_request_id("k7", f"poc-i7-ga:{acct}", "create")
            op = Operation(
                kit="k7",
                name="global_accounts.create",
                method="POST",
                path="/api/v1/global_accounts/create",
                body={
                    "request_id": rid,
                    "country_code": "US",
                    "required_features": [{"currency": USD, "transfer_method": "LOCAL"}],
                },
                request_id=rid,
                business_key=f"poc-i7-ga:{acct}",
                intent_hash="b" * 64,
                on_behalf_of=acct,
            )
            ga = super().execute(op).provider_id
            assert ga
            self._gas[acct] = ga  # type: ignore[index]
        reply = self.simulate(
            "deposit", global_account_id=ga, amount=amount.amount, on_behalf_of=acct
        )
        return reply["id"]

    def spend(self, acct: str, amount: Money) -> None:
        """Money leaving the borrower's wallet outside the kit (a supplier payment)."""
        self.credit(USD, -amount.amount, account=acct)

    def fail_next(self, op_name: str) -> None:
        """The next movement is created, then ends FAILED (money back to the payer)."""
        self.inject(op_name, Fault.END_AS, final_status="FAILED")

    def suspend_next(self, op_name: str) -> None:
        self.inject(op_name, Fault.END_AS, final_status="SUSPENDED")

    def refuse_next(self, op_name: str, times: int = 1) -> None:
        """A definitive 400 at create, nothing written."""
        self.inject(op_name, Fault.REFUSE, times=times)

    def _queue(self, op_name: str, what: str) -> None:
        self._once.setdefault(op_name, []).append(what)

    def lose_next(self, op_name: str) -> None:
        self._queue(op_name, "lose")

    def refuse_after_create_next(self, op_name: str) -> None:
        self._queue(op_name, "refuse_after_create")

    def crash_after_next(self, op_name: str) -> None:
        self._queue(op_name, "crash")

    def f13_refused_next(self, op_name: str) -> None:
        self._queue(op_name, "f13_refused")

    def f13_multiple_next(self, op_name: str) -> None:
        self._queue(op_name, "f13_multiple")

    def land(self, op: Operation) -> OperationResult:
        """Deliver a request that was in flight (the original of a lost send)."""
        return super().execute(op)

    @staticmethod
    def _kind(name: str) -> str:
        return "charges" if name == "charges.create" else "connected_account_transfers"

    def creates(self, name: str) -> int:
        return sum(1 for o in self.objects(self._kind(name)) if o["id"] not in self._foreign)

    def obj(self, kind: str, request_id: str) -> dict[str, Any]:
        found = self.find(kind, request_id)
        assert found is not None, request_id
        return found

    def foreign(self, kind: str, acct: str, amount: Money) -> None:
        """A SETTLED poc-k7- object from another run on the shared account (private edit: the
        fake has no API to seed one)."""
        rid = f"foreign-{len(self._foreign)}-run"
        oid = f"x_{rid}"
        self._foreign.add(oid)
        self._state.kind(kind)[oid] = {
            "id": oid,
            "request_id": rid,
            "amount": amount.amount,
            "currency": USD,
            ("source" if kind == "charges" else "destination"): acct,
            "fee": 0,
            "reason": "loan_credit_repayment",
            "reference": "poc-k7-collect-old-run",
            "status": "SETTLED",
            "_t": -1.0,
            "_owner": PLATFORM,
        }

    def duplicate(self, kind: str, request_id: str) -> None:
        """A second object under the same request_id (private edit: the sandbox has never shown
        one, and the fake has no API for it)."""
        (orig,) = [o for o in self._state.kind(kind).values() if o["request_id"] == request_id]
        self._state.kind(kind)["tr_second_object"] = {**orig, "id": "tr_second_object"}

    def mark_settled(self, request_id: str) -> None:
        """Turn a movement SETTLED after the fact (private edit: set_movement_status covers
        SUSPENDED, PENDING and FAILED only)."""
        for kind in ("charges", "connected_account_transfers"):
            for o in self._state.kind(kind).values():
                if o["request_id"] == request_id:
                    o["status"] = "SETTLED"
                    return
        raise KeyError(request_id)

    # port

    def execute(self, op: Operation) -> OperationResult:
        self.executed.append(op)
        once = self._once.get(op.name) or []
        what = once.pop(0) if once else None
        if what == "lose":
            return result_from_error(op, OutcomeUnknown(f"{op.name}: reply lost (fixture)"))
        if what == "f13_refused":
            return OperationResult(
                op.request_id,
                OperationStatus.OUTCOME_UNKNOWN,
                400,
                body={"refused": {"code": "validation_failed", "message": "refused"}},
                error_type="OutcomeUnknown",
                error_code="validation_failed",
                error_message="resend refused while the first send may have landed",
            )
        if what == "f13_multiple":
            return OperationResult(
                op.request_id,
                OperationStatus.OUTCOME_UNKNOWN,
                None,
                error_type="OutcomeUnknown",
                error_code="multiple_objects",
                error_message="two objects carry this request_id",
            )
        result = super().execute(op)
        if what == "refuse_after_create":
            return result_from_response(
                op, 400, {"code": "validation_failed", "message": "refused (fixture)"}
            )
        if what == "crash":
            if result.status is not OperationStatus.SUCCEEDED:  # crash only after a landing
                self._once.setdefault(op.name, []).insert(0, "crash")
                return result
            raise RuntimeError("adapter crashed after the call left (fixture)")
        return result

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        body = super().read(req)
        if req.path != HISTORY or not self.credits_as_deposits:
            return body
        rows = []
        for row in body.get("items", []):
            row = dict(row)
            if row.get("transaction_type") == "TRANSFER_IN":
                row["transaction_type"] = "DEPOSIT"
                if not self.name_credits:
                    row.pop("source_id", None)
            rows.append(row)
        return {**body, "items": rows}


@dataclass
class Outcome:
    """What a test needs from one run: the proposal, what was sent, and how it ended."""

    proposal: Proposal
    state: str
    detail: str | None
    results: Mapping[str, Sequence[OperationResult]]
    post: PostconditionResult | None
    ops: list[Operation] = field(default_factory=list)
    abandoned: str | None = None

    @property
    def decision(self) -> SimpleNamespace:
        p = self.proposal
        return SimpleNamespace(risk_tier=p.risk_tier, reasons=p.reasons, computed=p.computed)


def approve(proposal: Proposal, *, approvers: Sequence[str] = ("approver-1", "approver-2")):
    return [
        ApprovalDecision(
            proposal.proposal_id, proposal.intent_hash, a, Decision.APPROVE, proposal.created_at
        )
        for a in approvers
    ]


def reject(proposal: Proposal):
    return [
        ApprovalDecision(
            proposal.proposal_id,
            proposal.intent_hash,
            "approver-1",
            Decision.REJECT,
            proposal.created_at,
        )
    ]


class Portfolio:
    """One test's world: the fake, the shared operation store and the platform book store."""

    def __init__(
        self, fake: K7Fake, book_store: InMemoryKitStore, borrowers: Mapping[str, TenantContext]
    ) -> None:
        self.fake = fake
        self.store = book_store
        self.ops = InMemoryOperationStore(now=fake.now)
        self.borrowers = dict(borrowers)

    def resolve(self, tenant_id: str) -> TenantContext:
        if tenant_id == PLATFORM_TENANT.tenant_id:
            return PLATFORM_TENANT
        for t in self.borrowers.values():
            if t.tenant_id == tenant_id:
                return t
        raise TenantScopeViolation("unknown tenant")

    def run(
        self,
        spec: KitWorkflowSpec,
        raw: Mapping[str, Any],
        *,
        tenant: TenantContext,
        now: datetime | None = None,
        origin: Origin = Origin.SCHEDULE,
        decide: Callable[[Proposal], Sequence[ApprovalDecision]] = approve,
        before_execution: Callable[[], None] | None = None,
        proposed_by: str = "requester",
    ) -> Outcome:
        if now is not None:
            self.fake.move_to(now)
        sent_before = len(self.fake.executed)

        def deciding(p: Proposal):
            decisions = decide(p)
            if before_execution is not None:
                before_execution()
            return decisions

        wo = run_kit_workflow(
            spec,
            raw,
            proposed_by=proposed_by,
            tenant=tenant,
            port=self.fake,
            store=self.ops,
            decide=deciding,
            now=self.fake.now,
            sleep=self.fake.advance,
            kit_store=InMemoryKitStore(),
            platform_store=self.store,
            tenants=self,
            origin=origin,
        )
        ops = [
            o
            for o in self.fake.executed[sent_before:]
            if o.name
            in ("charges.create", "connected_account_transfers.create", "platform_reports.create")
        ]
        abandoned = wo.state if wo.state in ("REJECTED", "EXPIRED", "BLOCKED") else None
        if wo.detail and "changed since the proposal" in wo.detail:
            abandoned = "STALE"
        return Outcome(
            wo.proposal, wo.state, wo.detail, wo.results, wo.postcondition, ops, abandoned
        )

    def ctx(self, spec: KitWorkflowSpec, tenant: TenantContext) -> PolicyContext:
        """A read-only context for calling a policy or a read tool directly."""
        fake = self.fake

        def read(req: ReadRequest) -> Mapping[str, Any]:
            assert spec.allows_read(req.path), req.path
            return fake.read(ReadRequest(req.path, req.query, tenant.connected_account_id))

        def read_platform(req: ReadRequest) -> Mapping[str, Any]:
            assert spec.allows_platform_read(req.path), req.path
            return fake.read(req)

        return PolicyContext(
            tenant=tenant,
            now=fake.now(),
            read=read,
            store=InMemoryKitStore(),
            platform_store=self.store,
            read_platform=read_platform if spec.platform_read_allowlist else None,
        )


# Time and the portfolio. The fake clock starts at 2026-10-06 00:00 UTC (Tuesday, ISO W41).

T0 = EPOCH
W41, W42, W43 = "2026-W41", "2026-W42", "2026-W43"
IN_W42 = datetime.fromisoformat("2026-10-14T09:00:00+00:00")  # Wednesday of W42
AFTER_W42 = datetime.fromisoformat("2026-10-19T09:00:00+00:00")
AFTER_W43 = datetime.fromisoformat("2026-10-26T09:00:00+00:00")
SHARED_WALLET = "50000.00"


def config(**overrides: Any) -> PortfolioConfig:
    fields = {
        "currency": USD,
        "reserve_floor": usd("5000.00"),
        "min_disbursement": usd("1000.00"),
        "disbursement_increment": usd("100.00"),
        "dual_approval_above": usd("25000.00"),
    }
    fields.update(overrides)
    return PortfolioConfig(**fields)


TERMS = {
    "loan-a": ("8000.00", "0.10", "1500.00", "200.00"),
    "loan-b": ("5000.00", "0.15", "1200.00", "150.00"),
    "loan-c": ("10000.00", "0.12", "2000.00", "250.00"),
}


def borrower_accounts(fake: K7Fake, names: Sequence[str]) -> dict[str, str]:
    """Each borrower's connected account, found by its poc-i7- label and created only when
    missing: connected accounts cannot be deleted, so runs reuse them."""
    listing = fake.read(ReadRequest("/api/v1/accounts"))
    found = {a.get("nickname"): a["id"] for a in listing.get("items", [])}
    return {name: found.get(f"poc-i7-{name}") or fake.borrower(name) for name in names}


def make_portfolio(fake: K7Fake | None = None, capital: str = "20000.00", **cfg: Any) -> Portfolio:
    """Operator setup: borrowers' connected accounts, the book, three loans, capital."""
    fake = fake or K7Fake(usd(SHARED_WALLET))
    borrowers = {}
    accounts = borrower_accounts(fake, TERMS)
    for lid in TERMS:
        borrowers[lid] = TenantContext(f"tenant-{lid[-1]}", accounts[lid])
    store = InMemoryKitStore()
    open_portfolio(store, config(**cfg))

    def setup(book) -> None:
        for lid, (commitment, share, cap, floor) in TERMS.items():
            book.add_loan(
                LoanTerms(
                    loan_id=lid,
                    borrower_tenant_id=borrowers[lid].tenant_id,
                    borrower_account=borrowers[lid].connected_account_id or "",
                    commitment=usd(commitment),
                    fee_rate=Decimal("0.08"),
                    repayment=RepaymentTerms(Decimal(share), usd(cap), usd(floor)),
                    first_week=W41,
                )
            )
        book.fund("capital-1", usd(capital))

    update_book(store, setup)
    return Portfolio(fake, store, borrowers)


@pytest.fixture
def world() -> Portfolio:
    return make_portfolio()


@pytest.fixture
def k7() -> SimpleNamespace:
    """Helpers (conftest is not importable under --import-mode=importlib)."""
    return SimpleNamespace(
        usd=usd,
        approve=approve,
        reject=reject,
        make_portfolio=make_portfolio,
        K7Fake=K7Fake,
        borrower_accounts=borrower_accounts,
        PLATFORM=PLATFORM_TENANT,
        T0=T0,
        W41=W41,
        W42=W42,
        W43=W43,
        IN_W42=IN_W42,
        AFTER_W42=AFTER_W42,
        AFTER_W43=AFTER_W43,
        SHARED_WALLET=SHARED_WALLET,
    )
