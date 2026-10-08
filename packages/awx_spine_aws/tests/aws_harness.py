"""Shared AWS harness for the handler and ASL tests: moto tables and bucket, the real Lambda
handlers wired together in-process, the toy port behind awx_spine's real executor."""

from __future__ import annotations

import io
import json
import sys
import types
from collections.abc import Mapping
from typing import Any

import boto3
import pytest
from awx_common import Origin, RiskTier
from awx_common.transport import encode
from awx_spine_aws import adapter_core, engine
from awx_spine_aws.handlers import _common, adapter, approval, kit_step, reader
from moto import mock_aws

import toy

TABLES = {
    "AWX_OPERATIONS_TABLE": ("ops", "request_id", None),
    "AWX_PROPOSALS_TABLE": ("proposals", "proposal_id", None),
    "AWX_DECISIONS_TABLE": ("decisions", "proposal_id", "approver"),
    "AWX_TENANTS_TABLE": ("tenants", "tenant_id", None),
    "AWX_AUDIT_TABLE": ("audit", "proposal_id", "at"),
    "AWX_LEDGER_TABLE": ("ledger", "pk", "sk"),
}
BY_TENANT = {
    "IndexName": "by_tenant",
    "KeySchema": [
        {"AttributeName": "tenant_id", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}
BY_APPROVER = {  # proposals by approver_tenant
    "IndexName": "by_approver",
    "KeySchema": [
        {"AttributeName": "approver_tenant", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}
MODULE = "awx_s2_testkit"


class FakeLambda:
    """Stands in for boto3's Lambda client: invokes the reader handler in-process."""

    def invoke(self, FunctionName: str, Payload: bytes) -> Mapping[str, Any]:
        try:
            out = reader.handler(json.loads(Payload), None)
            return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(out).encode())}
        except Exception as exc:
            body = {"errorType": type(exc).__name__, "errorMessage": str(exc)}
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(body).encode()),
            }


class FakeSfn:
    class exceptions:
        class TaskTimedOut(Exception):
            pass

        class InvalidToken(Exception):
            pass

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.status = "RUNNING"  # what describe_execution reports
        self.fail_sends: list[type] = []  # exceptions the next sends raise, in order
        self.before_send = None  # a hook run before each send (to stage a race)
        self.described: list[str] = []

    def send_task_success(self, taskToken: str, output: str) -> None:
        if self.before_send is not None:
            hook, self.before_send = self.before_send, None
            hook()
        if any(t == taskToken for t, _ in self.sent):  # a token works once
            raise self.exceptions.InvalidToken("token already used")
        if self.fail_sends:
            raise self.fail_sends.pop(0)("token no longer valid")
        self.sent.append((taskToken, output))

    def describe_execution(self, executionArn: str) -> dict:
        self.described.append(executionArn)
        return {"executionArn": executionArn, "status": self.status}


def create_tables(db: Any) -> None:
    for name, pk, sk in TABLES.values():
        keys = [{"AttributeName": pk, "KeyType": "HASH"}]
        attrs = [{"AttributeName": pk, "AttributeType": "S"}]
        if sk:
            keys.append({"AttributeName": sk, "KeyType": "RANGE"})
            attrs.append({"AttributeName": sk, "AttributeType": "S"})
        extra: dict[str, Any] = {}
        if name == "proposals":
            attrs += [
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "approver_tenant", "AttributeType": "S"},
            ]
            extra["GlobalSecondaryIndexes"] = [BY_TENANT, BY_APPROVER]
        db.create_table(
            TableName=name,
            KeySchema=keys,
            AttributeDefinitions=attrs,
            BillingMode="PAY_PER_REQUEST",
            **extra,
        )


