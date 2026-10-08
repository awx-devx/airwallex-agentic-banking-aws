"""The executor's attempt lease and deduplication margin, in the run's own time.

local_runner passes its clock to MutationExecutor, so the deduplication margin and the attempt
lease run in fake time. A resend after a crash, or after in_progress_elsewhere, waits out the
attempt lease, so a landed original is read back and recorded once and a lost one is sent
once. A retryable failure keeps the short resend interval.
"""

from __future__ import annotations

import sys
import types
from collections import Counter
from datetime import timedelta

import pytest
from awx_common import Origin
from awx_common.ops import OperationResult, OperationStatus
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import encode
from awx_spine.executor import (
    DEDUP_RESEND_MARGIN,
    IN_PROGRESS_CODE,
    PAST_MARGIN_CODE,
    MutationExecutor,
)
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import adapter_core, engine
from awx_spine_aws.handlers import adapter, kit_step
from awx_spine_aws.local_runner import run_kit_workflow

import toy
from aws_harness import MODULE, sfn_json
from test_asl_equivalence import Interpreter

FEE = toy.fee_op().request_id


def _local(spec, port, store, clock, **kw):
    return run_kit_workflow(
        spec,
        toy.RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=port,
        store=store,
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.SCHEDULE,
        **kw,
    )


def _fee_sends(port) -> list:
    return [s for s in port.sends if s.request_id == FEE]


def _recording_dispatch(monkeypatch) -> list:
    real, seen = adapter_core.dispatch, []

    def dispatch(executor, store, op, action, result=None):
        out = real(executor, store, op, action, result)
        if op.request_id == FEE:
            seen.append(out.error_code)
        return out

    monkeypatch.setattr(adapter_core, "dispatch", dispatch)
    return seen


# --- the margin and the lease in fake time -------------------------------------------------------


def test_a_fake_time_run_past_the_dedupe_margin_does_not_resend():
    clock = toy.Clock()
    store = InMemoryOperationStore(now=clock.now)
    op = toy.fee_op()
    store.begin(op)
    store.record_attempt(op.request_id)  # first attempt at fake T0; it never landed
    store.complete(
        op.request_id,
        OperationResult(
            request_id=op.request_id,
            status=OperationStatus.OUTCOME_UNKNOWN,
            error_type="OutcomeUnknown",
            error_message="timeout after submit",
        ),
    )
    clock.t += DEDUP_RESEND_MARGIN + timedelta(days=1)
    port = toy.ToyPort()
    out = _local(toy.toy_spec(), port, store, clock)
    assert _fee_sends(port) == []  # past the dedup margin: never resent
    [fee] = out.results["fee"]
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN
    assert fee.error_code == PAST_MARGIN_CODE


def test_a_crash_inside_the_lease_is_in_progress_until_the_lease_ends(monkeypatch):
    clock = toy.Clock()
    store = InMemoryOperationStore(now=clock.now)
    port = toy.ToyPort()
    op = toy.fee_op()
    store.begin(op)
    store.record_attempt(op.request_id)  # a send left at fake T0 and its Lambda was killed
    port._create(op, key=op.request_id)  # it landed
    clock.t += timedelta(seconds=60)  # still inside the 180 s lease
    seen = _recording_dispatch(monkeypatch)
    out = _local(toy.toy_spec(), port, store, clock)
    assert seen[0] == IN_PROGRESS_CODE  # inside the lease: not read, not sent
    assert seen[-1] is None and len(seen) == 2  # after the lease: read back, SUCCEEDED
    assert engine.LEASE_RESEND_WAIT_S in clock.sleeps
    assert _fee_sends(port) == []  # never sent again
    assert out.results["fee"][-1].status is OperationStatus.SUCCEEDED


def test_a_store_on_the_default_clock_takes_the_runs_clock():
    clock = toy.Clock()
    store = InMemoryOperationStore()
    _local(toy.toy_spec(), toy.ToyPort(), store, clock)
    rec = store.get(FEE)
    assert rec.attempt_started_at is not None
    assert abs(rec.attempt_started_at - clock.t) < timedelta(hours=1)  # fake time, not wall


# --- crashed resends wait out the lease ----------------------------------------------------------


class _KilledStore(InMemoryOperationStore):
    """A killed Lambda never writes its own crash record: the attempt stays PENDING."""

    def complete(self, request_id, result):
        if result.error_message and "the send raised" in result.error_message:
            return self.get(request_id)
        return super().complete(request_id, result)


class _LosingPort(toy.ToyPort):
    """The first fee send dies before it reaches the provider (lost)."""

    lose: int = 1

    def execute(self, op):
        if op.request_id == FEE and self.lose:
            self.lose -= 1
            self.sends.append(op)
            raise toy.Crash("adapter Lambda timed out before the request left")
        return super().execute(op)


def _assert_landed_once(out, port, store):
    assert len(_fee_sends(port)) == 1  # the killed send; read back, never sent again
    assert out.results["fee"][-1].status is OperationStatus.SUCCEEDED
    assert store.get(FEE).status == "SUCCEEDED"


