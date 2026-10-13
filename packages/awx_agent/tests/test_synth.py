"""Synth AgentStack against a stand-in SpineStack and check its security shape."""

import json

import pytest
from awx_common.regions import DEFAULT_REGION

pytestmark = pytest.mark.synth


def _fixture_manifest():
    from infra.kit_manifest import build_manifest

    return build_manifest("k1", package="awx_fixture_kit")


@pytest.fixture(scope="module")
def synth():
    """The real app (SpineStack + AgentStack, cdk-nag on) with the fixture kit registered."""
    import os

    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"  # stage source only; no network in synth tests
    from aws_cdk import App
    from aws_cdk import aws_stepfunctions as sfn
    from aws_cdk.assertions import Template
    from awx_agent.registry import register_kit_tools
    from constructs import Construct

    from infra.app import build_app

    class StandInWorkflow(Construct):
        def __init__(self, scope, id):
            super().__init__(scope, id)
            self.state_machine = sfn.StateMachine(
                self, "SM", definition_body=sfn.DefinitionBody.from_chainable(sfn.Pass(self, "P"))
            )

    app = build_app(App(), kits=())  # the fixture kit stands in for k1 here
    spine = app.node.find_child("AwxSpine")
    agent = app.node.find_child("AwxAgentStack")
    wf = StandInWorkflow(spine, "K1Pay")
    tool_fn = register_kit_tools(
        agent,
        gateway=agent.gateway,
        spine=spine,
        kit="k1",
        manifest=_fixture_manifest(),
        workflows={"pay_supplier": wf, "probe_write": wf},
    )
    return {
        "agent": Template.from_stack(agent),
        "spine_template": Template.from_stack(spine),
        "stack": agent,
        "spine": spine,
        "tool_fn": tool_fn,
        "register": register_kit_tools,
        "wf": wf,
    }


def _policies(template, role_fragment):
    out = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        roles = json.dumps(res["Properties"].get("Roles", []))
        if role_fragment in roles:
            out.extend(res["Properties"]["PolicyDocument"]["Statement"])
    return out


def _actions(statements):
    acts = set()
    for s in statements:
        a = s["Action"]
        acts |= set(a if isinstance(a, list) else [a])
    return acts


def test_runtime_is_code_deployed_with_cognito_jwt_and_authorization_header(synth):
    t = synth["agent"]
    (rt,) = t.find_resources("AWS::BedrockAgentCore::Runtime").values()
    props = rt["Properties"]
    art = props["AgentRuntimeArtifact"]
    assert "CodeConfiguration" in art
    assert "ContainerConfiguration" not in art
    assert art["CodeConfiguration"]["Runtime"] == "PYTHON_3_12"
    assert art["CodeConfiguration"]["EntryPoint"] == ["main.py"]
    jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert "DiscoveryUrl" in jwt
    assert jwt["AllowedClients"]
    assert props["RequestHeaderConfiguration"]["RequestHeaderAllowlist"] == ["Authorization"]
    envv = props["EnvironmentVariables"]
    assert envv["AWX_MODEL_ID"] == "us.anthropic.claude-sonnet-5"
    assert envv["AWX_FALLBACK_MODEL_ID"] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert envv["AWX_REGION"] == DEFAULT_REGION


def test_gateway_uses_cognito_and_identity_interceptor(synth):
    t = synth["agent"]
    (gw,) = t.find_resources("AWS::BedrockAgentCore::Gateway").values()
    props = gw["Properties"]
    assert props["AuthorizerType"] == "CUSTOM_JWT"
    assert props["ProtocolType"] == "MCP"
    (ic,) = props["InterceptorConfigurations"]
    assert ic["InterceptionPoints"] == ["REQUEST"]
    assert ic["InputConfiguration"]["PassRequestHeaders"] is True


