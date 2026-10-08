"""Clock wiring: local_runner takes an injected sleep and now, and lends them on.

A kit test wires sleep=fake.advance and a now from fake.clock, and a lagged read-back is seen
once the fake clock passes the lag. Without that wiring the run stops OUTCOME_UNKNOWN. The
adapter Lambda passes its own clock to the executor, and a store on its package's default clock
takes each run's clock for that run only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from awx_common import Money, Origin, RiskTier, derive_request_id
from awx_common.ops import Operation, OperationStatus
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.workflow import KitWorkflowSpec, PolicyDecision, StepSpec
from awx_spine.executor import (
    MutationExecutor,
)
from awx_spine.fake import FakeAirwallex, Fault
from awx_spine.store import InMemoryOperationStore
from awx_spine.store import _utcnow as spine_default_clock
from awx_spine_aws.handlers import adapter
from awx_spine_aws.local_runner import RunRegistry, run_kit_workflow

import toy

T0 = datetime(2026, 10, 6, tzinfo=UTC)


USD = "USD"


def _beneficiary(fake: FakeAirwallex) -> str:
    rid = derive_request_id("k3", "poc-s2-ben", "create")
    op = Operation(
        kit="k3",
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body={
            "request_id": rid,
            "nickname": "poc-s2-ben",
            "transfer_methods": ["LOCAL"],
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "address": {"country_code": "US", "state": "US-NY"},
                "bank_details": {
                    "bank_account_category": "Checking",
                    "account_routing_type1": "aba",
                },
            },
        },
        request_id=rid,
        business_key="poc-s2-ben",
        intent_hash="b" * 64,
    )
    result = fake.execute(op)
    assert result.status is OperationStatus.SUCCEEDED, result
    return result.provider_id


def _spec(ben: str) -> KitWorkflowSpec:
    def build(ctx):
        key = "poc-s2-invoice:1"
        rid = derive_request_id("k3", key, "pay")
        return (
            Operation(
                kit="k3",
                name="transfers.create",
                method="POST",
                path="/api/v1/transfers/create",
                body={
                    "request_id": rid,
                    "beneficiary_id": ben,
                    "transfer_amount": ctx.decision.computed["amount"].amount,
                    "transfer_currency": USD,
                    "source_currency": USD,
                    "transfer_method": "LOCAL",
                    "reference": "poc-s2-clock",
                    "reason": "goods_purchased",
                },
                request_id=rid,
                business_key=key,
                intent_hash=ctx.intent_hash,
            ),
        )

    return KitWorkflowSpec(
        kit="k3",
        action="pay_invoice",
        validate=lambda raw: {"invoice": raw["invoice"]},
        policy=lambda intent, ctx: PolicyDecision(
            RiskTier.AUTO, ("clock",), {"amount": Money.of(Decimal("10.00"), USD)}
        ),
        steps=(StepSpec("pay", build),),
        approval_ttl=timedelta(hours=1),
        read_allowlist=(r"^/api/v1/transfers$",),
    )


def _run(*, wired: bool):
    fake = FakeAirwallex(balances={USD: Money.of("100.00", USD)})
    ben = _beneficiary(fake)
    fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=20)
    outcome = run_kit_workflow(
        _spec(ben),
        {"invoice": "1"},
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=fake,
        store=InMemoryOperationStore(),
        decide=lambda p: [],
        now=(lambda: T0 + timedelta(seconds=fake.clock())) if wired else (lambda: T0),
        sleep=fake.advance if wired else (lambda s: None),
        origin=Origin.SCHEDULE,
    )
    return outcome, fake


def test_a_lagged_read_back_is_seen_once_the_fake_clock_passes_it():
    outcome, fake = _run(wired=True)
    assert outcome.state == "SUCCEEDED"
    assert outcome.results["pay"][-1].status is OperationStatus.SUCCEEDED
    assert len(fake.objects("transfers")) == 1  # one transfer, never a second
    assert fake.clock() >= 20


def test_without_clock_wiring_the_lag_never_passes_and_the_run_stops_unknown():
    outcome, fake = _run(wired=False)
    assert outcome.state == "FAILED"
    assert outcome.results["pay"][-1].status is OperationStatus.OUTCOME_UNKNOWN
    assert len(fake.objects("transfers")) == 1


# --- the clock the executor and the store use ----------------------------------------------------


def test_the_adapter_passes_its_clock(monkeypatch):
    built: dict = {}

    class Recorder(MutationExecutor):
        def __init__(self, *a, **kw):
            built.update(kw)
            super().__init__(*a, **kw)

    monkeypatch.setattr("awx_spine.executor.MutationExecutor", Recorder)
    monkeypatch.setenv("AWX_OPERATIONS_TABLE", "ops")
    monkeypatch.setenv("AWX_ARTIFACTS_BUCKET", "b")
    monkeypatch.setenv("AWX_SECRET_ARN", "arn:secret")
    monkeypatch.setattr(adapter, "boto_client", lambda name: object())
    monkeypatch.setattr(adapter, "_port", lambda: object())
    adapter._executor("k3", "platform")
    assert built["clock"] is adapter._now


def _local(spec, clock, runs, **kw):
    return run_kit_workflow(
        spec,
        kw.pop("raw", toy.RAW),
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=toy.ToyPort(),
        store=kw.pop("store", InMemoryOperationStore()),
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.SCHEDULE,
        runs=runs,
        **kw,
    )


def test_a_reused_store_takes_each_runs_clock_and_gets_its_own_back():
    store = InMemoryOperationStore()
    first, second = toy.Clock(), toy.Clock(start=toy.T0 + timedelta(days=3))
    _local(toy.toy_spec(), first, RunRegistry(), store=store)
    assert store._now is spine_default_clock  # restored after the run
    _local(toy.toy_spec(), second, RunRegistry(), store=store, raw={**toy.RAW, "payee": "ben_2"})
    assert store._now is spine_default_clock
    pay2 = [
        r
        for r in store.all()
        if r.attempt_started_at and r.attempt_started_at > toy.T0 + timedelta(days=2)
    ]
    assert pay2  # the second run's attempts carry the second run's clock
