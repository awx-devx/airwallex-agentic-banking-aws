"""Kit 1 fixtures over the real spine: FakeAirwallex plus awx_spine_aws.local_runner.

Every scenario runs through run_kit_workflow, which is the same engine and adapter code the
deployed state machine uses, against awx_spine's FakeAirwallex (sandbox shapes, faults, lag,
7-day dedupe, simulated clock). CLOCK WIRING: sleep=fake.advance and now from fake.clock.

Kit-specific fixtures only:
- a seeded book whose beneficiaries really exist in the fake (poc-i1- nicknames);
- HookedPort, a thin wrapper that fires a test callback once around a named read or write, so a
  test can spend money elsewhere between two of the kit's own reads, or turn the sandbox's PAID
  transition into a FAILED one (a CANCELLED transfer).

The two interleavings the runner cannot express (a run that dies after a step and before its
postcondition, and a second delivery of one build) call the builds directly; their helpers are
in test_faults.py and test_release_races.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from awx_common import (
    ApprovalDecision,
    Decision,
    Money,
    Operation,
    OperationResult,
    OperationStatus,
    Proposal,
    ReadRequest,
    TenantContext,
    derive_request_id,
)
from awx_common.approvals import Origin
from awx_common.sandbox import make_transfer_advance
from awx_common.store import InMemoryKitStore
from awx_common.workflow import KitWorkflowSpec, PolicyContext
from awx_k1_treasury.book import DEMO_BOOK, TreasuryBook, load_book, seed_book
from awx_k1_treasury.workflow import build_specs
from awx_spine.fake import EPOCH, USD_PRICE, FakeAirwallex
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import RunRegistry, WorkflowOutcome, run_kit_workflow

TENANT = TenantContext("platform")  # kit 1 runs on the platform's own account
AFTER_DEPOSIT = {"USD": "42000", "EUR": "2000", "GBP": "1500"}
BEFORE_DEPOSIT = {"USD": "34000", "EUR": "2000", "GBP": "1500"}
SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
OFFICE = {"obligation_ids": ["ob-office-usd"]}
SHIPPING = {"obligation_ids": ["ob-shipping"]}

_BEN = {
    "US": {"country_code": "US", "state": "US-NY"},
    "DE": {"country_code": "DE"},
    "GB": {"country_code": "GB"},
}


class HookedPort:
    """FakeAirwallex with once-only test hooks around named reads and writes."""

    def __init__(self, fake: FakeAirwallex) -> None:
        self.fake = fake
        self._before_read: list[tuple[str, Callable[[], Any]]] = []
        self._after_exec: list[tuple[str, Callable[[Operation, OperationResult], Any]]] = []
        self._rewrite: list[tuple[str, Callable[[Operation], Operation]]] = []
        self.executed: list[Operation] = []

    def before_read(self, path_suffix: str, fn: Callable[[], Any]) -> None:
        self._before_read.append((path_suffix, fn))

    def after_execute(self, name: str, fn: Callable[[Operation, OperationResult], Any]) -> None:
        self._after_exec.append((name, fn))

    def rewrite(self, name: str, fn: Callable[[Operation], Operation]) -> None:
        self._rewrite.append((name, fn))

    def execute(self, op: Operation) -> OperationResult:
        for i, (name, fn) in enumerate(self._rewrite):
            if name == op.name:
                del self._rewrite[i]
                op = fn(op)
                break
        self.executed.append(op)
        result = self.fake.execute(op)
        for i, (name, fn) in enumerate(self._after_exec):
            if name == op.name:
                del self._after_exec[i]
                fn(op, result)
                break
        return result

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        for i, (suffix, fn) in enumerate(self._before_read):
            if req.path.endswith(suffix):
                del self._before_read[i]
                fn()
                break
        return self.fake.read(req)

    def names(self) -> list[str]:
        return [o.name for o in self.executed]


def _ben_op(fake: FakeAirwallex, name: str, country: str, method: str) -> str:
    key = f"poc-i1-ben-{name}"
    rid = derive_request_id("k1", key, "create")
    op = Operation(
        kit="k1",
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body={
            "request_id": rid,
            "nickname": key,
            "transfer_methods": [method],
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "address": dict(_BEN[country]),
                "bank_details": {
                    "bank_account_category": "Checking",
                    "account_routing_type1": "aba",
                }
                if country == "US"
                else {},
            },
        },
        request_id=rid,
        business_key=key,
        intent_hash="b" * 64,
    )
    res = fake.execute(op)
    assert res.status is OperationStatus.SUCCEEDED, res
    return str(res.provider_id)


def demo_book_for(fake: FakeAirwallex) -> TreasuryBook:
    """DEMO_BOOK with beneficiaries that exist in this fake (poc-i1- labels)."""
    where = {
        "ob-shipping": ("US", "LOCAL"),
        "ob-supplier-eur": ("DE", "SWIFT"),
        "ob-saas-gbp": ("GB", "LOCAL"),
        "ob-office-usd": ("US", "LOCAL"),
        "ob-contractor-gbp": ("GB", "LOCAL"),
    }
    obs = []
    for o in DEMO_BOOK.obligations:
        country, method = where[o.obligation_id]
        obs.append(replace(o, beneficiary_id=_ben_op(fake, o.obligation_id, country, method)))
    return replace(DEMO_BOOK, obligations=tuple(obs))


def fresh_store(book: TreasuryBook) -> InMemoryKitStore:
    store = InMemoryKitStore()
    seed_book(store, book)  # mints a fresh random book id (M2)
    return store


@dataclass
class Env:
    fake: FakeAirwallex
    port: HookedPort
    store: InMemoryKitStore
    ops: InMemoryOperationStore
    specs: dict[str, KitWorkflowSpec]
    book: TreasuryBook
    outcomes: list[WorkflowOutcome] = field(default_factory=list)
    prices: dict[str, Decimal] = field(default_factory=dict)
    runs: RunRegistry = field(default_factory=RunRegistry)  # run_status answers, shared by runs

    # --- clock ----------------------------------------------------------------------------
    def now(self) -> datetime:
        return EPOCH + timedelta(seconds=self.fake.clock())

    def advance(self, seconds: float) -> None:
        self.fake.advance(seconds)

    # --- runs -----------------------------------------------------------------------------
    def run(
        self,
        action: str,
        raw: Mapping[str, Any],
        *,
        approvers: Sequence[str] = (),
        origin: Origin = Origin.AGENT,
        during_approval: Callable[[Proposal], Any] | None = None,
    ) -> WorkflowOutcome:
        """`during_approval` runs while the proposal waits for its approvers (only for a
        SINGLE or DUAL proposal), e.g. to move the market or the clock."""

        def decide(proposal: Proposal) -> list[ApprovalDecision]:
            if during_approval is not None:
                during_approval(proposal)
            return [
                ApprovalDecision(
                    proposal.proposal_id, proposal.intent_hash, a, Decision.APPROVE, self.now()
                )
                for a in approvers
            ]

        out = run_kit_workflow(
            self.specs[action],
            raw,
            proposed_by="requester",
            tenant=TENANT,
            port=self.port,
            store=self.ops,
            decide=decide,
            now=self.now,
            sleep=self.fake.advance,
            kit_store=self.store,
            platform_store=self.store,
            origin=origin,
            runs=self.runs,
            actions=tuple(self.specs),
        )
        self.outcomes.append(out)
        return out

    def lock(self, raw: Mapping[str, Any]) -> WorkflowOutcome:
        ids = {"obligation_ids": raw["obligation_ids"]}
        out = self.run("lock_rate", ids)
        assert out.state == "SUCCEEDED", (out.state, out.detail, out.postcondition)
        return out

    def execute(self, raw: Mapping[str, Any], **kw: Any) -> WorkflowOutcome:
        return self.run("execute_plan", raw, **kw)

    def pay(self, raw: Mapping[str, Any], **kw: Any) -> WorkflowOutcome:
        """Lock the rate if the plan converts, then execute."""
        if any(
            self.book.obligation(i).amount.currency != self.book.funding_currency
            for i in raw["obligation_ids"]
        ):
            lock = self.run("lock_rate", {"obligation_ids": raw["obligation_ids"]})
            assert lock.state in ("SUCCEEDED", "BLOCKED"), (lock.state, lock.detail)
        return self.execute(raw, **kw)

    def release(self, oid: str, reason: str, **kw: Any) -> WorkflowOutcome:
        return self.run("release_obligation", {"obligation_id": oid, "reason": reason}, **kw)

    # --- reads ----------------------------------------------------------------------------
    def ctx(self) -> PolicyContext:
        spec = self.specs["execute_plan"]

        def guarded(req: ReadRequest) -> Mapping[str, Any]:
            assert spec.allows_read(req.path), f"read outside the allowlist: {req.path}"
            return self.port.read(req)

        return PolicyContext(TENANT, self.now(), guarded, store=self.store)

    def money(self, currency: str) -> Money:
        return self.fake.balance(currency)

    def transfers(self) -> list[dict[str, Any]]:
        return self.fake.objects("transfers")

    def conversions(self) -> list[dict[str, Any]]:
        return self.fake.objects("conversions")

    def spend_elsewhere(self, currency: str, amount: str) -> None:
        """Money leaving a wallet outside kit 1 (another team's payment)."""
        ben = _ben_op(self.fake, f"elsewhere-{len(self.transfers())}", "US", "LOCAL")
        rid = derive_request_id("k1", f"poc-i1-elsewhere-{len(self.transfers())}", "pay")
        res = self.fake.execute(
            Operation(
                kit="k1",
                name="transfers.create",
                method="POST",
                path="/api/v1/transfers/create",
                body={
                    "request_id": rid,
                    "beneficiary_id": ben,
                    "transfer_currency": currency,
                    "transfer_amount": Decimal(amount),
                    "source_currency": currency,
                    "transfer_method": "LOCAL",
                    "reference": "poc-i1-elsewhere",
                    "reason": "goods_purchased",
                },
                request_id=rid,
                business_key=f"poc-i1-elsewhere-{rid}",
                intent_hash="b" * 64,
            )
        )
        assert res.status is OperationStatus.SUCCEEDED, res

    def move_rate(self, currency: str, factor: str) -> None:
        """Move the fake's market price through FakeAirwallex.set_price."""
        current = self.prices.get(currency, USD_PRICE[currency])
        self.prices[currency] = current * Decimal(factor)
        self.fake.set_price(currency, self.prices[currency])

    def deposit(self, currency: str, amount: str) -> None:
        """Cash arriving outside any API call: FakeAirwallex.credit."""
        self.fake.credit(currency, Decimal(amount))

    def cancel_next_transfer(self) -> None:
        """The next PAID simulation becomes FAILED: the transfer reads CANCELLED with its
        failure block and the money is refunded (FAKE-FIDELITY Transfers)."""

        def to_failed(op: Operation) -> Operation:
            if op.body.get("next_status") != "PAID":
                return op
            return replace(
                op, body={"next_status": "FAILED", "failure_type": "BENEFICIARY_BANK_RETURNED"}
            )

        for _ in range(3):  # the rewrite only fires on a PAID transition
            self.port.rewrite("transfers.simulate_transition", to_failed)


def make_env_fn(
    balances: Mapping[str, str] = AFTER_DEPOSIT,
    *,
    fake: FakeAirwallex | None = None,
    store: InMemoryKitStore | None = None,
    advance: bool = True,
    **fake_kw: Any,
) -> Env:
    fake = fake or FakeAirwallex(
        balances={c: Money.of(v, c) for c, v in balances.items()}, **fake_kw
    )
    book = demo_book_for(fake) if store is None else load_book(store)
    assert book is not None
    store = store or fresh_store(book)
    specs = build_specs(transfer_advance=make_transfer_advance("k1") if advance else None)
    return Env(fake, HookedPort(fake), store, InMemoryOperationStore(), specs, book)


@pytest.fixture
def make_env():
    return make_env_fn


@pytest.fixture
def env():
    return make_env_fn()


CONTRADICTION = {
    "receipt_id": "rcpt-acme",
    "source": "email from Acme AP, 18 Oct 2026",
    "quote": "we will not be able to pay invoice INV-2207 until next month",
}


@pytest.fixture
def contradiction() -> dict[str, str]:
    return dict(CONTRADICTION)


def clone_store(store: InMemoryKitStore) -> InMemoryKitStore:
    """The same kit state (same book id) in a second store: another Lambda's view."""
    other = InMemoryKitStore()
    other._items = dict(store._items)  # test-only copy of the in-memory rows
    return other


# --- live-run evidence ------------------------------------------------------------------------


def explain(what: Any) -> str:
    """The whole failure detail as one JSON string. pytest prints a string assertion message
    in full, while a dict or tuple message is shortened to `...` unless you run with `-l`."""
    import json

    from awx_common import to_jsonable

    try:
        value = to_jsonable(what)
    except (TypeError, ValueError):  # never let the explanation hide the failure it explains
        value = what
    return json.dumps(value, indent=2, sort_keys=True, default=str)


@contextmanager
def evidence_file(seen: dict[str, Any], path: str | None) -> Iterator[None]:
    """Write `seen` to `path` when the block ends, whether it passed or failed. A failed run
    records `outcome: failed` and the error, so the evidence exists when it is most needed."""
    try:
        yield
    except BaseException as exc:
        seen["outcome"] = "failed"
        seen["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    else:
        seen["outcome"] = "passed"
    finally:
        if path:
            with open(path, "w") as fh:
                fh.write(explain(seen) + "\n")


@pytest.fixture(name="explain")
def explain_fixture() -> Callable[[Any], str]:
    return explain


@pytest.fixture(name="evidence_file")
def evidence_file_fixture() -> Callable[[dict[str, Any], str | None], Any]:
    return evidence_file
