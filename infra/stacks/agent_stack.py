"""AgentStack: the agent plane on top of SpineStack.

Creates the AgentCore Gateway (Cognito inbound auth, identity interceptor) and the AgentCore
Runtime. Both accept access tokens of the spine's one `awx-agent` app client
(SpineStack.agent_client), the same client the approval API's authorizer accepts, so one CLI
sign-in works everywhere. Kit tools are registered onto `gateway` by the kit stacks with
awx_agent.registry.register_kit_tools.

Uses these SpineStack attributes: user_pool, agent_client, kms_key, approval_api (here) and
reader_for(kit), grant_proposal_outcome_read, ledger_table, tenants_table, proposals_table
(register_kit_tools).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aws_cdk import CfnOutput, Stack
from constructs import Construct

from infra.constructs.agentcore_gateway import AgentCoreGateway
from infra.constructs.agentcore_runtime import AgentCoreRuntime


class AgentStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        spine: Any,
        model_ids: Sequence[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self.spine = spine

        self.client = spine.agent_client

        gw = AgentCoreGateway(
            self,
            "AgentGateway",
            user_pool=spine.user_pool,
            client=self.client,
            kms_key=spine.kms_key,
        )
        self.gateway = gw.gateway
        self.interceptor_fn = gw.interceptor_fn

        rt = AgentCoreRuntime(
            self,
            "AgentRuntime",
            user_pool=spine.user_pool,
            client=self.client,
            gateway_url=self.gateway.gateway_url,
            model_ids=model_ids,
        )
        self.runtime = rt.runtime

        CfnOutput(self, "AwxRegion", value=self.region)
        CfnOutput(self, "AwxUserPoolClientId", value=self.client.user_pool_client_id)
        CfnOutput(self, "AwxRuntimeArn", value=self.runtime.agent_runtime_arn)
        CfnOutput(self, "AwxGatewayUrl", value=self.gateway.gateway_url)
        CfnOutput(self, "AwxApprovalApiUrl", value=spine.approval_api.api_endpoint)
