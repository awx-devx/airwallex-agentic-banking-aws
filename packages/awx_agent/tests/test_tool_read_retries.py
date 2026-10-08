"""The tool plane retries reads through the engine.

The reader makes one attempt, so the engine's `retrying` is the only retry layer, and
GatewayToolEnv must give it `sleep` and `read_budget_s`. It has no run_status: a policy at
propose time sees PolicyContext.run_status = None and decides at recheck.
"""

from __future__ import annotations

import io
import json
import types

import awx_agent_stubs as stubs
from awx_spine_aws.handlers import kit_step


def test_the_tool_plane_retries_a_429_and_the_tool_answers():

    class Flaky(stubs.StubLambda):
        def __init__(self):
            super().__init__()
            self.fails = 1

        def invoke(self, *, FunctionName, InvocationType, Payload):
            if self.fails:
                self.fails -= 1
                self.calls.append({"FunctionName": FunctionName})
                fields = {"code": "too_many", "http_status": 429, "details": {"retry_after_s": 2}}
                msg = f"slow down [awx-error {json.dumps(fields)}]"
                raw = json.dumps({"errorType": "RateLimited", "errorMessage": msg}).encode()
                return {"FunctionError": "Unhandled", "Payload": io.BytesIO(raw)}
            return super().invoke(
                FunctionName=FunctionName, InvocationType=InvocationType, Payload=Payload
            )

    lam = Flaky()
    d, _, _ = stubs.dispatcher(lam=lam)
    waits: list = []
    d.env.sleeper = waits.append
    d.env.context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 60_000)
    out = d.dispatch("read_balances", stubs.event({}))
    assert "error" not in out, out
    assert out["untrusted_data"]["items"][0]["available"] == "500.00"
    assert len(lam.calls) == 2
    assert waits == [2.0]  # the reader's Retry-After reached engine.retrying


def test_the_tool_plane_budget_keeps_room_for_a_reader_call():

    d, _lam, _sfn = stubs.dispatcher()
    assert d.env.read_budget_s() is None  # no Lambda context: unbounded
    d.env.context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 60_000)
    assert d.env.read_budget_s() == 60 - kit_step.READER_TIMEOUT_S - kit_step.INVOKE_OVERHEAD_S
    assert getattr(d.env, "run_status", None) is None  # decide at recheck
