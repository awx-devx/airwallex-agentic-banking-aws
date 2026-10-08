"""Kit 4 specific synth checks (marker synth): the declared evidence upload function, built by
KitStack from FUNCTIONS, with exactly its grants. The generic KitStack tests cover the shared
shape."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:  # infra is a repo-root package, as in awx_agent's tests
    sys.path.insert(0, str(_REPO))

pytestmark = pytest.mark.synth


@pytest.fixture(scope="module")
def k4_template():
    from aws_cdk import App

    from infra.app import build_app

    built = build_app(App(), kits=("k4",))
    stack = built.node.find_child("AwxKitK4")
    return next(s for s in built.synth().stacks if s.stack_name == stack.stack_name).template


def _actions(stmts):
    out = set()
    for st in stmts:
        acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        out |= {a.lower() for a in acts}
    return out


def test_one_declared_function_with_its_handler_and_environment(k4_template):
    res = k4_template["Resources"]
    declared = [
        r
        for r in res.values()
        if r["Type"] == "AWS::Lambda::Function"
        and r["Properties"].get("Environment", {}).get("Variables", {}).get("AWX_FUNCTION")
    ]
    (fn,) = declared
    props = fn["Properties"]
    assert props["Handler"] == "awx_k4_dispute.upload.handler"
    assert props["Timeout"] == 60
    assert props["MemorySize"] == 512
    env = props["Environment"]["Variables"]
    assert env["AWX_KIT"] == "k4"
    assert env["AWX_FUNCTION"] == "evidence_upload"
    assert env["AWX_EVIDENCE_PREFIX"] == "evidence/k4/"
    assert {"AWX_ARTIFACTS_BUCKET", "AWX_LEDGER_TABLE"} <= set(env)
    assert "AWX_READER_FUNCTION" not in env  # no OWN_READER grant


def test_the_upload_role_has_exactly_evidence_put_and_kit_store(k4_template):
    res = k4_template["Resources"]
    (role,) = [
        k
        for k, v in res.items()
        if v["Type"] == "AWS::IAM::Role" and k.startswith("FnEvidenceUploadRole")
    ]
    stmts = [
        st
        for v in res.values()
        if v["Type"] == "AWS::IAM::Policy"
        and any(r.get("Ref") == role for r in v["Properties"]["Roles"])
        for st in v["Properties"]["PolicyDocument"]["Statement"]
    ]
    assert _actions(stmts) == {
        "s3:putobject",
        "s3:abortmultipartupload",
        "kms:generatedatakey",
        "kms:decrypt",
        "kms:describekey",
        "dynamodb:getitem",
        "dynamodb:putitem",
        "dynamodb:updateitem",
        "dynamodb:query",
        "xray:puttracesegments",
        "xray:puttelemetryrecords",
        "logs:createlogstream",
        "logs:putlogevents",
    }
    assert all(st["Effect"] == "Allow" for st in stmts)
    (s3,) = [st for st in stmts if "s3:PutObject" in st["Action"]]
    assert s3["Resource"]["Fn::Join"][1][-1] == "/evidence/k4/*"
    (ddb,) = [st for st in stmts if "dynamodb:Query" in st["Action"]]
    assert ddb["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["k4#*"]}}
    # nothing reads evidence back, invokes a Lambda or touches Airwallex credentials
    assert not any(a.startswith(("s3:get", "lambda:", "secretsmanager:")) for a in _actions(stmts))