def _assert_lost_sent_once(out, port, store):
    assert len(_fee_sends(port)) == 2  # the lost send and exactly one resend
    created = [o for o in port.created.values() if o["request_id"] == FEE]
    assert len(created) == 1  # one transfer at the provider
    assert out.results["fee"][-1].status is OperationStatus.SUCCEEDED


def test_after_a_crash_a_landed_send_is_read_back_after_the_lease():
    clock = toy.Clock()
    store = _KilledStore(now=clock.now)
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    out = _local(toy.toy_spec(), port, store, clock)
    assert clock.sleeps[0] == engine.LEASE_RESEND_WAIT_S
    _assert_landed_once(out, port, store)


def test_after_a_crash_a_lost_send_is_sent_once_after_the_lease():
    clock = toy.Clock()
    store = _KilledStore(now=clock.now)
    port = _LosingPort()
    out = _local(toy.toy_spec(), port, store, clock)
    assert clock.sleeps[0] == engine.LEASE_RESEND_WAIT_S
    _assert_lost_sent_once(out, port, store)


def test_a_retryable_failure_keeps_the_short_resend_interval():
    # Guard: only crashes and in_progress_elsewhere wait out the lease.
    clock = toy.Clock()
    port = toy.ToyPort(rate_limit_times=Counter({"transfers.create": 1}))
    _local(toy.toy_spec(), port, InMemoryOperationStore(now=clock.now), clock)
    assert engine.RESEND_INTERVAL_S in clock.sleeps
    assert engine.LEASE_RESEND_WAIT_S not in clock.sleeps


def _sfn(spec, aws, machines, monkeypatch, clock=None, store=None):
    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_supplier": spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", "pay_supplier")
    kit_step.root_spec.cache_clear()
    if clock is not None:

        def executor(kit, tenant_id):
            ex = MutationExecutor(
                aws.port,
                store,
                sleep=clock.sleep,
                max_attempts=adapter_core.MAX_ATTEMPTS_PER_CALL,
                clock=clock.now,
            )
            return ex, store

        monkeypatch.setattr(adapter, "_executor", executor)
    lenv = kit_step.LambdaEnv()
    proposal, decision = engine.propose(
        spec,
        (),
        toy.RAW,
        proposed_by="requester-sub",
        tenant=lenv.resolve_tenant("platform"),
        env=lenv,
        origin=Origin.SCHEDULE,
    )
    data = sfn_json(encode(engine.execution_input(proposal, decision, toy.RAW, None)))
    interp = Interpreter(machines["pay_supplier"], toy.no_decision, aws)
    if clock is not None:
        interp.on_wait = clock.sleep
    final = interp.execute("root", data, proposal.proposal_id)["run"]
    return interp, final, proposal


def _result(final):
    return final["results"]["fee"][-1]["status"]


@pytest.mark.synth
def test_asl_after_a_crash_a_landed_send_is_read_back_after_the_lease(aws, machines, monkeypatch):
    clock = toy.Clock()
    store = _KilledStore(now=clock.now)
    aws.port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    interp, final, _ = _sfn(toy.toy_spec(), aws, machines, monkeypatch, clock, store)
    assert {"Crashed1Fee", "ResendWait1Fee"} <= set(interp.visited)
    assert engine.LEASE_RESEND_WAIT_S in clock.sleeps  # the Wait state's SecondsPath
    assert len(_fee_sends(aws.port)) == 1
    assert _result(final) == "SUCCEEDED"
    assert store.get(FEE).status == "SUCCEEDED"


@pytest.mark.synth
def test_asl_after_a_crash_a_lost_send_is_sent_once_after_the_lease(aws, machines, monkeypatch):
    clock = toy.Clock()
    store = _KilledStore(now=clock.now)
    aws.port = _LosingPort()
    _, final, _ = _sfn(toy.toy_spec(), aws, machines, monkeypatch, clock, store)
    assert engine.LEASE_RESEND_WAIT_S in clock.sleeps
    assert len(_fee_sends(aws.port)) == 2
    assert len([o for o in aws.port.created.values() if o["request_id"] == FEE]) == 1
    assert _result(final) == "SUCCEEDED"


# --- budgets and timeouts ------------------------------------------------------------------------


def test_the_run_budget_covers_three_lease_waits():
    assert engine.child_budget_s(toy.toy_spec()) >= 2 * engine.MAX_RESENDS * (
        engine.LEASE_RESEND_WAIT_S
    )


@pytest.mark.synth
def test_the_adapter_timeout_is_shorter_than_the_attempt_lease():
    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from awx_spine.executor import DEFAULT_ATTEMPT_LEASE_S
    from infra.stacks.spine_stack import ADAPTER_TIMEOUT_S

    assert ADAPTER_TIMEOUT_S < DEFAULT_ATTEMPT_LEASE_S  # resends past the lease depend on it
