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
from awx_agent.runtime.app import DEFAULT_FALLBACK_MODEL_ID, DEFAULT_MODEL_ID
from awx_agent.staging import RUNTIME_ENTRY, stage_runtime
from constructs import Construct

# Australian cross-Region inference profiles route to these Regions.
AU_INFERENCE_REGIONS = ("ap-southeast-2", "ap-southeast-4")


def _foundation_model(profile_id: str) -> str:
    """au.anthropic.claude-x -> anthropic.claude-x (the model behind a geo profile)."""
    return profile_id.split(".", 1)[1] if profile_id.startswith("au.") else profile_id


class AgentCoreRuntime(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        user_pool: cognito.IUserPool,
        client: cognito.IUserPoolClient,
        gateway_url: str,
        model_ids: Sequence[str] = (DEFAULT_MODEL_ID, DEFAULT_FALLBACK_MODEL_ID),
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

        profiles = [
            f"arn:aws:bedrock:{stack.region}:{stack.account}:inference-profile/{m}"
            for m in model_ids
        ]
        # The trailing * covers a dated or versioned model ID behind the geo profile, so the
        # grant does not depend on the exact foundation-model ID AWS routes the profile to.
        models = [
            f"arn:aws:bedrock:{r}::foundation-model/{_foundation_model(m)}*"
            for m in model_ids
            for r in AU_INFERENCE_REGIONS
        ]
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeAgentModel",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[*profiles, *models],
            )
        )

        log_base = rf"Resource::arn:<AWS::Partition>:logs:{REGION}:{ACCT}:log-group:"
        suppress(
            self.runtime.role,
            [
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
                (
                    "AwsSolutions-IAM5",
                    "Foundation-model ARNs behind the two AU inference profiles; the trailing * "
                    "covers a dated or versioned model ID behind each profile.",
                    [f"Resource::{m}" for m in models if m.endswith("*")],
                ),
            ],
        )