def test_kit_target_has_inline_schema_of_read_and_propose_tools(synth):
    t = synth["agent"]
    (target,) = t.find_resources("AWS::BedrockAgentCore::GatewayTarget").values()
    props = target["Properties"]
    assert props["Name"] == "k1"
    tools = props["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
    assert [x["Name"] for x in tools] == [
        "read_balances",
        "propose_pay_supplier",
        "propose_probe_write",
    ]
    blob = json.dumps(tools)
    for owned in ("tenant_id", "on_behalf_of", "request_id", "approver"):
        assert f'"{owned}"' not in blob
    for tool in tools:  # the server-filled identity property, declared and optional
        schema = tool["InputSchema"]
        assert schema["Properties"]["_awx_caller"]["Type"] == "string"
        assert "_awx_caller" not in schema.get("Required", [])


def test_tool_role_is_read_and_propose_only(synth):
    t = synth["agent"]
    stmts = _policies(t, "K1ToolsHandler")
    assert _actions(stmts) == {
        "lambda:InvokeFunction",
        "states:StartExecution",
        "states:DescribeExecution",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "kms:Decrypt",
    }
    blob = json.dumps(stmts)
    assert "ReaderK1Fn" in blob  # this kit's own reader
    assert "Adapter" not in blob
    invokes = [s for s in stmts if s["Action"] == "lambda:InvokeFunction"]
    targets = json.dumps([s["Resource"] for s in invokes])
    assert "ReaderK1Fn" in targets
    assert "ReaderFn" not in targets.replace("ReaderK1Fn", "")  # no other reader
    (ledger,) = [s for s in stmts if s.get("Sid") == "ReadOwnKitLedger"]
    assert ledger["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["k1#*"]}}
    (tenants,) = [s for s in stmts if s.get("Sid") == "ResolveTenant"]
    assert tenants["Action"] == "dynamodb:GetItem"


def test_tool_role_reads_only_proposal_id_and_outcome(synth):
    """spine.grant_proposal_outcome_read: never task_token or execution_arn (2c)."""
    stmts = _policies(synth["agent"], "K1ToolsHandler")
    props = [s for s in stmts if "ProposalsTable" in json.dumps(s["Resource"])]
    assert props
    for s in props:
        assert s["Action"] == "dynamodb:GetItem"
        attrs = s["Condition"]["ForAllValues:StringEquals"]["dynamodb:Attributes"]
        assert attrs == ["proposal_id", "outcome"]
        assert "task_token" not in attrs
        assert "execution_arn" not in attrs
        assert s["Condition"]["StringEqualsIfExists"] == {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"}


def test_every_reader_is_per_kit_and_no_shared_reader_remains(synth):
    readers = {
        lid: res
        for lid, res in synth["spine_template"].find_resources("AWS::Lambda::Function").items()
        if res["Properties"]["Handler"] == "awx_spine_aws.handlers.reader.handler"
    }
    assert readers
    for lid, res in readers.items():
        env = res["Properties"]["Environment"]["Variables"]
        assert env.get("AWX_READER_KIT"), lid
        assert set(json.loads(env["AWX_READ_ALLOWLISTS"])) <= {env["AWX_READER_KIT"]}, lid
    assert not any(lid.startswith("ReaderFn") for lid in readers)
    assert not hasattr(synth["spine"], "reader_fn")


def test_gateway_kms_grant_is_pinned_to_this_gateway(synth):
    """The gateway uses the shared name; the key admits the AgentCore service principal only
    from this account (no undocumented aws:SourceArn); the gateway role holds the key."""
    from infra.stacks.spine_stack import AGENT_GATEWAY_NAME

    (gw,) = synth["agent"].find_resources("AWS::BedrockAgentCore::Gateway").values()
    assert gw["Properties"]["Name"] == AGENT_GATEWAY_NAME
    (key,) = synth["spine_template"].find_resources("AWS::KMS::Key").values()
    (stmt,) = [
        s
        for s in key["Properties"]["KeyPolicy"]["Statement"]
        if s.get("Principal", {}).get("Service") == "bedrock-agentcore.amazonaws.com"
    ]
    assert stmt["Condition"] == {"StringEquals": {"aws:SourceAccount": "111122223333"}}
    gw_role = [
        s
        for res in synth["agent"].find_resources("AWS::IAM::Policy").values()
        if "AgentGatewayServiceRole" in json.dumps(res["Properties"]["Roles"])
        for s in res["Properties"]["PolicyDocument"]["Statement"]
    ]
    kms = [s for s in gw_role if any(a.startswith("kms:") for a in s["Action"])]
    assert kms
    # Every KMS statement names exactly the spine key, and nothing more.
    spine_key = {"Fn::ImportValue": "AwxSpine:ExportsOutputFnGetAttSpineKey"}
    for s in kms:
        resource = s["Resource"]
        assert set(resource) == {"Fn::ImportValue"}, resource
        assert resource["Fn::ImportValue"].startswith(spine_key["Fn::ImportValue"]), resource
    actions = {a for s in kms for a in s["Action"]}
    assert actions == {
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:GenerateDataKeyPair",
        "kms:GenerateDataKeyPairWithoutPlaintext",
        "kms:GenerateDataKey*",
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:ReEncryptFrom",
        "kms:ReEncryptTo",
        "kms:ReEncrypt*",
        "kms:CreateGrant",
        "kms:DescribeKey",
    }


def test_tool_lambda_verifies_tokens_of_the_spine_agent_client(synth):
    t = synth["agent"]
    fns = [
        f
        for f in t.find_resources("AWS::Lambda::Function").values()
        if f["Properties"]["Handler"] == "awx_agent.handlers.tools.handler"
    ]
    (fn,) = fns
    env = fn["Properties"]["Environment"]["Variables"]
    assert "AgentClient" in json.dumps(env["AWX_EXPECTED_CLIENT_ID"])
    assert f"cognito-idp.{DEFAULT_REGION}.amazonaws.com" in json.dumps(env["AWX_EXPECTED_ISSUER"])
    for var in ("AWX_LEDGER_TABLE", "AWX_TENANTS_TABLE", "AWX_READER_FN", "AWX_STATE_MACHINES"):
        assert var in env


def test_runtime_role_cannot_reach_lambdas_secrets_or_workflows(synth):
    t = synth["agent"]
    stmts = _policies(t, "AgentRuntime")
    acts = _actions(stmts)
    assert "bedrock:InvokeModel" in acts
    forbidden = ("lambda:", "secretsmanager:", "states:", "dynamodb:")
    assert not any(a.startswith(forbidden) for a in acts), acts
    assert "Adapter" not in json.dumps(stmts)


def test_nothing_in_the_agent_stack_mentions_the_adapter(synth):
    assert "Adapter" not in json.dumps(synth["agent"].to_json())


def test_lambdas_are_arm64_python312(synth):
    for fn in synth["agent"].find_resources("AWS::Lambda::Function").values():
        assert fn["Properties"]["Architectures"] == ["arm64"]
        assert fn["Properties"]["Runtime"] == "python3.12"


def test_agent_stack_has_no_own_app_client(synth):
    """One awx-agent client (SpineStack.agent_client) for Gateway, Runtime and approvals."""
    assert synth["agent"].find_resources("AWS::Cognito::UserPoolClient") == {}


def test_outputs_match_cli_keys(synth):
    outputs = synth["agent"].to_json()["Outputs"]
    for key in ("AwxRegion", "AwxUserPoolClientId", "AwxRuntimeArn", "AwxApprovalApiUrl"):
        assert key in outputs


def test_register_refuses_missing_or_extra_workflows(synth):
    from awx_agent.registry import ToolRegistrationError

    reg, stack, spine = synth["register"], synth["stack"], synth["spine"]
    with pytest.raises(ToolRegistrationError, match="no KitWorkflow"):
        reg(
            stack,
            gateway=stack.gateway,
            spine=spine,
            kit="k1",
            manifest=_fixture_manifest(),
            workflows={},
        )
    with pytest.raises(ToolRegistrationError, match="undeclared"):
        reg(
            stack,
            gateway=stack.gateway,
            spine=spine,
            kit="k1",
            manifest=_fixture_manifest(),
            workflows={
                "pay_supplier": synth["wf"],
                "probe_write": synth["wf"],
                "drain": synth["wf"],
            },
        )


def test_real_app_synthesizes_with_cdk_nag_clean_for_the_agent_stack():
    """infra/app.py with the real SpineStack, a registered kit and AwsSolutions checks: no nag
    error on AgentStack, and every suppression is resource-level with appliesTo (no
    stack-wide suppression that could hide a future wildcard)."""
    import os

    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    from aws_cdk import App
    from aws_cdk import aws_stepfunctions as sfn
    from aws_cdk.assertions import Annotations, Match
    from awx_agent.registry import register_kit_tools
    from constructs import Construct

    from infra.app import build_app

    app = build_app(App(), kits=())  # the fixture kit stands in for k1
    spine = app.node.find_child("AwxSpine")
    agent = app.node.find_child("AwxAgentStack")

    class Wf(Construct):
        def __init__(self, scope, id):
            super().__init__(scope, id)
            self.state_machine = sfn.StateMachine(
                self, "SM", definition_body=sfn.DefinitionBody.from_chainable(sfn.Pass(self, "P"))
            )

    wf = Wf(spine, "FixtureWf")
    register_kit_tools(
        agent,
        gateway=agent.gateway,
        spine=spine,
        kit="k1",
        manifest=_fixture_manifest(),
        workflows={"pay_supplier": wf, "probe_write": wf},
    )
    errors = Annotations.from_stack(agent).find_error("*", Match.string_like_regexp(".*"))
    assert errors == [], [e.entry.data for e in errors][:5]
    template = app.synth().get_stack_by_name("AwxAgentStack").template
    assert "cdk_nag" not in json.dumps(template.get("Metadata", {}))
    for res in template["Resources"].values():
        for rule in res.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", []):
            if rule["id"] != "AwsSolutions-L1":
                assert rule.get("applies_to"), f"{rule['id']} suppressed without appliesTo"


EM_AARCH64 = 183


def _elf_machine(path):
    head = path.read_bytes()[:20]
    assert head[:4] == b"\x7fELF", f"{path} is not ELF"
    assert head[4] == 2, "not ELF64"
    return int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")


def test_synthesized_lambda_and_runtime_assets_carry_aarch64_cryptography(monkeypatch):
    """Inspect cdk.out: every agent-plane asset that verifies tokens carries cryptography's
    and cffi's extension modules built for linux/arm64 (staged without Docker)."""
    from pathlib import Path

    from aws_cdk import App

    from infra.app import build_app

    monkeypatch.delenv("AWX_SKIP_RUNTIME_DEPS", raising=False)  # stage the real Runtime asset
    app = build_app(App())
    assembly = app.synth()
    template = assembly.get_stack_by_name("AwxAgentStack").template
    out = Path(assembly.directory)

    lambda_assets = []
    for res in template["Resources"].values():
        if res["Type"] == "AWS::Lambda::Function" and res["Properties"]["Handler"].startswith(
            "awx_agent."
        ):
            key = res["Properties"]["Code"]["S3Key"]
            lambda_assets.append(out / f"asset.{key.removesuffix('.zip')}")
    runtime_assets = [p.parent for p in out.glob("asset.*/main.py")]
    assert lambda_assets, "no agent-plane Lambda in the stack"
    assert len(runtime_assets) == 1, "expected one Runtime code asset"

    for asset in [*lambda_assets, *runtime_assets]:
        rust = list(asset.glob("cryptography/hazmat/bindings/_rust*.so"))
        cffi = list(asset.glob("_cffi_backend*.so"))
        assert rust, f"{asset.name} has no cryptography binary"
        assert cffi, f"{asset.name} has no cffi binary"
        for so in (*rust, *cffi):
            assert _elf_machine(so) == EM_AARCH64, f"{so} is not aarch64"
        assert (asset / "jwt" / "__init__.py").exists()
