"""Read retries in kit code: ctx.read and ctx.read_platform through engine.retrying.

A transient read failure (429, 5xx, a read timeout) is retried up to 3 times with exponential
backoff, or the error's Retry-After when it carries one, as long as the wait fits in the
caller's budget, in every phase. A non-transient error is never retried.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from awx_common import Origin
from awx_common.errors import RateLimited, TransientServerError, ValidationFailed
from awx_common.ops import ReadRequest
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.handlers import kit_step
from awx_spine_aws.local_runner import run_kit_workflow

import toy

BALANCE = "/api/v1/balances/current"


def _local(spec, port, store=None, *, clock=None):
    clock = clock or toy.Clock()
    return run_kit_workflow(
        spec,
        toy.RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=port,
        store=store if store is not None else InMemoryOperationStore(),
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.SCHEDULE,
    )


# --- retries in a run ----------------------------------------------------------------------------


class FlakyBalances(toy.ToyPort):
    """The balance read fails with the scripted errors (in order) from the `after`-th read."""

    def __init__(self, faults, *, after=1, **kw):
        super().__init__(**kw)
        self.faults = list(faults)
        self.after = after
        self.balance_reads = 0

    def read(self, req: ReadRequest):
        if req.path == BALANCE:
            self.balance_reads += 1
            if self.balance_reads > self.after and self.faults:
                raise self.faults.pop(0)
        return super().read(req)


def test_a_429_at_recheck_is_retried_and_the_run_is_not_stale():
    port = FlakyBalances([RateLimited("slow down", http_status=429)], after=1)
    clock = toy.Clock()
    out = _local(toy.toy_spec(), port, clock=clock)
    assert out.state == "SUCCEEDED", out.detail
    assert port.balance_reads == 3  # propose, recheck's 429, recheck's retry
    assert 1.0 in clock.sleeps  # the first backoff


def test_a_429_in_the_postcondition_is_retried():
    spec = toy.toy_spec()
    base = spec.postcondition

    def post(ctx):
        ctx.policy.read(ReadRequest(BALANCE))
        return base(ctx)

    port = FlakyBalances([TransientServerError("down", http_status=503)], after=2)
    out = _local(replace(spec, postcondition=post), port)
    assert out.state == "SUCCEEDED", out.detail
    assert port.balance_reads == 4


def test_retries_stop_after_three_and_the_error_is_raised():
    faults = [RateLimited("slow down", http_status=429) for _ in range(4)]
    port = FlakyBalances(faults, after=1)
    out = _local(toy.toy_spec(), port)
    assert out.state == "FAILED"  # STALE: the recheck still could not read
    assert "RateLimited" in out.detail
    assert port.balance_reads == 1 + 4


def test_a_non_transient_read_error_is_never_retried():
    port = FlakyBalances([ValidationFailed("bad query", http_status=400)], after=1)
    out = _local(toy.toy_spec(), port)
    assert out.state == "FAILED"
    assert "ValidationFailed" in out.detail
    assert port.balance_reads == 2  # propose, then the one failed recheck read


# --- engine.retrying -----------------------------------------------------------------------------


class _Env:
    def __init__(self, budget=None):
        self.sleeps: list[float] = []
        self.budget = budget

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def read_budget_s(self):
        return self.budget


def _failing(errors):
    errors = list(errors)

    def read():
        if errors:
            raise errors.pop(0)
        return {"ok": True}

    return read


def test_backoff_is_exponential():
    env = _Env()
    errs = [RateLimited("x", http_status=429) for _ in range(3)]
    assert engine.retrying(env, _failing(errs), "/p") == {"ok": True}
    assert env.sleeps == [1.0, 2.0, 4.0]


def test_retry_after_is_honoured():
    env = _Env()
    err = RateLimited("x", http_status=429, details={"retry_after": "7"})
    engine.retrying(env, _failing([err]), "/p")
    assert env.sleeps == [7.0]


def test_a_wait_past_the_budget_raises_instead_of_sleeping():
    env = _Env(budget=1.5)
    errs = [RateLimited("x", http_status=429) for _ in range(2)]
    with pytest.raises(RateLimited):
        engine.retrying(env, _failing(errs), "/p")
    assert env.sleeps == [1.0]  # the 2 s wait would not fit


def test_lambda_env_budget_comes_from_the_remaining_time():
    class Ctx:
        def get_remaining_time_in_millis(self):
            return 40_000

    assert kit_step.LambdaEnv(Ctx()).read_budget_s() == 40.0 - kit_step.LambdaEnv.MARGIN_S
    assert kit_step.LambdaEnv().read_budget_s() is None


@pytest.mark.parametrize(
    "err", [ValidationFailed("x", http_status=400), engine.TenantScopeViolation("x")]
)
def test_non_transient_errors_raise_at_once(err):
    env = _Env()
    with pytest.raises(type(err)):
        engine.retrying(env, _failing([err]), "/p")
    assert env.sleeps == []
