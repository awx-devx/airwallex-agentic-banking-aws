"""SpineStack: the shared, kit-independent half of the PoC.

What it holds and who may touch it:

- one customer-managed KMS key for every table, the bucket, the secret, the topic and logs
- the Airwallex secret, created EMPTY and seeded out of band; readable only by the reader
  and adapter roles
- tables: operations (adapter; no TTL, because Airwallex deduplicates a request_id for only
  about 7 days, so this table is the long-term guard), proposals (kit steps write, approval
  and adapter read),
  decisions (approval Lambda only writes), tenants (server-side tenant map), ledger (kit
  subledgers) and audit (append-only)
- reader Lambda (GET only, per-kit allowlist) and adapter Lambda (writes only). Nothing in
  this stack grants invoke on the adapter; each KitWorkflow grants it to its own state
  machine role and to nothing else
- approval API: HTTP API with a Cognito JWT authorizer and the approval Lambda
- Cognito user pool with `requester` and `approver` groups, an immutable custom:tenant_id
  attribute and a Pre Token Generation V2 trigger that copies it into the access token's
  `tenant_id` claim; the `awx-agent` app client is the one the authorizer and the agent plane accept
- artifacts bucket (KMS, TLS only, no public access, versioned); evidence/<kit>/<tenant>/<id>
  is readable by the adapter role only
- AWS Budgets alarm (USD 100 a month), adapter and reader alarms, log retention, cdk-nag
"""

from __future__ import annotations

import json
import os

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as apigw_auth
from aws_cdk import aws_apigatewayv2_integrations as apigw_int
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

from infra.constructs.spine_lambda import (
    LOG_RETENTION,
    SPINE_PACKAGES,
    kms_use,
    lambda_role,
    spine_function,
    staged_code,
    table_access,
)


def _client_timeout_default() -> float:
    import inspect

    from awx_spine.client import AirwallexClient

    return inspect.signature(AirwallexClient.__init__).parameters["timeout_s"].default


EVIDENCE_PREFIX = "evidence"
CLIENT_TIMEOUT_S = int(_client_timeout_default())  # AirwallexClient's own default, not restated
ADAPTER_TIMEOUT_S = 4 * CLIENT_TIMEOUT_S + 15
MONTHLY_BUDGET_USD = 100
REQUESTER_GROUP = "requester"
APPROVER_GROUP = "approver"
# The agent plane's AgentCore Gateway name (ARN gateway/<name>-<10 lowercase alphanumerics>).
# Kept as the one place the name is defined, so the key grant can later be scoped by
# encryption context.
AGENT_GATEWAY_NAME = "awx-agent-gateway"


def alert_email(scope: Construct) -> str:
    """Where alarms and budget alerts go: `-c alert_email=<address>`, else AWX_ALERT_EMAIL.
    Required, so a deployment never ships alarms that notify nobody."""
    address = scope.node.try_get_context("alert_email") or os.environ.get("AWX_ALERT_EMAIL")
    if not address or "@" not in str(address):
        raise ValueError("no alert email: pass -c alert_email=<address> or set AWX_ALERT_EMAIL")
    return str(address)


class SpineStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        self.alert_email = alert_email(self)
        self.kms_key = self._key()
        self.alarm_topic = self._alarm_topic()
        self.airwallex_secret = self._secret()
        self.operations_table = self._table("Operations", "request_id")
        self.proposals_table = self._table("Proposals", "proposal_id")
        self.proposals_table.add_global_secondary_index(
            index_name="by_tenant",
            partition_key=ddb.Attribute(name="tenant_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )
        self.proposals_table.add_global_secondary_index(  # what a tenant's approvers decide
            index_name="by_approver",
            partition_key=ddb.Attribute(name="approver_tenant", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )
        self.decisions_table = self._table("Decisions", "proposal_id", sort_key="approver")
        self.tenants_table = self._table("Tenants", "tenant_id")
        self.ledger_table = self._table("Ledger", "pk", sort_key="sk")
        self.audit_table = self._table("Audit", "proposal_id", sort_key="at")
        self.artifacts_bucket = self._bucket()
        self.code = staged_code("spine", SPINE_PACKAGES)
        self.user_pool, self.user_pool_client = self._user_pool()
        self.agent_client = self.user_pool_client  # the awx-agent app client (agent plane)

        self._allowlists: dict[str, dict[str, list[str]]] = {}
        self._kit_readers: dict[str, lambda_.Function] = {}
        self.reader_role = self._reader_role()
        self.adapter_role, self.adapter_fn = self._adapter()
        self.approval_role, self.approval_fn, self.approval_api = self._approval_api()
        self._budget()
        self._alarms()
        self._outputs()

    # --- public API for other stacks -----------------------------------------------------

    def register_read_allowlist(
        self, kit: str, patterns: tuple[str, ...], platform_patterns: tuple[str, ...] = ()
    ) -> None:
        """Add a kit's tenant and platform GET allowlists to the reader (KitWorkflow and the
        kit stacks call this)."""
        entry = self._allowlists.setdefault(kit, {"tenant": [], "platform": []})
        for mode, new in (("tenant", patterns), ("platform", platform_patterns)):
            entry[mode].extend(p for p in new if p not in entry[mode])
        self.reader_for(kit).add_environment("AWX_READ_ALLOWLISTS", self._allowlist_json(kit))

    def grant_proposal_outcome_read(self, grantee: iam.IGrantable) -> None:
        """For the agent plane's tool Lambda: GetItem on the proposals table limited to the
        attributes proposal_id and outcome (fine-grained IAM). The task token lives in its own
        attribute, so this grant can never return it."""
        grantee.grant_principal.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[self.proposals_table.table_arn],
                conditions={
                    "ForAllValues:StringEquals": {
                        "dynamodb:Attributes": ["proposal_id", "outcome"]
                    },
                    "StringEqualsIfExists": {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"},
                },
            )
        )
        self.kms_key.grant_decrypt(grantee)

    def reader_for(self, kit: str) -> lambda_.Function:
        """The reader for one kit. Every reader shares one role (reader_role, the only reader
        secret holder) and code; AWX_READER_KIT fixes the kit and AWX_READ_ALLOWLISTS holds
        only that kit's patterns. Grant invoke on it to that kit's callers only (its
        KitWorkflow step role and the agent plane's tool Lambda for that kit); then no caller
        can read under another kit's allowlist. There is no shared reader."""
        if kit not in self._kit_readers:
            fn = spine_function(
                self,
                f"Reader{kit.upper()}Fn",
                handler="awx_spine_aws.handlers.reader.handler",
                code=self.code,
                role=self.reader_role,
                key=self.kms_key,
                description=f"Airwallex reader for {kit} only: GET, derived scope",
                environment={
                    "AWX_SECRET_ARN": self.airwallex_secret.secret_arn,
                    "AWX_TENANTS_TABLE": self.tenants_table.table_name,
                    "AWX_READER_KIT": kit,
                    "AWX_READ_ALLOWLISTS": self._allowlist_json(kit),
                },
            )
            self._reader_alarms(f"Reader{kit.upper()}", fn)
            self._kit_readers[kit] = fn
        return self._kit_readers[kit]

    def _allowlist_json(self, kit: str | None = None) -> str:
        table = self._allowlists if kit is None else {kit: self._allowlists.get(kit, {})}
        return json.dumps(table, sort_keys=True, separators=(",", ":"))

    def spine_environment(self) -> dict[str, str]:
        return {
            "AWX_OPERATIONS_TABLE": self.operations_table.table_name,
            "AWX_PROPOSALS_TABLE": self.proposals_table.table_name,
            "AWX_DECISIONS_TABLE": self.decisions_table.table_name,
            "AWX_TENANTS_TABLE": self.tenants_table.table_name,
            "AWX_LEDGER_TABLE": self.ledger_table.table_name,
            "AWX_AUDIT_TABLE": self.audit_table.table_name,
        }

    # --- resources -----------------------------------------------------------------------

    def _key(self) -> kms.Key:
        key = kms.Key(
            self,
            "SpineKey",
            alias="alias/awx-poc-spine",
            description="Airwallex hackathon PoC: tables, bucket, secret, topic and logs",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                actions=[
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:ReEncryptFrom",
                    "kms:ReEncryptTo",
                    "kms:GenerateDataKey",
                    "kms:DescribeKey",
                ],
                resources=["*"],  # in a key policy "*" means this key
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:aws:logs:{self.region}:{self.account}:log-group:*"
                        )
                    }
                },
            )
        )
        key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=["*"],
                # SourceAccount only: AWS documents no aws:SourceArn on Gateway KMS calls (the
                # documented path is the Gateway's service role, granted through IAM). Any
                # tighter scoping waits on CloudTrail evidence of the Gateway's own KMS calls.
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )
        key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )
        return key

    def _alarm_topic(self) -> sns.Topic:
        topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name="Airwallex PoC alarms",
            master_key=self.kms_key,
            enforce_ssl=True,
        )
        topic.add_subscription(subs.EmailSubscription(self.alert_email))
        return topic

    def _secret(self) -> sm.Secret:
        secret = sm.Secret(
            self,
            "AirwallexSecret",
            description=(
                "Airwallex SANDBOX client_id and api_key as JSON. Created empty; seeded out of "
                "band. Readable only by the reader and adapter Lambdas."
            ),
            encryption_key=self.kms_key,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # The L2 always generates a random value; delete that so the secret is created empty.
        secret.node.default_child.add_property_deletion_override("GenerateSecretString")
        NagSuppressions.add_resource_suppressions(
            secret,
            [
                NagPackSuppression(
                    id="AwsSolutions-SMG4",
                    reason=(
                        "Airwallex API keys are issued and rotated in the Airwallex console, so "
                        "Secrets Manager cannot rotate them; the secret is reseeded by hand."
                    ),
                )
            ],
        )
        return secret

    def _table(self, name: str, partition: str, *, sort_key: str | None = None) -> ddb.Table:
        return ddb.Table(
            self,
            f"{name}Table",
            partition_key=ddb.Attribute(name=partition, type=ddb.AttributeType.STRING),
            sort_key=(
                ddb.Attribute(name=sort_key, type=ddb.AttributeType.STRING) if sort_key else None
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            deletion_protection=False,
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _bucket(self) -> s3.Bucket:
        bucket = s3.Bucket(
            self,
            "ArtifactsBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        NagSuppressions.add_resource_suppressions(
            bucket,
            [
                NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason=(
                        "Evidence and report artifacts for a sandbox PoC; CloudTrail covers "
                        "access and a separate access-log bucket adds cost without a reader."
                    ),
                )
            ],
        )
        return bucket

    def _user_pool(self) -> tuple[cognito.UserPool, cognito.UserPoolClient]:
        pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=True,
            ),
            feature_plan=cognito.FeaturePlan.ESSENTIALS,  # needed for access-token claims
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.NONE,
            custom_attributes={
                # Set by an administrator when the demo user is created; never by the user.
                "tenant_id": cognito.StringAttribute(min_len=1, max_len=64, mutable=False)
            },
            removal_policy=RemovalPolicy.RETAIN,
        )
        for group, text in (
            (REQUESTER_GROUP, "May chat with kits and create proposals"),
            (APPROVER_GROUP, "May approve or reject proposals made by someone else"),
        ):
            cognito.CfnUserPoolGroup(
                self,
                f"{group.capitalize()}Group",
                user_pool_id=pool.user_pool_id,
                group_name=group,
                description=text,
            )
        pre_token = self._pre_token_fn()
        pool.add_trigger(
            cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
            pre_token,
            cognito.LambdaVersion.V2_0,
        )
        client = pool.add_client(
            "AgentClient",
            user_pool_client_name="awx-agent",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            prevent_user_existence_errors=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(1),
            read_attributes=cognito.ClientAttributes()
            .with_standard_attributes(email=True)
            .with_custom_attributes("tenant_id"),
            write_attributes=cognito.ClientAttributes().with_standard_attributes(email=True),
        )
        NagSuppressions.add_resource_suppressions(
            pool,
            [
                NagPackSuppression(
                    id="AwsSolutions-COG2",
                    reason=(
                        "Two administrator-created demo users in a sandbox; TOTP MFA is "
                        "available (OPTIONAL) and required before any non-sandbox use."
                    ),
                ),
                NagPackSuppression(
                    id="AwsSolutions-COG8",
                    reason=(
                        "The Plus feature plan adds per-user cost for threat protection that "
                        "two administrator-created sandbox demo users do not need."
                    ),
                ),
                NagPackSuppression(
                    id="AwsSolutions-COG3",
                    reason=(
                        "Threat protection is a paid feature plan; self sign-up is disabled "
                        "and only two demo users exist."
                    ),
                ),
            ],
        )
        return pool, client

    def _pre_token_fn(self) -> lambda_.Function:
        role = lambda_role(self, "PreTokenRole", description="Adds tenant_id to access tokens")
        return spine_function(
            self,
            "PreTokenFn",
            handler="awx_spine_aws.handlers.pre_token.handler",
            code=self.code,
            role=role,
            key=self.kms_key,
            timeout=Duration.seconds(5),
            memory_size=256,
            description="Cognito Pre Token Generation V2: tenant_id claim on the access token",
            environment={},
        )

    def _reader_role(self) -> iam.Role:
        """The one role every per-kit reader (reader_for) runs as."""
        role = lambda_role(self, "ReaderRole", description="Airwallex GET reader (holds secret)")
        role.add_to_policy(self._secret_read())
        role.add_to_policy(kms_use(self.kms_key, write=False))
        role.add_to_policy(table_access(self.tenants_table.table_arn, ["GetItem"]))
        return role

    def _adapter(self) -> tuple[iam.Role, lambda_.Function]:
        role = lambda_role(self, "AdapterRole", description="Airwallex mutation adapter")
        role.add_to_policy(self._secret_read())
        role.add_to_policy(kms_use(self.kms_key))
        role.add_to_policy(
            table_access(self.operations_table.table_arn, ["GetItem", "PutItem", "UpdateItem"])
        )
        role.add_to_policy(table_access(self.proposals_table.table_arn, ["GetItem"]))
        role.add_to_policy(table_access(self.decisions_table.table_arn, ["Query"]))
        role.add_to_policy(table_access(self.tenants_table.table_arn, ["GetItem"]))
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[self.artifacts_bucket.arn_for_objects(f"{EVIDENCE_PREFIX}/*")],
            )
        )
        NagSuppressions.add_resource_suppressions(
            role,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Evidence object keys are evidence/<kit>/<tenant>/<id> and are only "
                        "known at run time; read-only, limited to the evidence/ prefix."
                    ),
                    applies_to=[{"regex": "/^Resource::<ArtifactsBucket.*\\.Arn>/evidence/\\*$/"}],
                )
            ],
            apply_to_children=True,
        )
        fn = spine_function(
            self,
            "AdapterFn",
            handler="awx_spine_aws.handlers.adapter.handler",
            code=self.code,
            role=role,
            key=self.kms_key,
            # One send per call (max_attempts=1): a read-back, the send and a read-back after
            # an unknown outcome, each bounded by the client's 30 s timeout, plus the login.
            timeout=Duration.seconds(ADAPTER_TIMEOUT_S),
            description="Airwallex mutation adapter: the only path to a write endpoint",
            environment={
                "AWX_SECRET_ARN": self.airwallex_secret.secret_arn,
                "AWX_ARTIFACTS_BUCKET": self.artifacts_bucket.bucket_name,
                **self.spine_environment(),
            },
        )
        return role, fn

    def _secret_read(self) -> iam.PolicyStatement:
        return iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[self.airwallex_secret.secret_arn],
        )

    def _approval_api(self) -> tuple[iam.Role, lambda_.Function, apigw.HttpApi]:
        role = lambda_role(self, "ApprovalRole", description="Approval API callback")
        role.add_to_policy(kms_use(self.kms_key))
        role.add_to_policy(table_access(self.proposals_table.table_arn, ["GetItem"]))
        for index in ("by_tenant", "by_approver"):
            role.add_to_policy(
                table_access(f"{self.proposals_table.table_arn}/index/{index}", ["Query"])
            )
        role.add_to_policy(table_access(self.decisions_table.table_arn, ["PutItem", "Query"]))
        task_token = iam.PolicyStatement(
            actions=["states:SendTaskSuccess"],
            resources=["*"],  # task-token APIs support no resource-level permissions
        )
        role.add_to_policy(task_token)
        fn = spine_function(
            self,
            "ApprovalFn",
            handler="awx_spine_aws.handlers.approval.handler",
            code=self.code,
            role=role,
            key=self.kms_key,
            description="Approval API: binds a decision to the proposal hash, then sends the token",
            environment=self.spine_environment(),
        )
        access_logs = logs.LogGroup(
            self,
            "ApprovalApiAccessLogs",
            retention=LOG_RETENTION,
            encryption_key=self.kms_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        api = apigw.HttpApi(
            self,
            "ApprovalApi",
            description="Approve or reject Airwallex PoC proposals",
            create_default_stage=True,
        )
        stage = api.default_stage.node.default_child
        stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=json.dumps(
                {
                    "requestId": "$context.requestId",
                    "routeKey": "$context.routeKey",
                    "status": "$context.status",
                    "sub": "$context.authorizer.claims.sub",
                    "ip": "$context.identity.sourceIp",
                    "time": "$context.requestTime",
                }
            ),
        )
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=10, throttling_rate_limit=5
        )
        # Cognito access tokens carry client_id, not aud, so the authorizer checks client_id.
        authorizer = apigw_auth.HttpUserPoolAuthorizer(
            "CognitoAuthorizer",
            self.user_pool,
            user_pool_clients=[self.user_pool_client],
        )
        integration = apigw_int.HttpLambdaIntegration("ApprovalIntegration", fn)
        for path, method in (
            ("/proposals", apigw.HttpMethod.GET),
            ("/proposals/{proposal_id}", apigw.HttpMethod.GET),
            ("/proposals/{proposal_id}/decision", apigw.HttpMethod.POST),
        ):
            api.add_routes(
                path=path, methods=[method], integration=integration, authorizer=authorizer
            )
        return role, fn, api

    def _budget(self) -> None:
        def notify(
            kind: str, threshold: int
        ) -> budgets.CfnBudget.NotificationWithSubscribersProperty:
            return budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type=kind,
                    comparison_operator="GREATER_THAN",
                    threshold=threshold,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[
                    budgets.CfnBudget.SubscriberProperty(
                        subscription_type="EMAIL", address=self.alert_email
                    )
                ],
            )

        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="awx-poc-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=MONTHLY_BUDGET_USD, unit="USD"),
            ),
            notifications_with_subscribers=[
                notify("ACTUAL", 50),
                notify("ACTUAL", 100),
                notify("FORECASTED", 100),
            ],
        )

    def _alarms(self) -> None:
        self._reader_alarms("Adapter", self.adapter_fn)  # each per-kit reader adds its own

    def _reader_alarms(self, name: str, fn: lambda_.Function) -> None:
        action = cw_actions.SnsAction(self.alarm_topic)
        for metric_name, metric in (
            ("Errors", fn.metric_errors(period=Duration.minutes(5))),
            ("Throttles", fn.metric_throttles(period=Duration.minutes(5))),
        ):
            alarm = cw.Alarm(
                self,
                f"{name}{metric_name}Alarm",
                alarm_description=f"{name} Lambda {metric_name.lower()} (Airwallex PoC)",
                metric=metric,
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(action)

    def _outputs(self) -> None:
        CfnOutput(self, "ApprovalApiUrl", value=self.approval_api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "ArtifactsBucketName", value=self.artifacts_bucket.bucket_name)
        CfnOutput(self, "AirwallexSecretArn", value=self.airwallex_secret.secret_arn)
        CfnOutput(self, "TenantsTableName", value=self.tenants_table.table_name)
