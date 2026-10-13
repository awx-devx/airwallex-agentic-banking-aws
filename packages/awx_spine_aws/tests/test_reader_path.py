"""The deployed read path: the kit step invoking the reader Lambda, and the reader itself.

The kit step's read budget keeps room for a whole reader call. An Airwallex error crossing the
reader keeps its http_status and details (appended to the message, capped at 1 KB). Invoke
throttling, invoke timeouts and a reader Lambda timeout count as transient. awx_spine's client
is asked for one attempt per read, so the engine is the only retry layer.
"""

from __future__ import annotations

import io
import json
import sys
import types

import pytest
from awx_common.errors import RateLimited, TransientServerError
from awx_common.ops import ReadRequest
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import encode
from awx_spine.client import AirwallexClient
from awx_spine.testing import ScriptedTransport, creds, login_ok, resp
from awx_spine_aws import codec, engine, local_runner
from awx_spine_aws.handlers import kit_step, reader

import toy
from aws_harness import MODULE

# --- the read budget keeps room for a whole reader call ------------------------------------------


def test_the_budget_margin_covers_a_reader_call():
    assert kit_step.LambdaEnv.MARGIN_S >= kit_step.READER_TIMEOUT_S + kit_step.INVOKE_OVERHEAD_S
    ctx = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 60_000)
    assert kit_step.LambdaEnv(ctx).read_budget_s() <= 60 - kit_step.READER_TIMEOUT_S


@pytest.mark.synth
def test_the_deployed_reader_timeout_fits_the_margin(monkeypatch):
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from awx_common.regions import DEFAULT_REGION
    from infra.app import build_app
    from infra.constructs.kit_workflow import STEP_HANDLER, KitWorkflow

    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_supplier": toy.toy_spec()}
    monkeypatch.setitem(sys.modules, MODULE, module)
    app = cdk.App(context={"account": "111122223333", "region": DEFAULT_REGION})
    build_app(app)
    env = cdk.Environment(account="111122223333", region=DEFAULT_REGION)
    kit = cdk.Stack(app, "MarginKit", env=env)
    spine = app.node.find_child("AwxSpine")
    from infra.kit_manifest import parse_workflow

    KitWorkflow(
        kit,
        "Wf",
        spine=spine,
        kit="k3",
        action="pay_supplier",
        spec_module=MODULE,
        workflow=parse_workflow(module.SPECS["pay_supplier"]),  # the manifest, not the spec
        allow_stand_in=True,  # MODULE is an in-memory stand-in, not a staged kit package
    )
    readers, steps = [], []
    for stack in app.node.children:
        if not isinstance(stack, cdk.Stack):
            continue
        for res in Template.from_stack(stack).to_json()["Resources"].values():
            if res["Type"] != "AWS::Lambda::Function":
                continue
            props = res["Properties"]
            if props.get("Handler") == "awx_spine_aws.handlers.reader.handler":
                readers.append(props.get("Timeout", 3))
            if props.get("Handler") == STEP_HANDLER:
                steps.append(props.get("Timeout", 3))
    assert readers and steps
    assert max(readers) <= kit_step.READER_TIMEOUT_S
    assert min(steps) > kit_step.LambdaEnv.MARGIN_S  # some budget is left to retry in


# --- errors crossing the reader keep their fields ------------------------------------------------


