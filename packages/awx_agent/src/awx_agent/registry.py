"""Turn a kit's ToolSpec list into AgentCore Gateway Lambda targets.

`load_kit_tools` is the pure half: it imports the kit's tools, workflow and reads modules and
refuses anything the agent plane must never expose (a server-owned field, a tool for an unknown
kit, a propose tool with no SPECS entry, a read tool with no READS entry, a name that is neither
read_* nor propose_*). `register_kit_tools` is the CDK half and runs only at synth time.

Each kit gets one tool Lambda and one Gateway target named after the kit, so the model sees
tools as `<kit>___<tool>`. The tool Lambda may invoke the spine reader and start this kit's
state machines. It gets no other permission: no adapter, no secret, no task-token callbacks.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from awx_common.kits import KITS
from awx_common.tools import SERVER_OWNED_FIELDS, ToolKind, ToolSpec, _property_names
from awx_common.workflow import KitWorkflowSpec

from .identity import IDENTITY_FIELD

TOOL_TARGET_DELIMITER = "___"


class ToolRegistrationError(ValueError):
    """A kit's tools cannot be registered as they stand."""


@dataclass(frozen=True)
class KitTools:
    kit: str
    tools_module: str
    workflow_module: str
    reads_module: str
    tools: tuple[ToolSpec, ...]
    specs: Mapping[str, KitWorkflowSpec]
    reads: Mapping[str, Any]

    @property
    def propose_actions(self) -> tuple[str, ...]:
        return tuple(
            t.name.removeprefix("propose_") for t in self.tools if t.kind is ToolKind.PROPOSE
        )


def sibling_module(tools_module: str, name: str) -> str:
    package, _, leaf = tools_module.rpartition(".")
    if leaf != "tools" or not package:
        raise ToolRegistrationError(f"tools_module {tools_module!r} must end in '.tools'")
    return f"{package}.{name}"


def validate_kit_tools(
    kit: str,
    tools: Any,
    specs: Mapping[str, KitWorkflowSpec],
    reads: Mapping[str, Any],
) -> tuple[ToolSpec, ...]:
    """Check one kit's declared tools against what the agent plane allows."""
    if kit not in KITS:
        raise ToolRegistrationError(f"unknown kit {kit!r}")
    tools = tuple(tools)
    if not tools:
        raise ToolRegistrationError(f"kit {kit} declares no tools")
    names = [t.name for t in tools if isinstance(t, ToolSpec)]
    if len(names) != len(tools):
        raise ToolRegistrationError("TOOLS must contain only awx_common.tools.ToolSpec")
    if len(set(names)) != len(names):
        raise ToolRegistrationError(f"kit {kit} declares a tool name twice")
    for spec in tools:
        # ToolSpec checks this in __post_init__; check again in case an object was mutated
        # with object.__setattr__ after construction, or built some other way.
        owned = _property_names(spec.input_schema) & (SERVER_OWNED_FIELDS | {IDENTITY_FIELD})
        if owned:
            raise ToolRegistrationError(
                f"tool {spec.name!r} exposes server-owned fields {sorted(owned)}"
            )
        if spec.input_schema.get("additionalProperties") is not False:
            raise ToolRegistrationError(f"tool {spec.name!r} must set additionalProperties false")
        if len(spec.name) + len(kit) + len(TOOL_TARGET_DELIMITER) > 64:
            raise ToolRegistrationError(f"tool {spec.name!r} is too long once prefixed by {kit}")
        if spec.kind is ToolKind.PROPOSE:
            action = spec.name.removeprefix("propose_")
            wf = specs.get(action)
            if wf is None:
                raise ToolRegistrationError(f"propose tool {spec.name!r} has no SPECS entry")
            if wf.kit != kit or wf.tool_name != spec.name:
                raise ToolRegistrationError(f"SPECS[{action!r}] belongs to another kit or tool")
        elif spec.kind is ToolKind.READ:
            if not callable(reads.get(spec.name)):
                raise ToolRegistrationError(f"read tool {spec.name!r} has no READS entry")
        else:  # pragma: no cover - ToolKind has two members
            raise ToolRegistrationError(f"tool {spec.name!r} has unknown kind")
    return tools