@pytest.fixture
def aws(monkeypatch):
    port = toy.ToyPort()
    sfn = FakeSfn()
    with mock_aws():
        db = boto3.client("dynamodb")
        create_tables(db)
        for var, (name, _, _) in TABLES.items():
            monkeypatch.setenv(var, name)
        db.put_item(
            TableName="tenants",
            Item={  # a row belongs to one kit (the toy kit is k3)
                "tenant_id": {"S": "tenant-a"},
                "connected_account_id": {"S": "acct_A"},
                "kit": {"S": "k3"},
                "role": {"S": "customer"},
            },
        )
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket="artifacts", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        spec = toy.toy_spec()
        allow = {
            "k3": {
                "tenant": list(spec.read_allowlist),
                "platform": list(spec.platform_read_allowlist),
            }
        }
        monkeypatch.setenv("AWX_READ_ALLOWLISTS", json.dumps(allow))
        monkeypatch.setenv("AWX_READER_KIT", "k3")  # every reader is a per-kit reader
        monkeypatch.setenv("AWX_ARTIFACTS_BUCKET", "artifacts")
        monkeypatch.setenv("AWX_SECRET_ARN", "arn:aws:secretsmanager:ap-southeast-2:1:secret:x")
        monkeypatch.setenv("AWX_READER_FUNCTION", "reader")
        monkeypatch.setenv("AWX_SPEC_MODULE", MODULE)
        monkeypatch.setenv("AWX_ACTION", "pay_supplier")
        monkeypatch.setenv("AWX_SANDBOX_MODE", "1")

        clients = {"dynamodb": db, "lambda": FakeLambda(), "stepfunctions": sfn, "s3": s3}
        for module in (adapter, reader, approval, kit_step):
            monkeypatch.setattr(module, "boto_client", clients.__getitem__)

        from awx_spine.executor import MutationExecutor
        from awx_spine_aws.store import DynamoDbOperationStore

        built: list = []

        def executor(kit, tenant_id):
            built.append((kit, tenant_id))
            store = DynamoDbOperationStore("ops", client=db)
            ex = MutationExecutor(
                ns.port,
                store,
                sleep=lambda s: None,
                max_attempts=adapter_core.MAX_ATTEMPTS_PER_CALL,
            )
            return ex, store

        ns = types.SimpleNamespace(db=db, port=port, sfn=sfn, s3=s3, executors=built)
        monkeypatch.setattr(adapter, "_executor", executor)
        monkeypatch.setattr(reader, "_client", lambda: ns.port)
        yield ns
        kit_step.root_spec.cache_clear()
        _common.reset_caches()


@pytest.fixture
def testkit(monkeypatch):
    module = types.ModuleType(MODULE)
    module.SPECS = {"pay_supplier": toy.toy_spec(RiskTier.SINGLE)}
    monkeypatch.setitem(sys.modules, MODULE, module)
    kit_step.root_spec.cache_clear()
    return module


def use_spec(testkit, spec, action: str = "pay_supplier", monkeypatch=None) -> None:
    testkit.SPECS[action] = spec
    if monkeypatch is not None:
        monkeypatch.setenv("AWX_ACTION", action)
    kit_step.root_spec.cache_clear()


def sfn_json(value: Any) -> Any:
    """What a value looks like after Step Functions has stored it between two states."""
    return json.loads(json.dumps(value))


def step(phase: engine.Phase, run: Any = None, **extra: Any) -> dict[str, Any]:
    event = {"phase": phase.value, **extra}
    if run is not None:
        event["run"] = run
    return sfn_json(kit_step.handler(sfn_json(event), None))


def execution(tenant_id=None, raw=None, origin=Origin.SCHEDULE) -> tuple[str, dict[str, Any]]:
    """What the agent plane's propose tool does: preview the policy, build the proposal,
    start the run."""
    root = kit_step.root_spec()
    lenv = kit_step.LambdaEnv()
    tenant = lenv.resolve_tenant(engine.resolve_tenant_id(tenant_id))
    raw = raw or toy.RAW
    proposal, decision = engine.propose(
        root, (), raw, proposed_by="requester-sub", tenant=tenant, env=lenv, origin=origin
    )
    data = sfn_json(encode(engine.execution_input(proposal, decision, raw, tenant_id)))
    return proposal.proposal_id, data


def start(tenant_id=None, raw=None, origin=Origin.SCHEDULE) -> dict[str, Any]:
    name, data = execution(tenant_id, raw, origin)
    return step(engine.Phase.START, execution=data, execution_name=name)