class _Lambda:
    def __init__(self, *, error=None, function_error=None):
        self.error, self.function_error = error, function_error

    def invoke(self, FunctionName, Payload):
        if self.error is not None:
            raise self.error
        body = json.dumps(self.function_error).encode()
        return {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": io.BytesIO(body)}


def _invoke(monkeypatch, client):
    monkeypatch.setenv("AWX_READER_FUNCTION", "reader")
    monkeypatch.setattr(kit_step, "boto_client", lambda name: client)
    return kit_step.LambdaEnv._invoke({"kit": "k3"})


def test_a_reader_error_keeps_http_status_and_details(monkeypatch):
    exc = RateLimited(
        "slow down", code="too_many_requests", http_status=429, details={"retry_after": 2}
    )
    codec.with_error_fields(exc)  # what the reader handler does before re-raising
    payload = {"errorType": "RateLimited", "errorMessage": str(exc)}
    with pytest.raises(RateLimited) as got:
        _invoke(monkeypatch, _Lambda(function_error=payload))
    assert str(got.value) == "slow down"
    assert got.value.http_status == 429
    assert got.value.code == "too_many_requests"
    assert got.value.details == {"retry_after": 2}
    assert engine._retry_after(got.value) == 2


def test_the_reader_handler_marks_its_errors(aws, monkeypatch):
    class Port(toy.ToyPort):
        def read(self, req):
            raise TransientServerError("bad gateway", http_status=502, details={"retry_after": 1})

    aws.port = Port()
    from awx_spine_aws.handlers import reader

    event = encode(
        {"kit": "k3", "read": ReadRequest("/api/v1/balances/current"), "tenant_id": None}
    )
    with pytest.raises(TransientServerError) as got:
        reader.handler(event, None)
    text, fields = codec.error_fields(str(got.value))
    assert text == "bad gateway"
    assert fields == {"code": None, "http_status": 502, "details": {"retry_after": 1}}


def test_the_appended_error_fields_are_capped_and_still_parse():
    big = {"source": "x" * 5000, "retry_after_s": 2}
    exc = TransientServerError("bad gateway", code="upstream", http_status=502, details=big)
    codec.with_error_fields(exc)
    appended = str(exc).rpartition(codec.ERROR_MARK)[2]
    assert len(appended.encode()) <= codec.ERROR_FIELDS_MAX + 1  # the JSON plus "]"
    text, fields = codec.error_fields(str(exc))
    assert text == "bad gateway"
    assert fields["http_status"] == 502
    assert fields["code"] == "upstream"
    assert fields["details"] == {"truncated": True}


def test_small_error_fields_are_unchanged():
    # Guard: under the cap, details pass through whole.
    exc = TransientServerError("bad gateway", http_status=502, details={"retry_after_s": 2})
    codec.with_error_fields(exc)
    assert codec.error_fields(str(exc))[1]["details"] == {"retry_after_s": 2}


# --- invoke errors and reader timeouts -----------------------------------------------------------


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class ReadTimeoutError(Exception):
    """Named like botocore's."""


@pytest.mark.parametrize(
    "error", [_ClientError("TooManyRequestsException"), ReadTimeoutError("read timed out")]
)
def test_invoke_throttling_and_timeouts_are_transient(monkeypatch, error):
    with pytest.raises(TransientServerError) as got:
        _invoke(monkeypatch, _Lambda(error=error))
    assert engine.transient_read_error(got.value) is not None


def test_other_invoke_errors_are_not_transient(monkeypatch):
    with pytest.raises(_ClientError):
        _invoke(monkeypatch, _Lambda(error=_ClientError("AccessDeniedException")))


def test_a_reader_lambda_timeout_is_a_transient_read_failed(monkeypatch):
    # Guard for engine.transient_read_error's Sandbox.Timedout prefix match.
    payload = {
        "errorType": "Sandbox.Timedout",
        "errorMessage": "Task timed out after 30.00 seconds",
    }
    with pytest.raises(kit_step.ReadFailed) as got:
        _invoke(monkeypatch, _Lambda(function_error=payload))
    assert str(got.value).startswith("Sandbox.Timedout")
    why = engine.transient_read_error(got.value)
    assert why is not None and "timeout" in why and "ReadFailed" in why


# --- one retry layer on reads --------------------------------------------------------------------


BALANCE = ReadRequest("/api/v1/balances/current")


def _client(transport) -> AirwallexClient:
    return AirwallexClient(
        creds(), transport=transport, clock=lambda: 1_000.0, sleep=lambda s: None
    )


def test_a_transient_read_is_attempted_once_per_engine_retry():
    t = ScriptedTransport([login_ok()] + [resp(503, {"code": "unavailable"})] * 20)
    env = local_runner._LocalEnv(
        port=_client(t),
        clock=toy.Clock().now,
        kit_store=InMemoryKitStore(),
        platform_store=InMemoryKitStore(),
        tenants=None,
        sandbox=True,
        sleeper=lambda s: None,
    )
    ctx = engine.policy_context(toy.toy_spec(), env, TenantContext("platform"))
    with pytest.raises(TransientServerError):
        ctx.read(BALANCE)
    assert len(t.api_calls()) == engine.READ_RETRIES + 1  # 4, not (2 + 1) * 4 = 12


def test_the_reader_lambda_asks_the_client_for_one_attempt(aws, monkeypatch):
    t = ScriptedTransport([login_ok()] + [resp(503, {"code": "unavailable"})] * 5)
    monkeypatch.setattr(reader, "_client", lambda: _client(t))
    event = encode({"kit": "k3", "read": BALANCE, "tenant_id": None})
    with pytest.raises(TransientServerError):
        reader.handler(event, None)
    assert len(t.api_calls()) == 1