def load_kit_tools(kit: str, tools_module: str) -> KitTools:
    workflow_module = sibling_module(tools_module, "workflow")
    reads_module = sibling_module(tools_module, "reads")
    tools = getattr(importlib.import_module(tools_module), "TOOLS", None)
    specs = getattr(importlib.import_module(workflow_module), "SPECS", None)
    reads = getattr(importlib.import_module(reads_module), "READS", None)
    if tools is None or not isinstance(specs, Mapping) or not isinstance(reads, Mapping):
        raise ToolRegistrationError(f"kit {kit} must export TOOLS, SPECS and READS")
    return KitTools(
        kit=kit,
        tools_module=tools_module,
        workflow_module=workflow_module,
        reads_module=reads_module,
        tools=validate_kit_tools(kit, tools, specs, reads),
        specs=dict(specs),
        reads=dict(reads),
    )


def register_kit_tools(
    scope: Any,
    *,
    gateway: Any,
    spine: Any,
    kit: str,
    manifest: Any,
    workflows: Mapping[str, Any],
    client: Any | None = None,
) -> Any:
    """Add one kit's tools to the Gateway. Returns the kit's tool Lambda function.

    `manifest` is the kit's infra.kit_manifest.KitManifest: tool names, kinds, descriptions
    and schemas as data. This function imports no kit code (kit code never runs in the CDK
    process); the loader subprocess already ran load_kit_tools on the real modules.

    `workflows` maps each propose action to its KitWorkflow construct. Every propose tool must
    have one, and no workflow may be passed for an action the kit does not propose. `client`
    is the Cognito app client whose access tokens the tool Lambda accepts (default
    spine.agent_client).

    The tool role gets exactly: invoke this kit's own reader (spine.reader_for(kit)); start,
    and describe executions of, this kit's state machines; GetItem/Query on this kit's ledger
    partitions; GetItem on the tenants table; spine.grant_proposal_outcome_read (GetItem of
    only proposal_id and outcome, never the task token); decrypt with the spine key for those
    reads. No adapter, no secret, no SendTask*, no
    DynamoDB write.
    """
    from aws_cdk import Duration, RemovalPolicy, Stack
    from aws_cdk import aws_bedrockagentcore as agentcore
    from aws_cdk import aws_iam as iam
    from aws_cdk import aws_lambda as lambda_
    from aws_cdk import aws_logs as logs
    from constructs import Construct

    from .staging import kit_source_dirs, stage_lambda_source

    if manifest.kit != kit:
        raise ToolRegistrationError(f"manifest is for {manifest.kit}, not {kit}")
    tools_module = manifest.tools_module
    tool_specs = manifest.tool_specs()  # ToolSpec checks again, on data
    reader = spine.reader_for(kit)  # per-kit reader; there is no shared reader
    actions = set(manifest.propose_actions)
    missing = actions - set(workflows)
    if missing:
        raise ToolRegistrationError(f"kit {kit}: no KitWorkflow for actions {sorted(missing)}")
    extra = set(workflows) - actions
    if extra:
        raise ToolRegistrationError(f"kit {kit}: workflows for undeclared actions {sorted(extra)}")

    client = client if client is not None else spine.agent_client
    region = Stack.of(scope).region
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{spine.user_pool.user_pool_id}"
    holder = Construct(scope, f"{kit.upper()}Tools")
    asset_dir = stage_lambda_source(f"tools-{kit}", kit_source_dirs(tools_module))
    fn = lambda_.Function(
        holder,
        "Handler",
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.ARM_64,
        handler="awx_agent.handlers.tools.handler",
        code=lambda_.Code.from_asset(str(asset_dir)),
        timeout=Duration.seconds(60),
        memory_size=512,
        description=f"AgentCore Gateway tools for {kit} (read and propose only)",
        environment={
            "AWX_KIT": kit,
            "AWX_TOOLS_MODULE": tools_module,
            "AWX_READER_FN": reader.function_name,
            "AWX_LEDGER_TABLE": spine.ledger_table.table_name,
            "AWX_TENANTS_TABLE": spine.tenants_table.table_name,
            "AWX_PROPOSALS_TABLE": spine.proposals_table.table_name,
            "AWX_EXPECTED_ISSUER": issuer,
            "AWX_EXPECTED_CLIENT_ID": client.user_pool_client_id,
            "AWX_STATE_MACHINES": json.dumps(
                {a: workflows[a].state_machine.state_machine_arn for a in sorted(actions)}
            ),
        },
        log_group=logs.LogGroup(
            holder,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,  # never orphaned
            encryption_key=spine.kms_key,
        ),
    )
    reader.grant_invoke(fn)
    for action in sorted(actions):
        sm = workflows[action].state_machine
        sm.grant_start_execution(fn)
        sm.grant_execution(fn, "states:DescribeExecution")
    fn.add_to_role_policy(
        iam.PolicyStatement(
            sid="ReadOwnKitLedger",
            actions=["dynamodb:GetItem", "dynamodb:Query"],
            resources=[spine.ledger_table.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{kit}#*"]}},
        )
    )
    fn.add_to_role_policy(
        iam.PolicyStatement(
            sid="ResolveTenant",
            actions=["dynamodb:GetItem"],
            resources=[spine.tenants_table.table_arn],
        )
    )
    spine.grant_proposal_outcome_read(fn)  # proposal_id and outcome only
    fn.add_to_role_policy(
        iam.PolicyStatement(
            sid="DecryptTableReads",
            actions=["kms:Decrypt"],
            resources=[spine.kms_key.key_arn],
        )
    )

    schema = agentcore.ToolSchema.from_inline(
        [_cdk_tool_definition(agentcore, t) for t in tool_specs]
    )
    gateway.add_lambda_target(
        f"{kit.upper()}Target",
        gateway_target_name=kit,
        description=f"{KITS[kit]} read and propose tools",
        lambda_function=fn,
        tool_schema=schema,
    )
    _suppress_nag(fn, kit=kit)
    return fn


def _suppress_nag(fn: Any, *, kit: str) -> None:
    from .nag import ACCT, REGION, lambda_basics, regex, suppress

    # Same-stack form <ReaderK1Fn....Arn>:* or, with the tool Lambda in the agent stack, the
    # spine export AwxSpine:ExportsOutputFnGetAttReaderK1Fn...Arn...:*; either names only
    # this kit's reader.
    reader = regex(
        rf"Resource::(<Reader{kit.upper()}Fn[0-9A-F]{{8}}\.Arn>"
        rf"|[A-Za-z0-9]+:ExportsOutputFnGetAttReader{kit.upper()}Fn[0-9A-F]{{8}}Arn[0-9A-F]{{8}}):\*"
    )
    executions = regex(rf"Resource::arn:<AWS::Partition>:states:{REGION}:{ACCT}:execution:.*:\*")
    suppress(
        fn,
        [
            *lambda_basics(fn),
            (
                "AwsSolutions-IAM5",
                "grant_invoke adds the version/alias form <arn>:* of this kit's own reader; "
                "no other reader is invokable.",
                [reader],
            ),
            (
                "AwsSolutions-IAM5",
                "DescribeExecution needs execution:<this kit's state machine>:*; the machine "
                "name is fixed, only the execution name is a wildcard.",
                [executions],
            ),
        ],
    )


def _cdk_schema(agentcore: Any, node: Mapping[str, Any]) -> Any:
    return agentcore.SchemaDefinition(
        type=agentcore.SchemaDefinitionType.of(node["type"]),
        description=node.get("description"),
        properties=(
            {k: _cdk_schema(agentcore, v) for k, v in node["properties"].items()}
            if "properties" in node
            else None
        ),
        required=node.get("required"),
        items=_cdk_schema(agentcore, node["items"]) if "items" in node else None,
    )


def _cdk_tool_definition(agentcore: Any, spec: ToolSpec) -> Any:
    from .schema import tool_definition

    d = tool_definition(spec)
    return agentcore.ToolDefinition(
        name=d["name"],
        description=d["description"],
        input_schema=_cdk_schema(agentcore, d["inputSchema"]),
    )
