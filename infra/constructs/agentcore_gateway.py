"""AgentCore Gateway for the agent plane: Cognito JWT inbound auth plus the identity interceptor.

The Gateway accepts only access tokens issued to the agent app client of the spine user pool.
Its REQUEST interceptor writes the verified caller into every tools/call (see
awx_agent.handlers.interceptor), because Lambda targets get no caller identity of their own.
Kit tool targets are added later with awx_agent.registry.register_kit_tools.

Do not add an `__init__.py` to infra/constructs: a regular package named `constructs` would shadow
the jsii `constructs` library. Import this module as `infra.constructs.agentcore_gateway`.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from awx_agent.nag import lambda_basics, regex, suppress
from awx_agent.staging import first_party_dirs, stage_lambda_source
from constructs import Construct

from infra.stacks.spine_stack import AGENT_GATEWAY_NAME


class AgentCoreGateway(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        user_pool: cognito.IUserPool,
        client: cognito.IUserPoolClient,
        kms_key: kms.IKey,
    ) -> None:
        super().__init__(scope, id)
        region = Stack.of(self).region
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool.user_pool_id}"

        self.interceptor_fn = lambda_.Function(
            self,
            "Interceptor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="awx_agent.handlers.interceptor.handler",
            code=lambda_.Code.from_asset(
                str(stage_lambda_source("gateway-interceptor", first_party_dirs()))
            ),
            timeout=Duration.seconds(10),
            memory_size=256,
            description="Writes the verified caller into AgentCore Gateway tools/call requests",
            environment={
                "AWX_EXPECTED_ISSUER": issuer,
                "AWX_EXPECTED_CLIENT_ID": client.user_pool_client_id,
            },
            log_group=logs.LogGroup(
                self,
                "InterceptorLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,  # never orphaned
                encryption_key=kms_key,
            ),
        )

        self.gateway = agentcore.Gateway(
            self,
            "Gateway",
            gateway_name=AGENT_GATEWAY_NAME,  # pinned in the spine key policy (SourceArn)
            description="read_* and propose_* tools for the Airwallex hackathon kits",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_cognito(
                user_pool=user_pool, allowed_clients=[client]
            ),
            protocol_configuration=agentcore.GatewayProtocol.mcp(
                instructions="Airwallex hackathon kit tools. Read and propose only."
            ),
            interceptor_configurations=[
                agentcore.LambdaInterceptor.for_request(
                    self.interceptor_fn, pass_request_headers=True
                )
            ],
            kms_key=kms_key,
        )

        # The Gateway encrypts with the spine key at create time, through its service role's
        # DefaultPolicy; the L2 adds no DependsOn, so CloudFormation may create the Gateway
        # first, and a real deployment's create then failed with "not authorized to perform:
        # kms:GenerateDataKey".
        policy = self.gateway.role.node.try_find_child("DefaultPolicy")
        if policy is not None:
            self.gateway.node.default_child.add_dependency(policy.node.default_child)

        suppress(self.interceptor_fn, lambda_basics(self.interceptor_fn))
        suppress(
            self.gateway.role,
            [
                (
                    "AwsSolutions-IAM5",
                    "kms:GenerateDataKey* and kms:ReEncrypt* come from the KMS grant for the "
                    "spine key the Gateway encrypts with; scoped to that one key.",
                    ["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"],
                ),
                (
                    "AwsSolutions-IAM5",
                    "The Gateway invokes the interceptor and each kit tool Lambda; <arn>:* is "
                    "CDK's version/alias form of those exact functions.",
                    [
                        regex(r"Resource::<AgentGatewayInterceptor[0-9A-F]{8}\.Arn>:\*"),
                        regex(r"Resource::<K[1-8]ToolsHandler[0-9A-F]{8}\.Arn>:\*"),
                    ],
                ),
            ],
        )
