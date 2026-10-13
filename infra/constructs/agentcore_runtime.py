"""AgentCore Runtime for the agent plane, as a direct code deployment (no container).

The code asset is staged by awx_agent.staging.stage_runtime: first-party source plus
linux/arm64 CPython 3.12 wheels for strands-agents and bedrock-agentcore. Inbound auth is the
spine user pool's agent app client; `Authorization` is allowlisted so the agent can present the
user's token to the Gateway. The execution role gets Bedrock model invocation and nothing that
reaches a tool, the reader, the adapter or the Airwallex secret.
"""

from __future__ import annotations

from collections.abc import Sequence

from aws_cdk import Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from awx_agent.nag import ACCT, REGION, regex, suppress
from awx_agent.staging import RUNTIME_ENTRY, stage_runtime
from awx_common.regions import foundation_model, is_inference_profile
from constructs import Construct


class AgentCoreRuntime(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        user_pool: cognito.IUserPool,
        client: cognito.IUserPoolClient,
        gateway_url: str,
        model_ids: Sequence[str],
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)

        artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path=str(stage_runtime()),
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
            entrypoint=[RUNTIME_ENTRY],
        )
        self.runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name="awx_agent",
            description="Strands agent for the Airwallex hackathon kits (read and propose only)",
            agent_runtime_artifact=artifact,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(
                user_pool, [client]
            ),
            request_header_configuration=agentcore.RequestHeaderConfiguration(
                allowlisted_headers=["Authorization"]
            ),
            environment_variables={
                "AWX_GATEWAY_URL": gateway_url,
                "AWX_REGION": stack.region,
                "AWX_MODEL_ID": model_ids[0],
                "AWX_FALLBACK_MODEL_ID": model_ids[1] if len(model_ids) > 1 else "",
            },
        )

        # The role invokes inference profiles in the stack Region, and the foundation models
        # behind them in any Region only through those profiles (the bedrock:InferenceProfileArn
        # condition). The profile's destination Regions are AWS's to change, so the grant
        # names none. The trailing * covers a dated or versioned model ID behind a profile; it
        # also matches other model IDs with the same start (anthropic.claude-sonnet-5* matches
        # Claude Sonnet 5.5), and the condition keeps it to the role's own profiles.
        # A plain foundation-model ID (no profile prefix) is granted in the stack Region only.
        invoke = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        profile_ids = [m for m in model_ids if is_inference_profile(m)]
        plain_ids = [m for m in model_ids if not is_inference_profile(m)]
        profiles = [
            f"arn:aws:bedrock:{stack.region}:{stack.account}:inference-profile/{m}"
            for m in profile_ids
        ]
        routed = [
            f"arn:aws:bedrock:*::foundation-model/{foundation_model(m)}*" for m in profile_ids
        ]
        direct = [f"arn:aws:bedrock:{stack.region}::foundation-model/{m}" for m in plain_ids]
        if profiles:
            self.runtime.add_to_role_policy(
                iam.PolicyStatement(sid="InvokeAgentModel", actions=invoke, resources=profiles)
            )
            self.runtime.add_to_role_policy(
                iam.PolicyStatement(
                    sid="InvokeAgentModelThroughProfile",
                    actions=invoke,
                    resources=routed,
                    conditions={"StringEquals": {"bedrock:InferenceProfileArn": profiles}},
                )
            )
        if direct:
            self.runtime.add_to_role_policy(
                iam.PolicyStatement(
                    sid="InvokeAgentFoundationModel", actions=invoke, resources=direct
                )
            )

        log_base = rf"Resource::arn:<AWS::Partition>:logs:{REGION}:{ACCT}:log-group:"
        findings = [
            (
                "AwsSolutions-IAM5",
                "The L2 Runtime's own observability grants: its log groups under "
                "/aws/bedrock-agentcore/runtimes/, DescribeLogGroups (needs log-group:*), "
                "X-Ray (no resource-level permissions) and PutMetricData (conditioned on "
                "the bedrock-agentcore namespace), and its workload identity directory.",
                [
                    "Resource::*",
                    regex(log_base + r"\*"),
                    regex(log_base + r"/aws/bedrock-agentcore/runtimes/\*"),
                    regex(log_base + r"/aws/bedrock-agentcore/runtimes/\*:log-stream:\*"),
                    regex(
                        rf"Resource::arn:<AWS::Partition>:bedrock-agentcore:{REGION}:{ACCT}:"
                        r"workload-identity-directory/default/workload-identity/\*"
                    ),
                ],
            ),
            (
                "AwsSolutions-IAM5",
                "AgentRuntimeArtifact.from_code_asset grants the Runtime read on its own "
                "code asset in the CDK bootstrap assets bucket (s3:GetObject*, "
                "s3:GetBucket*, s3:List*); the bucket is the bootstrap bucket only. The "
                "Action:: entries would also cover the same wildcards on a later statement "
                "of this role, so test_synth pins this role to exactly one S3 statement, "
                "on the bootstrap bucket.",
                [
                    "Action::s3:GetObject*",
                    "Action::s3:GetBucket*",
                    "Action::s3:List*",
                    regex(
                        r"Resource::arn:<AWS::Partition>:s3:::cdk-[a-z0-9]+-assets-"
                        rf"{ACCT}-{REGION}/\*"
                    ),
                ],
            ),
        ]
        if routed:  # an empty appliesTo list would suppress the whole rule
            findings.append(
                (
                    "AwsSolutions-IAM5",
                    "Foundation-model ARNs behind the agent's inference profiles, in any "
                    "Region (the profile picks the destination) and with a trailing * for a "
                    "dated or versioned model ID; the statement's bedrock:InferenceProfileArn "
                    "condition allows them only through the role's own profiles.",
                    [f"Resource::{m}" for m in routed],
                )
            )
        suppress(self.runtime.role, findings)
