"""Synth-time security assertions over the spine stack and a toy kit workflow.

These read the synthesised templates, so they hold for whatever IAM CDK generates, not
just for the statements written by hand.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from aws_cdk.assertions import Annotations, Match, Template
from awx_common.regions import DEFAULT_REGION

pytestmark = pytest.mark.synth

WRITE_VERBS = re.compile(
    r":(Put|Update|Delete|Create|Batch|Send|Start|Invoke|Write|Transact|Encrypt|GenerateDataKey)"
)


@pytest.fixture(scope="module")
def templates(synthed) -> dict[str, dict[str, Any]]:
    out = {
        "spine": Template.from_stack(synthed["spine"]).to_json(),
        "toy": Template.from_stack(synthed["toy"]).to_json(),
    }
    _ALL_ROLES[:] = [
        (stack, lid, res)
        for stack, t in out.items()
        for lid, res in t["Resources"].items()
        if res["Type"] == "AWS::IAM::Role"
    ]
    return out


def resources(templates, kind: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for stack, t in templates.items():
        for lid, res in t["Resources"].items():
            if res["Type"] == kind:
                yield stack, lid, res


def policies(templates) -> Iterator[tuple[str, list[str], dict[str, Any]]]:
    """(stack, attached role logical ids, statement) for every identity policy statement."""
    for stack, _lid, res in resources(templates, "AWS::IAM::Policy"):
        roles = [_role_ref(r) for r in res["Properties"].get("Roles", [])]
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            yield stack, roles, stmt
    for stack, lid, res in resources(templates, "AWS::IAM::Role"):
        for pol in res["Properties"].get("Policies", []):
            for stmt in pol["PolicyDocument"]["Statement"]:
                yield stack, [lid], stmt


def _role_ref(ref):
    """A role in this stack is {"Ref": id}; the spine's role seen from a kit stack is an
    import of an export named after it, so return the logical id it names."""
    if "Ref" in ref:
        return ref["Ref"]
    name = text(ref.get("Fn::ImportValue"))
    for lid in ("ApprovalRole", "ReaderRole", "AdapterRole"):
        if lid in name:
            return next(r for _s, r, res in _ALL_ROLES if r.startswith(lid))
    return name


_ALL_ROLES: list = []


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def logical_id(templates, stack: str, kind: str, prefix: str) -> str:
    matches = [
        lid for s, lid, _ in resources(templates, kind) if s == stack and lid.startswith(prefix)
    ]
    assert len(matches) == 1, (prefix, matches)
    return matches[0]


def roles_granted(templates, action: str, resource_marker: str) -> set[str]:
    out: set[str] = set()
    for _stack, roles, stmt in policies(templates):
        if stmt.get("Effect") != "Allow":
            continue
        actions = as_list(stmt["Action"])
        if not any(a == action or (a.endswith("*") and action.startswith(a[:-1])) for a in actions):
            continue
        resources_ = as_list(stmt.get("Resource"))
        wildcard = any(
            r == "*" or (isinstance(r, str) and r.endswith(":*") and ":function:" in r)
            for r in resources_
        )  # a "*" anywhere in a list, or a function ARN wildcard, counts
        if resource_marker in text(resources_) or wildcard:
            out.update(roles)
    return out


def test_only_reader_and_adapter_roles_can_read_the_secret(templates):
    secret = logical_id(templates, "spine", "AWS::SecretsManager::Secret", "AirwallexSecret")
    readers = roles_granted(templates, "secretsmanager:GetSecretValue", secret)
    # Cross-stack the toy stack sees the secret through an import; check both forms.
    readers |= roles_granted(templates, "secretsmanager:GetSecretValue", "AirwallexSecret")
    assert readers == {
        logical_id(templates, "spine", "AWS::IAM::Role", "ReaderRole"),
        logical_id(templates, "spine", "AWS::IAM::Role", "AdapterRole"),
    }


def test_the_secret_is_created_empty_and_encrypted(templates):
    [(_, _, secret)] = list(resources(templates, "AWS::SecretsManager::Secret"))
    props = secret["Properties"]
    assert "GenerateSecretString" not in props and "SecretString" not in props
    assert "SpineKey" in text(props["KmsKeyId"])


def test_only_kit_workflow_state_machines_can_invoke_the_adapter(templates):
    adapter = logical_id(templates, "spine", "AWS::Lambda::Function", "AdapterFn")
    granted = roles_granted(templates, "lambda:InvokeFunction", adapter)
    granted |= roles_granted(templates, "lambda:InvokeFunction", "AdapterFn")
    assert granted, "no state machine can reach the adapter"
    machine_roles = {
        res["Properties"]["RoleArn"]["Fn::GetAtt"][0]
        for _, _, res in resources(templates, "AWS::StepFunctions::StateMachine")
    }
    assert granted <= machine_roles
    step_role = logical_id(templates, "toy", "AWS::IAM::Role", "PayAllStepRole")
    assert step_role not in granted  # tool and step Lambdas never reach the adapter
    lambda_roles = {
        res["Properties"]["Role"]["Fn::GetAtt"][0]
        for _, _, res in resources(templates, "AWS::Lambda::Function")
    }
    assert not granted & lambda_roles


def test_no_resource_policy_lets_another_principal_invoke_the_adapter(templates):
    adapter = logical_id(templates, "spine", "AWS::Lambda::Function", "AdapterFn")
    for _, _, perm in resources(templates, "AWS::Lambda::Permission"):
        assert adapter not in text(perm["Properties"]["FunctionName"])


def test_every_table_uses_the_spine_kms_key_and_pitr(templates):
    tables = list(resources(templates, "AWS::DynamoDB::Table"))
    assert len(tables) == 6
    for _, lid, table in tables:
        sse = table["Properties"]["SSESpecification"]
        assert sse["SSEEnabled"] is True and sse["SSEType"] == "KMS", lid
        assert "SpineKey" in text(sse["KMSMasterKeyId"]), lid
        assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST", lid
        pitr = table["Properties"]["PointInTimeRecoverySpecification"]
        assert pitr["PointInTimeRecoveryEnabled"] is True, lid


def test_bucket_topic_logs_use_the_spine_key(templates):
    [(_, _, bucket)] = list(resources(templates, "AWS::S3::Bucket"))
    rule = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert "SpineKey" in text(rule["ServerSideEncryptionByDefault"]["KMSMasterKeyID"])
    for _, lid, topic in resources(templates, "AWS::SNS::Topic"):
        assert "SpineKey" in text(topic["Properties"]["KmsMasterKeyId"]), lid
    for _, lid, group in resources(templates, "AWS::Logs::LogGroup"):
        assert "SpineKey" in text(group["Properties"]["KmsKeyId"]), lid
        assert group["Properties"]["RetentionInDays"] == 30, lid


def test_no_wildcard_actions_anywhere(templates):
    for stack, roles, stmt in policies(templates):
        for action in as_list(stmt["Action"]):
            assert action != "*" and not action.endswith(":*"), (stack, roles, action)


def test_no_wildcard_resources_on_write_paths(templates):
    """A write action may have Resource "*" only where AWS offers no resource-level control:
    X-Ray telemetry (CDK tracing), Step Functions log delivery and states:SendTaskSuccess
    (task tokens)."""
    allowed = {
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "states:SendTaskSuccess",
        # Step Functions log delivery (CDK-generated); these APIs take no resource ARN.
        "logs:CreateLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:PutResourcePolicy",
    }
    for stack, roles, stmt in policies(templates):
        if stmt.get("Effect") != "Allow" or "*" not in as_list(stmt.get("Resource")):
            continue
        for action in as_list(stmt["Action"]):
            if WRITE_VERBS.search(action):
                assert action in allowed, (stack, roles, action)


def test_send_task_success_only_on_the_approval_role(templates):
    granted = roles_granted(templates, "states:SendTaskSuccess", "*")
    assert granted == {logical_id(templates, "spine", "AWS::IAM::Role", "ApprovalRole")}


def test_lambdas_are_arm64_python312_with_log_groups(templates):
    fns = list(resources(templates, "AWS::Lambda::Function"))
    assert len(fns) == 5  # k6 reader, adapter, approval, pre-token, toy step (no shared reader)
    for _, lid, fn in fns:
        props = fn["Properties"]
        assert props["Architectures"] == ["arm64"], lid
        assert props["Runtime"] == "python3.12", lid
        assert "LoggingConfig" in props, lid


def test_each_kit_reads_only_through_its_own_reader(templates):
    """The kit comes from the reader's configuration and IAM lets only that kit's step role
    invoke it; no kit invokes a shared reader."""
    kit_reader = logical_id(templates, "spine", "AWS::Lambda::Function", "ReaderK6Fn")
    props = templates["spine"]["Resources"][kit_reader]["Properties"]
    env = props["Environment"]["Variables"]
    assert env["AWX_READER_KIT"] == "k6"
    assert set(json.loads(env["AWX_READ_ALLOWLISTS"])) == {"k6"}
    reader_role = logical_id(templates, "spine", "AWS::IAM::Role", "ReaderRole")
    assert props["Role"]["Fn::GetAtt"][0] == reader_role  # no new secret holder
    step_role = logical_id(templates, "toy", "AWS::IAM::Role", "PayAllStepRole")
    invokers = roles_granted(templates, "lambda:InvokeFunction", "ReaderK6Fn")
    assert invokers == {step_role}
    step = logical_id(templates, "toy", "AWS::Lambda::Function", "PayAllStepFn")
    step_env = templates["toy"]["Resources"][step]["Properties"]["Environment"]["Variables"]
    assert "ReaderK6Fn" in text(step_env["AWX_READER_FUNCTION"])


def test_every_reader_is_per_kit_and_there_is_no_shared_reader(templates):
    """Every function running awx_spine_aws.handlers.reader fixes its kit (AWX_READER_KIT)."""
    readers = [
        (lid, fn)
        for _s, lid, fn in resources(templates, "AWS::Lambda::Function")
        if fn["Properties"]["Handler"] == "awx_spine_aws.handlers.reader.handler"
    ]
    assert readers
    for lid, fn in readers:
        env = fn["Properties"]["Environment"]["Variables"]
        assert env.get("AWX_READER_KIT"), lid
        assert set(json.loads(env["AWX_READ_ALLOWLISTS"])) <= {env["AWX_READER_KIT"]}, lid
    assert not any(lid.startswith("ReaderFn") for lid, _ in readers)


def test_reader_gets_the_toy_kits_allowlist(templates):
    reader = logical_id(templates, "spine", "AWS::Lambda::Function", "ReaderK6Fn")
    env = templates["spine"]["Resources"][reader]["Properties"]["Environment"]["Variables"]
    allow = json.loads(env["AWX_READ_ALLOWLISTS"])
    assert allow == {
        "k6": {
            "tenant": [r"^/api/v1/balances/current$", r"^/api/v1/transfers/[A-Za-z0-9_-]+$"],
            "platform": [r"^/api/v1/transfers/[A-Za-z0-9_-]+$"],
        }
    }


def test_approval_api_routes_require_the_cognito_authorizer(templates):
    routes = list(resources(templates, "AWS::ApiGatewayV2::Route"))
    assert {r["Properties"]["RouteKey"] for _, _, r in routes} == {
        "GET /proposals",
        "GET /proposals/{proposal_id}",
        "POST /proposals/{proposal_id}/decision",
    }
    for _, lid, route in routes:
        assert route["Properties"]["AuthorizationType"] == "JWT", lid
    [(_, _, auth)] = list(resources(templates, "AWS::ApiGatewayV2::Authorizer"))
    assert auth["Properties"]["AuthorizerType"] == "JWT"
    jwt = auth["Properties"]["JwtConfiguration"]
    assert "UserPool" in text(jwt["Issuer"])
    [audience] = jwt["Audience"]
    assert "AgentClient" in text(audience)  # the awx-agent app client, nothing else


def test_user_pool_has_groups_and_an_immutable_tenant_claim(templates):
    [(_, _, pool)] = list(resources(templates, "AWS::Cognito::UserPool"))
    attrs = {a["Name"]: a for a in pool["Properties"]["Schema"]}
    assert attrs["tenant_id"]["Mutable"] is False
    assert pool["Properties"]["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    groups = {
        g["Properties"]["GroupName"]
        for _, _, g in resources(templates, "AWS::Cognito::UserPoolGroup")
    }
    assert groups == {"requester", "approver"}
    [(_, _, client)] = list(resources(templates, "AWS::Cognito::UserPoolClient"))
    assert client["Properties"]["ClientName"] == "awx-agent"
    assert "custom:tenant_id" not in client["Properties"].get("WriteAttributes", [])
    trigger = pool["Properties"]["LambdaConfig"]["PreTokenGenerationConfig"]
    assert trigger["LambdaVersion"] == "V2_0"
    assert "PreTokenFn" in text(trigger["LambdaArn"])
    assert pool["Properties"]["UserPoolTier"] == "ESSENTIALS"


def test_budget_alarm_is_100_usd_to_the_alert_email(templates):
    [(_, _, budget)] = list(resources(templates, "AWS::Budgets::Budget"))
    data = budget["Properties"]["Budget"]
    assert data["BudgetLimit"] == {"Amount": 100, "Unit": "USD"}
    assert data["TimeUnit"] == "MONTHLY"
    subs = {
        s["Address"]
        for n in budget["Properties"]["NotificationsWithSubscribers"]
        for s in n["Subscribers"]
    }
    assert subs == {"alerts@example.com"}


def test_adapter_error_alarms_notify_the_topic(templates):
    adapter = logical_id(templates, "spine", "AWS::Lambda::Function", "AdapterFn")
    alarms = [
        a
        for _, _, a in resources(templates, "AWS::CloudWatch::Alarm")
        if adapter in text(a["Properties"].get("Dimensions"))
    ]
    assert {a["Properties"]["MetricName"] for a in alarms} == {"Errors", "Throttles"}
    assert all("AlarmTopic" in text(a["Properties"]["AlarmActions"]) for a in alarms)


def _definitions(templates):
    machines = {
        lid: res for _, lid, res in resources(templates, "AWS::StepFunctions::StateMachine")
    }
    defs = {}
    for lid, res in machines.items():
        parts = res["Properties"]["DefinitionString"]["Fn::Join"][1]
        defs[lid] = json.loads("".join(p if isinstance(p, str) else "REF" for p in parts))
        assert res["Properties"]["StateMachineType"] == "STANDARD"
    root = next(d for lid, d in defs.items() if "Root" in lid)
    child = next(d for lid, d in defs.items() if "Child" in lid)
    return machines, root, child


def test_state_machines_follow_the_spec(templates):
    machines, root, child = _definitions(templates)
    assert len(machines) == 2  # pay_all and its child
    assert "RequestApproval" not in child["States"]  # rule 3: children never wait
    assert root["States"]["Approved"]["Choices"][0]["Next"] == "Recheck"  # rule 1
    assert root["States"]["Error"]["Next"] == "AfterStop"  # rule 2
    after = root["States"]["AfterStop"]["Choices"][0]
    assert after == {
        "Variable": "$.run.postcondition_due",
        "BooleanEquals": True,
        "Next": "Postcondition",
    }
    assert child["States"]["Route1Fund"]["Choices"][0]["Next"] == "AfterStop"
    start_child = root["States"]["Children"]["ItemProcessor"]["States"]["StartChild"]
    assert start_child["Parameters"]["Name.$"] == "$.proposal.proposal_id"
    assert start_child["Parameters"]["Input"]["origin.$"] == "$.origin"  # rule 11
    assert start_child["Catch"][0]["Next"] == "ChildLost"  # a lost child still finishes
    approval = root["States"]["RequestApproval"]
    assert approval["Resource"].endswith(":states:::lambda:invoke.waitForTaskToken")
    assert approval["TimeoutSeconds"] == 4 * 3600  # spec.approval_ttl
    assert approval["Catch"][0] == {
        "ErrorEquals": ["States.Timeout"],
        "ResultPath": "$.error",
        "Next": "Binding",
    }
    assert root["States"]["Children"]["Type"] == "Map"
    assert child["States"]["FinalityWait1Fund"]["SecondsPath"] == "$.run.step.wait_s"
    states = list(child["States"])
    assert states.index("Build1Fund") < states.index("Build2Pay")
    assert child["States"]["Route1Fund"]["Default"] == "Build2Pay"


def test_adapter_failures_after_a_possible_send_go_to_reconcile(templates):
    """A timeout or crash is never a plain FAILED; only refusals go to Error,
    and nothing but an invoke throttle is ever retried automatically (rule 9)."""
    _, _, child = _definitions(templates)
    for n in ("1Fund", "2Pay"):
        adapter = child["States"][f"Adapter{n}"]
        assert [r["ErrorEquals"] for r in adapter.get("Retry", [])] == [
            ["Lambda.TooManyRequestsException"]
        ]
        assert adapter["Catch"] == [
            {
                "ErrorEquals": ["NotAuthorised", "TenantScopeViolation", "RequestIdReuse"],
                "ResultPath": "$.error",
                "Next": "Error",
            },
            {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": f"Crashed{n}"},
        ]
        assert child["States"][f"Crashed{n}"]["Next"] == f"Route{n}"
        assert child["States"][f"Dispatch{n}"]["Choices"][0]["Next"] == f"AdapterComplete{n}"


def test_postcondition_and_audit_are_never_retried(templates):
    """No automatic retry on the states that must run once."""
    _, root, child = _definitions(templates)
    for definition in (root, child):
        for name in ("Postcondition", "PostconditionError", "Audit"):
            assert "Retry" not in definition["States"][name], name


def test_adapter_timeout_covers_one_call(templates):
    from infra.stacks.spine_stack import ADAPTER_TIMEOUT_S, CLIENT_TIMEOUT_S

    adapter = logical_id(templates, "spine", "AWS::Lambda::Function", "AdapterFn")
    timeout = templates["spine"]["Resources"][adapter]["Properties"]["Timeout"]
    assert timeout == ADAPTER_TIMEOUT_S
    assert timeout > 3 * CLIENT_TIMEOUT_S  # read-back, send, read-back, each one timeout


def test_step_lambda_runs_in_sandbox_mode(templates):
    step = logical_id(templates, "toy", "AWS::Lambda::Function", "PayAllStepFn")
    env = templates["toy"]["Resources"][step]["Properties"]["Environment"]["Variables"]
    assert env["AWX_SANDBOX_MODE"] == "1"


def test_every_wildcard_resource_action_is_one_aws_scopes_no_further(templates):
    """Every action granted on Resource "*" is on a known list, reads included, so a blanket
    nag suppression cannot hide a new one."""
    allowed = {
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "states:SendTaskSuccess",
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups",
        "xray:GetSamplingRules",  # CDK tracing on state machines
        "xray:GetSamplingTargets",
    }
    for stack, roles, stmt in policies(templates):
        if stmt.get("Effect") != "Allow" or "*" not in as_list(stmt.get("Resource")):
            continue
        for action in as_list(stmt["Action"]):
            assert action in allowed, (stack, roles, action)


def test_pre_token_role_has_no_kms_and_the_reader_cannot_encrypt(templates):
    pre = logical_id(templates, "spine", "AWS::IAM::Role", "PreTokenRole")
    reader = logical_id(templates, "spine", "AWS::IAM::Role", "ReaderRole")
    actions = {r: set() for r in (pre, reader)}
    for _stack, roles, stmt in policies(templates):
        for r in roles:
            if r in actions:
                actions[r] |= set(as_list(stmt["Action"]))
    assert not {a for a in actions[pre] if a.startswith("kms:")}
    assert "kms:GenerateDataKey" not in actions[reader]
    assert "kms:Encrypt" not in actions[reader]


def test_only_the_adapter_role_reads_evidence(templates):
    readers = set()
    for _stack, roles, stmt in policies(templates):
        actions = as_list(stmt["Action"])
        if stmt.get("Effect") == "Allow" and any(
            a.startswith("s3:Get") or a == "s3:*" for a in actions
        ):
            readers.update(roles)
            assert text(stmt["Resource"]).endswith('"/evidence/*"]]}') or "/evidence/*" in text(
                stmt["Resource"]
            ), stmt
    assert readers == {logical_id(templates, "spine", "AWS::IAM::Role", "AdapterRole")}
    adapter = logical_id(templates, "spine", "AWS::Lambda::Function", "AdapterFn")
    env = templates["spine"]["Resources"][adapter]["Properties"]["Environment"]["Variables"]
    assert "ArtifactsBucket" in text(env["AWX_ARTIFACTS_BUCKET"])


def test_artifacts_bucket_is_versioned_private_and_tls_only(templates):
    [(_, _, bucket)] = list(resources(templates, "AWS::S3::Bucket"))
    props = bucket["Properties"]
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    [(_, _, policy)] = list(resources(templates, "AWS::S3::BucketPolicy"))
    denies = [
        s for s in policy["Properties"]["PolicyDocument"]["Statement"] if s["Effect"] == "Deny"
    ]
    assert any(s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}} for s in denies)


def test_operations_table_has_no_ttl(templates):
    ops = logical_id(templates, "spine", "AWS::DynamoDB::Table", "OperationsTable")
    props = templates["spine"]["Resources"][ops]["Properties"]
    assert "TimeToLiveSpecification" not in props
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


def test_proposals_table_has_the_tenant_and_approver_indexes(templates):
    table = logical_id(templates, "spine", "AWS::DynamoDB::Table", "ProposalsTable")
    gsis = templates["spine"]["Resources"][table]["Properties"]["GlobalSecondaryIndexes"]
    keys = {g["IndexName"]: [k["AttributeName"] for k in g["KeySchema"]] for g in gsis}
    assert keys == {  # by_approver: what a tenant's approvers decide
        "by_tenant": ["tenant_id", "created_at"],
        "by_approver": ["approver_tenant", "created_at"],
    }


def test_kms_key_policy_admits_logs_and_agentcore_only_from_this_account(templates):
    [(_, _, key)] = list(resources(templates, "AWS::KMS::Key"))
    services = {}
    for stmt in key["Properties"]["KeyPolicy"]["Statement"]:
        svc = stmt.get("Principal", {}).get("Service")
        if svc:
            services[text(svc)] = stmt
    assert any(f"logs.{DEFAULT_REGION}" in k for k in services)
    agentcore = services['"bedrock-agentcore.amazonaws.com"']
    assert agentcore["Condition"] == {"StringEquals": {"aws:SourceAccount": "111122223333"}}
    logs_stmt = next(v for k, v in services.items() if f"logs.{DEFAULT_REGION}" in k)
    arn = logs_stmt["Condition"]["ArnLike"]["kms:EncryptionContext:aws:logs:arn"]
    assert arn == f"arn:aws:logs:{DEFAULT_REGION}:111122223333:log-group:*"
    for stmt in services.values():
        assert "Condition" in stmt


def test_cdk_nag_passes(synthed):
    for stack in (synthed["spine"], synthed["toy"]):
        errors = Annotations.from_stack(stack).find_error(
            "*", Match.string_like_regexp("AwsSolutions-.*")
        )
        assert errors == [], [e.entry.data for e in errors]
        warnings = Annotations.from_stack(stack).find_warning(
            "*", Match.string_like_regexp("AwsSolutions-.*")
        )
        assert warnings == [], [w.entry.data for w in warnings]


def test_each_kit_step_role_is_pinned_to_its_own_ledger_partitions(templates):
    """dynamodb:LeadingKeys keeps a kit inside "<kit>#*"."""
    ledger = logical_id(templates, "spine", "AWS::DynamoDB::Table", "LedgerTable")
    step_role = logical_id(templates, "toy", "AWS::IAM::Role", "PayAllStepRole")
    stmts = [
        stmt
        for _s, roles, stmt in policies(templates)
        if step_role in roles and ledger in text(stmt.get("Resource"))
    ]
    assert stmts
    for stmt in stmts:
        assert stmt["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["k6#*"]}}


def test_agent_tool_proposal_grant_reads_only_proposal_id_and_outcome(templates):
    probe = logical_id(templates, "toy", "AWS::IAM::Role", "ToolRoleProbe")
    proposals = logical_id(templates, "spine", "AWS::DynamoDB::Table", "ProposalsTable")
    stmts = [
        stmt
        for _s, roles, stmt in policies(templates)
        if probe in roles and proposals in text(stmt.get("Resource"))
    ]
    assert len(stmts) == 1
    [stmt] = stmts
    assert as_list(stmt["Action"]) == ["dynamodb:GetItem"]
    assert stmt["Condition"] == {
        "ForAllValues:StringEquals": {"dynamodb:Attributes": ["proposal_id", "outcome"]},
        "StringEqualsIfExists": {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"},
    }


def test_client_timeout_is_read_from_the_client():
    import inspect

    from awx_spine.client import AirwallexClient
    from infra.stacks.spine_stack import CLIENT_TIMEOUT_S

    default = inspect.signature(AirwallexClient.__init__).parameters["timeout_s"].default
    assert default == CLIENT_TIMEOUT_S


def test_approval_role_describes_only_the_kits_root_executions(templates):
    approval = logical_id(templates, "spine", "AWS::IAM::Role", "ApprovalRole")
    stmts = [
        stmt
        for _s, roles, stmt in policies(templates)
        if approval in roles and "states:DescribeExecution" in as_list(stmt["Action"])
    ]
    assert len(stmts) == 1
    [stmt] = stmts
    [resource] = as_list(stmt["Resource"])
    rendered = text(resource)
    assert ":execution:" in rendered
    assert "PayAllRootMachine" in rendered  # the kit's root machine, not "*"
    assert "ChildMachine" not in rendered
