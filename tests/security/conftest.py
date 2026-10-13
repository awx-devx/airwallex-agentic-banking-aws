"""Synthesise the CDK app once per session, plus a toy kit stack that exercises KitWorkflow.

The toy kit stands in for the real kit stacks: it uses fan_out, a child spec, a finality
loop and an approval tier, so every state type the construct can emit is synthesised.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
for path in (str(ROOT), str(FIXTURES)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(scope="session")
def synthed():
    import aws_cdk as cdk
    from awx_common.regions import DEFAULT_REGION
    from infra.app import build_app
    from infra.constructs.kit_workflow import KitWorkflow

    app = cdk.App(context={"account": "111122223333", "region": DEFAULT_REGION})
    toy = cdk.Stack(
        app, "AwxToyKit", env=cdk.Environment(account="111122223333", region=DEFAULT_REGION)
    )
    build_app(app, kits=())  # spine + agent stacks and cdk-nag; the toy kit stands in for kits
    spine = app.node.find_child("AwxSpine")
    import awx_toykit.workflow  # a test fixture: tests may run stand-in kit code in-process
    from infra.kit_manifest import parse_workflow

    workflow = KitWorkflow(
        toy,
        "PayAll",
        spine=spine,
        kit="k6",
        action="pay_all",
        spec_module="awx_toykit.workflow",
        workflow=parse_workflow(awx_toykit.workflow.SPECS["pay_all"]),
    )
    # Stands in for the agent plane's tool role: the proposals read grant it will use.
    from aws_cdk import aws_iam as iam

    tool_role = iam.Role(
        toy, "ToolRoleProbe", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
    )
    spine.grant_proposal_outcome_read(tool_role)
    assembly = app.synth()
    return {"app": app, "spine": spine, "toy": toy, "workflow": workflow, "assembly": assembly}
