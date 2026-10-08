"""Strict stand-ins for the boto3 clients and spine pieces the tool handler uses.

Each stub implements only the calls the handler is allowed to make, so any other call (an
adapter invoke, a send_task_success, a store write) fails the test.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from awx_agent.handlers.tools import GatewayToolEnv, ToolDispatcher
from awx_agent.identity import IDENTITY_FIELD
from awx_agent.registry import load_kit_tools
from awx_common import transport
from awx_common.errors import TenantScopeViolation
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from jwt_helpers import token, verifier

READER_FN = "spine-reader"
ADAPTER_FN = "spine-adapter"
SM_ARN = "arn:aws:states:ap-southeast-2:111111111111:stateMachine:k1-pay_supplier"
NOW = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
TENANTS = {"tenant-a": "acct_A", "tenant-b": "acct_B"}


def default_reader(req: Mapping[str, Any]) -> Any:
    if req["read"]["path"] == "/api/v1/account":  # the fixture's platform-scope read
        return {"id": "acct_platform"}
    return {"items": [{"currency": "AUD", "available": "500.00"}]}


class StubLambda:
    def __init__(self, respond: Callable[[Mapping[str, Any]], Any] | None = None, *, fail=None):
        self.calls: list[dict[str, Any]] = []
        self._respond = respond or default_reader
        self._fail = fail  # None, or an errorType string the reader raises

    def invoke(self, *, FunctionName, InvocationType, Payload):
        decoded = transport.loads(Payload.decode())
        self.calls.append({"FunctionName": FunctionName, "payload": decoded})
        if self._fail:
            raw = json.dumps({"errorType": self._fail, "errorMessage": "nope"}).encode()
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(raw)}
        body = self._respond(decoded)
        return {"Payload": io.BytesIO(transport.dumps({"body": body}).encode())}


class AlreadyExists(Exception):
    def __init__(self):
        super().__init__("ExecutionAlreadyExists")
        self.response = {"Error": {"Code": "ExecutionAlreadyExists"}}


class StubSfn:
    """Step Functions name uniqueness as documented for Standard workflows: StartExecution with
    a used name and the SAME input is idempotent and returns the existing execution; with a
    different input it raises ExecutionAlreadyExists."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.executions: dict[str, str] = {}
        self.status: dict[str, str] = {}  # executionArn -> status; default RUNNING

    def start_execution(self, *, stateMachineArn, name, input):
        self.calls.append({"stateMachineArn": stateMachineArn, "name": name, "input": input})
        arn = stateMachineArn.replace(":stateMachine:", ":execution:") + f":{name}"
        if arn in self.executions:
            if self.executions[arn] == input and self.status.get(arn, "RUNNING") == "RUNNING":
                return {"executionArn": arn, "startDate": NOW}
            raise AlreadyExists()
        self.executions[arn] = input
        return {"executionArn": arn, "startDate": NOW}

    def describe_execution(self, *, executionArn):
        return {
            "executionArn": executionArn,
            "status": self.status.get(executionArn, "RUNNING"),
            "input": self.executions[executionArn],
        }


class DictResolver:
    def resolve(self, tenant_id: str) -> TenantContext:
        if tenant_id == "platform":
            return TenantContext(tenant_id="platform")
        if tenant_id not in TENANTS:
            raise TenantScopeViolation("unknown tenant")
        return TenantContext(tenant_id=tenant_id, connected_account_id=TENANTS[tenant_id])


def caller(**over) -> str:
    return token(**over)


def event(args: Mapping[str, Any], tok: str | None = None) -> dict[str, Any]:
    e = dict(args)
    e[IDENTITY_FIELD] = tok if tok is not None else caller()
    return e


def dispatcher(lam=None, sfn=None, kit="k1", stores=None, clock=None, outcomes=None):
    plan = load_kit_tools("k1", "awx_fixture_kit.tools")
    lam = lam or StubLambda()
    sfn = sfn or StubSfn()
    stores = {} if stores is None else stores

    def store_factory(k, tenant):
        return stores.setdefault((k, tenant.tenant_id), InMemoryKitStore())

    env = GatewayToolEnv(
        kit=kit,  # this env invokes only reader_for(kit)
        lambda_client=lam,
        reader_fn=READER_FN,
        resolver=DictResolver(),
        store_factory=store_factory,
        clock=clock or (lambda: NOW),
    )
    d = ToolDispatcher(
        kit=kit,
        tools={t.name: t for t in plan.tools},
        # The fixture kit is declared as k1; tests that need a connected-account kit (k5)
        # re-label its specs so the whole dispatcher really serves that kit.
        specs={a: replace(s, kit=kit) for a, s in plan.specs.items()},
        reads=plan.reads,
        env=env,
        sfn_client=sfn,
        state_machines={
            "pay_supplier": SM_ARN,
            "probe_write": SM_ARN.replace("pay_supplier", "probe_write"),
        },
        verifier=verifier(),
        proposal_outcome=(outcomes.get if outcomes is not None else None),
    )
    return d, lam, sfn
