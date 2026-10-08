"""Lambda building blocks shared by SpineStack and KitWorkflow.

Code: the workspace's own pure-Python packages are copied into a staging directory and used
as the asset. Nothing is pip-installed for spine and kit Lambdas and nothing runs in Docker,
so synth needs no emulation on any host and needs no credentials. (The agent-plane assets add
wheels: see `awx_agent.staging`.) boto3 comes from the Lambda runtime.

Function: Python 3.12 on arm64, an explicit role with no managed policies, a KMS-encrypted
log group with fixed retention, and X-Ray tracing.
"""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
SPINE_PACKAGES: tuple[str, ...] = ("awx_common", "awx_spine", "awx_spine_aws")
LOG_RETENTION = logs.RetentionDays.ONE_MONTH
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".pytest_cache")


def package_dir(name: str) -> Path | None:
    """The source directory of an importable top-level package, or None if not installed."""
    try:
        spec = importlib.util.find_spec(name)
    except ValueError:  # an in-memory stand-in module (tests) has no spec and no files
        return None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))
    return Path(spec.origin) if spec.origin else None


def staging_root() -> Path:
    """This process's private asset staging directory (awx_agent.staging.run_dir), so two
    synths in one checkout never share, delete or rewrite each other's asset directories."""
    from awx_agent.staging import run_dir

    return run_dir("lambda-build")


def staged_code(
    name: str, packages: Sequence[str], *, allow_stand_in: bool = False
) -> lambda_.Code:
    """Copy the named top-level packages into <staging_root()>/<name> and use that.

    Any package that cannot be found on disk raises, because the Lambda would fail on import.
    Only a test that builds a KitWorkflow from an in-memory stand-in spec module passes
    allow_stand_in=True, and even then a SPINE_PACKAGES member must exist.
    """
    out = staging_root() / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for pkg in packages:
        src = package_dir(pkg)
        if src is None:
            if pkg in SPINE_PACKAGES or not allow_stand_in:
                raise ModuleNotFoundError(
                    f"{pkg} is not installed; the {name} Lambda asset would fail on import"
                )
            continue  # an in-memory test stand-in (allow_stand_in=True)
        if src.is_dir():
            shutil.copytree(src, out / pkg, ignore=_IGNORE)
        else:
            shutil.copy2(src, out / src.name)
    return lambda_.Code.from_asset(str(out))


def lambda_role(scope: Construct, id: str, *, description: str) -> iam.Role:
    return iam.Role(
        scope,
        id,
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        description=description,
    )


def spine_function(
    scope: Construct,
    id: str,
    *,
    handler: str,
    code: lambda_.Code,
    role: iam.Role,
    key: kms.IKey,
    environment: Mapping[str, str],
    description: str,
    timeout: Duration | None = None,
    memory_size: int = 512,
) -> lambda_.Function:
    log_group = logs.LogGroup(
        scope,
        f"{id}Logs",
        retention=LOG_RETENTION,
        encryption_key=key,
        removal_policy=RemovalPolicy.DESTROY,
    )
    fn = lambda_.Function(
        scope,
        id,
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.ARM_64,
        handler=handler,
        code=code,
        role=role,
        environment=dict(environment),
        description=description,
        timeout=timeout or Duration.seconds(30),
        memory_size=memory_size,
        log_group=log_group,
        tracing=lambda_.Tracing.ACTIVE,
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"{log_group.log_group_arn}:log-stream:*"],
        )
    )
    NagSuppressions.add_resource_suppressions(
        fn,
        [
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="Decision d2 fixes Python 3.12 for the whole PoC workspace.",
            )
        ],
    )
    NagSuppressions.add_resource_suppressions(
        role,
        [
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason=(
                    "Log streams are created at runtime inside this function's own log group; "
                    "X-Ray PutTraceSegments/PutTelemetryRecords (added by CDK for active "
                    "tracing) and states:SendTaskSuccess have no resource-level permissions."
                ),
                applies_to=[
                    "Resource::*",
                    {"regex": "/^Resource::<.*Logs[0-9A-F]{8}\\.Arn>:log-stream:\\*$/"},
                ],
            )
        ],
        apply_to_children=True,
    )
    return fn


def kms_use(key: kms.IKey, *, write: bool = True) -> iam.PolicyStatement:
    """Use the spine key, listed action by action (no wildcards). A role that only reads
    encrypted data (the secret, a table) needs Decrypt alone; a role that writes encrypted
    DynamoDB items or S3 objects also needs GenerateDataKey."""
    actions = ["kms:Decrypt", "kms:DescribeKey"]
    if write:
        actions += ["kms:GenerateDataKey"]
    return iam.PolicyStatement(actions=actions, resources=[key.key_arn])


def table_access(table_arn: str, actions: Sequence[str]) -> iam.PolicyStatement:
    return iam.PolicyStatement(actions=[f"dynamodb:{a}" for a in actions], resources=[table_arn])


def account_region(scope: Construct) -> tuple[str, str]:
    stack = Stack.of(scope)
    return stack.account, stack.region
