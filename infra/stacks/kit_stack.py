"""KitStack: everything one kit deploys, built from the kit's own exports.

For kit k<N> (package awx_k<N>_<slug>, directory in awx_common.kits.KIT_DIRS):

- one KitWorkflow per action in `awx_k<N>_<slug>.workflow.SPECS`, in this stack;
- the kit's read_* and propose_* tools registered on the AgentCore Gateway through
  awx_agent.registry.register_kit_tools. The tool Lambda and Gateway target live in the AGENT
  stack, so the dependency graph is only spine <- kit stacks <- agent stack;
- one Lambda per `awx_k<N>_<slug>.functions.FUNCTIONS` entry (awx_common.functions), if the
  kit declares any. KitStack builds each one itself: own role trusted by lambda.amazonaws.com,
  its own log group, the declared grants from the closed KitGrant menu (exact statements
  below) and nothing else; no trigger, URL, alias, permission or resource policy.

Kit code never runs in this process. Everything above is built from the kit's KitManifest,
which infra.kit_manifest produces in a subprocess and validates strictly before any construct
exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from awx_common.functions import KitFunctionSpec, KitGrant
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

from infra.constructs.kit_workflow import KitWorkflow, _camel
from infra.constructs.spine_lambda import (
    SPINE_PACKAGES,
    kms_use,
    lambda_role,
    spine_function,
    staged_code,
)
from infra.kit_manifest import (  # noqa: F401 - re-export
    KitManifest,
    RemovedExtensionHook,
    kit_package,
)

EVIDENCE_PREFIX = "evidence"
# The step roles' ledger actions (kit_workflow.KitWorkflow._step_role), and nothing more.
KIT_STORE_ACTIONS = (
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:Query",
)
EVIDENCE_PUT_ACTIONS = ("s3:PutObject", "s3:AbortMultipartUpload")
# SSE-KMS put: a data key for the object, and Decrypt for multipart uploads' parts.
EVIDENCE_KEY_ACTIONS = ("kms:GenerateDataKey", "kms:Decrypt")


class KitStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        spine: Any,
        agent: Any,
        kit: str,
        manifest: KitManifest,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, **kwargs)
        if not isinstance(manifest, KitManifest) or manifest.kit != kit:
            raise TypeError(f"KitStack({kit}) takes that kit's KitManifest (infra.kit_manifest)")
        if not manifest.stand_in and manifest.package != kit_package(kit):
            raise ValueError(f"KitStack({kit}) manifest names package {manifest.package}")
        from awx_agent.registry import register_kit_tools

        self.kit = kit
        self.spine = spine
        self.manifest = manifest
        package = manifest.package
        self.package = package
        self.spec_module = manifest.spec_module
        self.tools_module = manifest.tools_module
        self.workflows: dict[str, KitWorkflow] = {
            action: KitWorkflow(
                self,
                _camel(action),
                spine=spine,
                kit=kit,
                action=action,
                spec_module=self.spec_module,
                workflow=manifest.workflows[action],
                kit_workflows=manifest.workflows,
            )
            for action in sorted(manifest.workflows)
        }
        self.tool_fn = register_kit_tools(
            agent,
            gateway=agent.gateway,
            spine=spine,
            kit=kit,
            manifest=manifest,
            workflows=self.workflows,
        )
        self.functions: dict[str, lambda_.Function] = {}
        specs_fn = manifest.function_specs()
        if specs_fn:
            code = staged_code(f"{kit}-functions", (*SPINE_PACKAGES, package))
            for spec in specs_fn:
                self.functions[spec.name] = self._function(spec, code)

    # --- declared functions ---------------------------------------------------------------

    def _function(self, spec: KitFunctionSpec, code: lambda_.Code) -> lambda_.Function:
        s = self.spine
        cid = f"Fn{_camel(spec.name)}"
        role = lambda_role(self, f"{cid}Role", description=f"{self.kit} {spec.name} (declared)")
        env: dict[str, str] = {"AWX_KIT": self.kit, "AWX_FUNCTION": spec.name}
        for grant in spec.grants:
            for stmt in self.grant_statements(grant):
                role.add_to_policy(stmt)
            env.update(self._grant_environment(grant))
        fn = spine_function(
            self,
            cid,
            handler=spec.handler,
            code=code,
            role=role,
            key=s.kms_key,
            timeout=Duration.seconds(spec.timeout_s),
            memory_size=spec.memory_mb,
            description=(spec.description or f"{self.kit} {spec.name}")[:250],
            environment=env,
        )
        CfnOutput(self, f"{cid}Name", value=fn.function_name)  # no export_name: not an Export
        if KitGrant.EVIDENCE_PUT in spec.grants:
            NagSuppressions.add_resource_suppressions(
                role,
                [
                    NagPackSuppression(
                        id="AwsSolutions-IAM5",
                        reason=(
                            f"EVIDENCE_PUT: s3:PutObject/AbortMultipartUpload on this kit's own "
                            f"evidence/{self.kit}/ prefix; object keys are known only at run time."
                        ),
                        applies_to=[
                            {
                                "regex": (
                                    # same-stack token or the spine export form
                                    r"/^Resource::(<ArtifactsBucket[0-9A-F]{8}\.Arn>|"
                                    r"[A-Za-z0-9]+:ExportsOutputFnGetAtt"
                                    r"ArtifactsBucket[0-9A-F]{8}Arn[0-9A-F]{8})"
                                    rf"\/{EVIDENCE_PREFIX}\/{self.kit}\/\*$/"
                                )
                            }
                        ],
                    )
                ],
                apply_to_children=True,
            )
        return fn

    def grant_statements(self, grant: KitGrant) -> Sequence[iam.PolicyStatement]:
        """The exact statements each grant maps to. Change only with its exact-pairs test."""
        s, kit = self.spine, self.kit
        if grant is KitGrant.EVIDENCE_PUT:
            return [
                iam.PolicyStatement(
                    actions=list(EVIDENCE_PUT_ACTIONS),
                    resources=[s.artifacts_bucket.arn_for_objects(f"{EVIDENCE_PREFIX}/{kit}/*")],
                ),
                iam.PolicyStatement(
                    actions=list(EVIDENCE_KEY_ACTIONS), resources=[s.kms_key.key_arn]
                ),
            ]
        if grant is KitGrant.KIT_STORE:
            return [
                iam.PolicyStatement(
                    actions=list(KIT_STORE_ACTIONS),
                    resources=[s.ledger_table.table_arn],
                    conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{kit}#*"]}},
                ),
                kms_use(s.kms_key),  # as the step roles: Decrypt, DescribeKey, GenerateDataKey
            ]
        if grant is KitGrant.OWN_READER:
            return [
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=[s.reader_for(kit).function_arn],
                )
            ]
        raise ValueError(f"unknown grant {grant!r}")  # pragma: no cover - closed enum

    def _grant_environment(self, grant: KitGrant) -> dict[str, str]:
        s, kit = self.spine, self.kit
        if grant is KitGrant.EVIDENCE_PUT:
            return {
                "AWX_ARTIFACTS_BUCKET": s.artifacts_bucket.bucket_name,
                "AWX_EVIDENCE_PREFIX": f"{EVIDENCE_PREFIX}/{kit}/",
            }
        if grant is KitGrant.KIT_STORE:
            return {"AWX_LEDGER_TABLE": s.ledger_table.table_name}
        if grant is KitGrant.OWN_READER:
            return {"AWX_READER_FUNCTION": s.reader_for(kit).function_name}
        raise ValueError(f"unknown grant {grant!r}")  # pragma: no cover
