"""KitStack and the full app topology: every kit deploys through one generic stack."""

from __future__ import annotations

import importlib.machinery
import json
import sys
import types

import pytest
from awx_common.kits import KITS

pytestmark = pytest.mark.synth

STACK_LIMIT = 500  # CloudFormation's resources-per-stack quota
HEADROOM = 300  # fail well before the quota so a kit can grow


@pytest.fixture(scope="module")
def app():
    import os

    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    from aws_cdk import App

    from infra.app import build_app

    built = build_app(App())
    return built, built.synth()


def _stacks(app):
    _, assembly = app
    return {s.stack_name: s for s in assembly.stacks}


def _deps(stack) -> set[str]:
    return {d.id for d in stack.dependencies if not d.id.endswith(".assets")}


def test_one_kit_stack_per_kit_and_the_dependency_order_is_spine_kits_agent(app):
    stacks = _stacks(app)
    kit_stacks = {f"AwxKit{k.upper()}" for k in KITS}
    assert set(stacks) == {"AwxSpine", "AwxAgentStack", *kit_stacks}
    assert _deps(stacks["AwxSpine"]) == set()
    for name in kit_stacks:
        assert _deps(stacks[name]) == {"AwxSpine"}, name  # never kit -> agent, kit -> kit
    assert _deps(stacks["AwxAgentStack"]) == {"AwxSpine", *kit_stacks}


def test_the_stack_graph_has_no_cycle(app):
    graph = {n: _deps(s) for n, s in _stacks(app).items()}
    state: dict[str, int] = {}

    def visit(n, path):
        if state.get(n) == 1:
            raise AssertionError(f"cycle: {' -> '.join([*path, n])}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in graph[n]:
            visit(d, [*path, n])
        state[n] = 2

    for n in graph:
        visit(n, [])


def test_every_stack_stays_well_under_the_resource_quota(app, capsys):
    counts = {n: len(s.template["Resources"]) for n, s in _stacks(app).items()}
    with capsys.disabled():
        print("\nresource counts:", json.dumps(counts, sort_keys=True))
    for name, n in counts.items():
        assert n < HEADROOM < STACK_LIMIT, (name, n)


def test_every_kit_has_one_state_machine_per_action_and_one_gateway_target(app):
    import importlib

    from infra.stacks.kit_stack import kit_package

    stacks = _stacks(app)
    targets = {
        r["Properties"]["Name"]
        for r in stacks["AwxAgentStack"].template["Resources"].values()
        if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
    }
    assert targets == set(KITS)
    for kit in KITS:
        specs = importlib.import_module(f"{kit_package(kit)}.workflow").SPECS
        template = stacks[f"AwxKit{kit.upper()}"].template["Resources"]
        roots = [
            r
            for lid, r in template.items()
            if r["Type"] == "AWS::StepFunctions::StateMachine" and "RootMachine" in lid
        ]
        assert len(roots) == len(specs), (kit, len(roots), sorted(specs))


def test_each_gateway_target_carries_exactly_its_kits_tools(app):
    """The tool names in every target's inline payload equal the kit's TOOLS."""
    import importlib

    from infra.stacks.kit_stack import kit_package

    agent = _stacks(app)["AwxAgentStack"].template["Resources"]
    payloads = {
        r["Properties"]["Name"]: r["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"][
            "ToolSchema"
        ]["InlinePayload"]
        for r in agent.values()
        if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
    }
    for kit in KITS:
        tools = importlib.import_module(f"{kit_package(kit)}.tools").TOOLS
        assert {t["Name"] for t in payloads[kit]} == {t.name for t in tools}, kit


def test_cdk_nag_has_no_unsuppressed_finding_in_any_stack(app):
    from aws_cdk.assertions import Annotations, Match

    built, _ = app
    for stack in built.node.children:
        if not hasattr(stack, "stack_name"):
            continue
        errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp(".*"))
        assert errors == [], (stack.stack_name, [e.entry.data for e in errors][:3])


# --- exact pairs, resolved from stack attributes ------------------------------------------
# Every role KitStack and the spine build is pinned to an exact (action, resource, condition)
# set. Resources are symbols resolved from SpineStack / KitStack / AgentStack ATTRIBUTES
# (role_check.export_symbols for imports, local_symbols per stack), never logical-id
# substrings. Actions compare lower-case.

NONE = "{}"
XRAY = {("xray:puttelemetryrecords", "*", NONE), ("xray:puttracesegments", "*", NONE)}
BASIC = ("managed-policy", "AWSLambdaBasicExecutionRole", NONE)


def _ledger_cond(kit):
    return json.dumps(
        {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{kit}#*"]}}, sort_keys=True
    )


PROPOSAL_OUTCOME_COND = json.dumps(
    {
        "ForAllValues:StringEquals": {"dynamodb:Attributes": ["proposal_id", "outcome"]},
        "StringEqualsIfExists": {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"},
    },
    sort_keys=True,
)


def _logs(symbol):
    return {
        ("logs:createlogstream", "{" + symbol + "}:log-stream:*", NONE),
        ("logs:putlogevents", "{" + symbol + "}:log-stream:*", NONE),
    }


def symbols_by_stack(built):
    """{stack name: {export name or local json: symbol}} from the stacks' attributes."""
    from role_check import export_symbols, local_symbols

    templates = {s.stack_name: s.template for s in built.synth().stacks}
    spine = built.node.find_child("AwxSpine")
    spine_attrs = {
        "ledger": spine.ledger_table.table_arn,
        "tenants": spine.tenants_table.table_arn,
        "proposals": spine.proposals_table.table_arn,
        "decisions": spine.decisions_table.table_arn,
        "audit": spine.audit_table.table_arn,
        "operations": spine.operations_table.table_arn,
        "key": spine.kms_key.key_arn,
        "bucket": spine.artifacts_bucket.bucket_arn,
        "secret": spine.airwallex_secret.secret_arn,
        "approval_role_name": spine.approval_role.role_name,
        "logs_adapter": spine.adapter_fn.log_group.log_group_arn,
        "logs_approval": spine.approval_fn.log_group.log_group_arn,
    }
    for k, fn in spine._kit_readers.items():
        spine_attrs[f"reader_{k}"] = fn.function_arn
        spine_attrs[f"logs_reader_{k}"] = fn.log_group.log_group_arn
    exports = export_symbols(spine, templates["AwxSpine"], spine_attrs)
    local = {"AwxSpine": local_symbols(spine, spine_attrs)}
    for ks in [c for c in built.node.children if getattr(c, "kit", None)]:
        attrs = {}
        for a, w in ks.workflows.items():
            attrs[f"sm_{ks.kit}_{a}"] = w.state_machine.state_machine_arn
            attrs[f"smname_{ks.kit}_{a}"] = w.state_machine.state_machine_name
            attrs[f"logs_step_{ks.kit}_{a}"] = w.step_fn.log_group.log_group_arn
        for name, fn in ks.functions.items():
            attrs[f"logs_fn_{ks.kit}_{name}"] = fn.log_group.log_group_arn
        exports.update(export_symbols(ks, templates[ks.stack_name], attrs))
        local[ks.stack_name] = local_symbols(ks, attrs)
    return {name: {**exports, **local.get(name, {})} for name in templates}


@pytest.fixture(scope="module")
def symbols(app):
    built, _ = app
    return symbols_by_stack(built)


def _tool_expected(kit, actions, stack):
    reader = "{reader_" + kit + "}"
    out = {
        BASIC,
        ("dynamodb:getitem", "{ledger}", _ledger_cond(kit)),
        ("dynamodb:query", "{ledger}", _ledger_cond(kit)),
        ("dynamodb:getitem", "{proposals}", PROPOSAL_OUTCOME_COND),
        ("dynamodb:getitem", "{tenants}", NONE),
        ("kms:decrypt", "{key}", NONE),
        ("lambda:invokefunction", reader, NONE),
        ("lambda:invokefunction", reader + ":*", NONE),
    }
    for a in actions:
        sm = "{sm_" + kit + "_" + a + "}"
        out.add(("states:startexecution", sm, NONE))
        out.add(
            (
                "states:describeexecution",
                f"arn:${{Partition}}:states:{stack.region}:{stack.account}:execution:"
                f"sel(6,split(:,{sm})):*",
                NONE,
            )
        )
    return out


def _step_expected(kit, action, stack):
    lc = _ledger_cond(kit)
    return {
        # run_status describes this kit's root executions only, every action's
        # (awx-<kit>-<action>), never another kit's. Exactly this pair, nothing wider.
        (
            "states:describeexecution",
            f"arn:${{Partition}}:states:{stack.region}:{stack.account}:execution:awx-{kit}-*:*",
            NONE,
        ),
        *(("dynamodb:" + a, "{ledger}", lc) for a in ("getitem", "putitem", "updateitem", "query")),
        ("dynamodb:getitem", "{proposals}", NONE),
        ("dynamodb:putitem", "{proposals}", NONE),
        ("dynamodb:updateitem", "{proposals}", NONE),
        ("dynamodb:query", "{decisions}", NONE),
        ("dynamodb:getitem", "{tenants}", NONE),
        ("dynamodb:putitem", "{audit}", NONE),
        ("kms:decrypt", "{key}", NONE),
        ("kms:describekey", "{key}", NONE),
        ("kms:generatedatakey", "{key}", NONE),
        ("lambda:invokefunction", "{reader_" + kit + "}", NONE),
        *_logs(f"logs_step_{kit}_{action}"),
        *XRAY,
    }


def _assert_exact(actual, expected):
    from role_check import check_exact

    check_exact(actual, expected)
    assert {(a, r) for a, r, _ in actual} == {(a, r) for a, r, _ in expected}


@pytest.mark.parametrize("kit", sorted(KITS))
def test_kit_tool_role_holds_exactly_its_pairs(app, symbols, kit):
    from role_check import pairs, role_statements

    built, _ = app
    ks = built.node.find_child(f"AwxKit{kit.upper()}")
    agent = built.node.find_child("AwxAgentStack")
    sym = symbols["AwxAgentStack"]
    template = _stacks(app)["AwxAgentStack"].template
    actual = pairs(role_statements(template, f"{kit.upper()}ToolsHandlerServiceRole", sym), sym)
    _assert_exact(actual, _tool_expected(kit, sorted(ks.workflows), agent))


def test_gateway_interceptor_role_holds_exactly_basic_execution(app, symbols):
    from role_check import pairs, role_statements

    sym = symbols["AwxAgentStack"]
    template = _stacks(app)["AwxAgentStack"].template
    actual = pairs(role_statements(template, "AgentGatewayInterceptorServiceRole", sym), sym)
    _assert_exact(actual, {BASIC})


@pytest.mark.parametrize("kit", sorted(KITS))
def test_kit_step_roles_hold_exactly_their_pairs(app, symbols, kit):
    from role_check import pairs, role_statements

    from infra.constructs.kit_workflow import _camel

    built, _ = app
    ks = built.node.find_child(f"AwxKit{kit.upper()}")
    sym = symbols[ks.stack_name]
    template = _stacks(app)[ks.stack_name].template
    for action in ks.workflows:
        actual = pairs(role_statements(template, f"{_camel(action)}StepRole", sym), sym)
        _assert_exact(actual, _step_expected(kit, action, ks))


def test_spine_reader_adapter_and_approval_roles_hold_exactly_their_pairs_in_the_full_app(
    app, symbols
):
    """Pinned in the FULL app (every kit stack built), not kits=()."""
    from role_check import pairs, role_statements

    built, _ = app
    sym = symbols["AwxSpine"]
    spine_t = _stacks(app)["AwxSpine"].template
    reader = pairs(role_statements(spine_t, "ReaderRole", sym), sym)
    expected_reader = {
        ("dynamodb:getitem", "{tenants}", NONE),
        ("kms:decrypt", "{key}", NONE),
        ("kms:describekey", "{key}", NONE),
        ("secretsmanager:getsecretvalue", "{secret}", NONE),
        *XRAY,
    }
    for k in KITS:
        expected_reader |= _logs(f"logs_reader_{k}")
    _assert_exact(reader, expected_reader)

    adapter = pairs(role_statements(spine_t, "AdapterRole", sym), sym)
    _assert_exact(
        adapter,
        {
            ("dynamodb:getitem", "{operations}", NONE),
            ("dynamodb:putitem", "{operations}", NONE),
            ("dynamodb:updateitem", "{operations}", NONE),
            ("dynamodb:getitem", "{proposals}", NONE),
            ("dynamodb:getitem", "{tenants}", NONE),
            ("dynamodb:query", "{decisions}", NONE),
            ("kms:decrypt", "{key}", NONE),
            ("kms:describekey", "{key}", NONE),
            ("kms:generatedatakey", "{key}", NONE),
            ("s3:getobject", "{bucket}/evidence/*", NONE),
            ("secretsmanager:getsecretvalue", "{secret}", NONE),
            *_logs("logs_adapter"),
            *XRAY,
        },
    )

    # The approval role, across every stack (kit stacks attach ApprovalDescribe to it).
    approval = set()
    for name, stack in _stacks(app).items():
        stmts = role_statements(
            stack.template,
            "ApprovalRole" if name == "AwxSpine" else None,
            symbols[name],
            imported_role="approval_role_name",
        )
        approval |= pairs(stmts, symbols[name], star_ok=("states:sendtasksuccess",))
    from awx_common.regions import DEFAULT_REGION

    region, account = DEFAULT_REGION, "111122223333"
    expected_approval = {
        ("dynamodb:putitem", "{decisions}", NONE),
        ("dynamodb:query", "{decisions}", NONE),
        ("dynamodb:getitem", "{proposals}", NONE),
        ("dynamodb:query", "{proposals}/index/by_tenant", NONE),
        ("dynamodb:query", "{proposals}/index/by_approver", NONE),
        ("kms:decrypt", "{key}", NONE),
        ("kms:describekey", "{key}", NONE),
        ("kms:generatedatakey", "{key}", NONE),
        ("states:sendtasksuccess", "*", NONE),
        *_logs("logs_approval"),
        *XRAY,
    }
    for ks in [c for c in built.node.children if getattr(c, "kit", None)]:
        for a in ks.workflows:
            expected_approval.add(
                (
                    "states:describeexecution",
                    f"arn:${{Partition}}:states:{region}:{account}:execution:"
                    "{smname_" + f"{ks.kit}_{a}" + "}:*",
                    NONE,
                )
            )
    _assert_exact(approval, expected_approval)


def test_role_check_refuses_wildcards_literal_arns_case_variants_and_unknown_imports(app, symbols):
    """The checker itself: each probe shape (wildcard, literal ARN, case variant, unknown
    import) is refused, and so is every DescribeExecution wider than this kit's own root
    machines."""
    from role_check import check_exact, pairs

    built, _ = app
    ks = built.node.find_child("AwxKitK4")
    sym = symbols["AwxKitK4"]
    allowed = _step_expected("k4", "accept_dispute", ks)

    def execs(tail):
        return {
            "Action": "states:DescribeExecution",
            "Resource": {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        f":states:{ks.region}:{ks.account}:execution:{tail}",
                    ],
                ]
            },
        }

    wider = [execs("*"), execs("awx-*:*"), execs("awx-k1-*:*"), execs("awx-k4*:*")]
    for probe in wider:
        with pytest.raises(AssertionError):
            check_exact(pairs([probe], sym), allowed)
    check_exact(pairs([execs("awx-k4-*:*")], sym), allowed)  # the pinned pair itself passes
    probes = [
        {"Action": ["dynamodb:Query"], "Resource": "arn:aws:dynamodb:ap-southeast-2:1:table/*"},
        {"Action": "states:StartExecution", "Resource": "arn:aws:states:x:1:stateMachine:*"},
        {"Action": "lambda:InvokeFunction", "Resource": "arn:aws:lambda:x:1:function:*"},
        {"Action": "SecretsManager:GetSecretValue", "Resource": "*"},
        {"Action": "IAM:PassRole", "Resource": {"Fn::ImportValue": "AwxSpine:Unknown"}},
        {
            "Action": "DYNAMODB:PutItem",
            "Resource": {"Fn::ImportValue": next(n for n, s in sym.items() if s == "ledger")},
        },  # ledger without the condition
    ]
    for probe in probes:
        with pytest.raises(AssertionError):
            check_exact(pairs([probe], sym), allowed)


def _role_template(role_props, *extra):
    resources = {
        "R": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {"Statement": []},
                **role_props,
            },
        }
    }
    for lid, res in extra:
        resources[lid] = res
    return {"Resources": resources}


ADMIN = {"Effect": "Allow", "Action": "*", "Resource": "*"}


@pytest.mark.parametrize(
    "template",
    [
        _role_template({"ManagedPolicyArns": ["arn:aws:iam::aws:policy/AdministratorAccess"]}),
        _role_template(
            {},
            (
                "P",
                {
                    "Type": "AWS::IAM::Policy",
                    "Properties": {
                        "Roles": ["literal-role-name"],
                        "PolicyDocument": {"Statement": [ADMIN]},
                    },
                },
            ),
        ),
        _role_template(
            {"ManagedPolicyArns": [{"Ref": "M"}]},
            (
                "M",
                {
                    "Type": "AWS::IAM::ManagedPolicy",
                    "Properties": {"PolicyDocument": {"Statement": [ADMIN]}},
                },
            ),
        ),
        _role_template(
            {},
            (
                "RP",
                {
                    "Type": "AWS::IAM::RolePolicy",
                    "Properties": {
                        "RoleName": "literal-role-name",
                        "PolicyName": "x",
                        "PolicyDocument": {"Statement": [ADMIN]},
                    },
                },
            ),
        ),
    ],
    ids=["managed-admin", "policy-literal-role", "local-managed-policy-ref", "rolepolicy-literal"],
)
def test_role_check_fails_closed_on_managed_policies_and_non_ref_attachments(template):
    """Each raw case is a refusal (AssertionError), never a pass and never an
    AttributeError."""
    from role_check import pairs, role_statements

    with pytest.raises(AssertionError):
        pairs(role_statements(template, "R", {}), {})


def test_runtime_role_bootstrap_s3_statement_is_exactly_pinned(app):
    """One S3 statement on the Runtime role, on the bootstrap bucket, with
    exactly CDK's read actions, so no wildcard can be merged into it unseen."""
    from role_check import role_statements

    stmts = role_statements(_stacks(app)["AwxAgentStack"].template, "AgentRuntimeExecutionRole")
    s3 = [s for s in stmts if any(str(a).lower().startswith("s3:") for a in _actions(s))]
    assert len(s3) == 1, s3
    assert sorted(_actions(s3[0])) == ["s3:GetBucket*", "s3:GetObject*", "s3:List*"]
    assert "cdk-hnb659fds-assets-" in json.dumps(s3[0]["Resource"])
    assert "Condition" not in s3[0]


def _actions(stmt):
    a = stmt["Action"]
    return a if isinstance(a, list) else [a]


# --- kit code never runs in the CDK process ------------------------------------------------

KIT_STACK_RESOURCE_TYPES = {
    "AWS::CloudWatch::Alarm",
    "AWS::IAM::Policy",
    "AWS::IAM::Role",
    "AWS::Lambda::Function",
    "AWS::Logs::LogGroup",
    "AWS::StepFunctions::StateMachine",
}
REPO = __import__("pathlib").Path(__file__).resolve().parents[3]


def test_a_full_build_app_in_a_fresh_interpreter_imports_no_kit_module():
    import subprocess

    code = (
        "import os, sys; os.environ['AWX_SKIP_RUNTIME_DEPS'] = '1'\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from aws_cdk import App\n"
        "from infra.app import build_app\n"
        "build_app(App()).synth()\n"
        "bad = sorted(m for m in sys.modules if m.startswith('awx_k'))\n"
        "print('KITMODS', bad)\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO), check=False
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "KITMODS []" in proc.stdout, proc.stdout[-400:]


def test_the_eight_real_kits_manifests_load_and_validate():
    from awx_common.kits import KIT_DIRS

    from infra.kit_manifest import _run_loader, load_manifests, parse_manifest

    manifests = load_manifests(sorted(KITS))
    assert set(manifests) == set(KITS)
    for kit, m in manifests.items():
        assert m.package == "awx_" + KIT_DIRS[kit].rsplit("/", 1)[1]
        assert m.workflows
        assert m.tools
        assert parse_manifest(_run_loader(kit, ())) == m  # deterministic


def _overlay(tmp_path, files):
    """A copy of the real awx_k4_dispute package with extra or replaced files, put first on the
    loader subprocess's path. The CDK process never imports it."""
    import shutil

    dest = tmp_path / "overlay"
    shutil.copytree(
        REPO / "kits" / "k4_dispute" / "src" / "awx_k4_dispute",
        dest / "awx_k4_dispute",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name, text in files.items():
        path = dest / "awx_k4_dispute" / name
        path.write_text(text if name != "__init__.py" else path.read_text() + text)
    return str(dest)


PROBES = {
    # Widen the spine ReaderRole at import time, no aws_cdk import.
    "gc-reader-role": (
        "import gc\n"
        "from infra.stacks.kit_stack import iam\n"
        "from infra.stacks.spine_stack import SpineStack\n"
        "for o in gc.get_objects():\n"
        "    if isinstance(o, SpineStack):\n"
        "        o.reader_role.add_to_policy(iam.PolicyStatement(actions=['s3:*'], "
        "resources=['*']))\n"
    ),
    # ... or patch KitStack's grant map
    "patch-grant-map": (
        "from infra.stacks import kit_stack\n"
        "from aws_cdk import aws_iam as iam\n"
        "_orig = kit_stack.KitStack.grant_statements\n"
        "def _wide(self, grant):\n"
        "    return [*_orig(self, grant), iam.PolicyStatement(actions=['*'], resources=['*'])]\n"
        "kit_stack.KitStack.grant_statements = _wide\n"
    ),
    # ... or reach CDK through a computed import name
    "computed-import": (
        "import importlib\n"
        "cdk = importlib.import_module('aws_' + 'cdk')\n"
        "__import__('constructs')\n"
    ),
}


@pytest.fixture(scope="module")
def k4_templates():
    from aws_cdk import App

    from infra.app import build_app

    return {s.stack_name: s.template for s in build_app(App(), kits=("k4",)).synth().stacks}


@pytest.mark.parametrize("probe", sorted(PROBES))
def test_import_time_kit_code_cannot_change_any_template(tmp_path, k4_templates, probe):
    """Each probe runs in the loader subprocess (a marker file proves it ran) and leaves every
    template byte for byte unchanged, because the CDK process never imports the kit."""
    from aws_cdk import App

    from infra.app import build_app
    from infra.kit_manifest import load_manifests

    marker = tmp_path / "ran"
    code = PROBES[probe] + f"open({str(marker)!r}, 'w').write('ran')\n"
    # The probe is its own module, imported by the package's __init__ (appended), so the
    # kit's real modules, FUNCTIONS included, stay exactly as they are.
    path = _overlay(
        tmp_path,
        {"_import_time_probe.py": code, "__init__.py": "\nfrom . import _import_time_probe\n"},
    )
    probed = load_manifests(["k4"], pythonpath=[path], cache=False)
    assert marker.read_text() == "ran"

    after = {
        s.stack_name: s.template
        for s in build_app(App(), kits=("k4",), manifests=probed).synth().stacks
    }
    diffs = template_diffs(k4_templates, after)
    assert not diffs, f"probe {probe} changed templates:\n" + "\n".join(diffs)


def template_diffs(before, after, *, limit=20):
    """Each differing (stack, section, logical id), with both values abridged."""
    out = []
    for stack in sorted(set(before) | set(after)):
        a, b = before.get(stack), after.get(stack)
        if a is None or b is None:
            out.append(f"{stack}: only in {'after' if a is None else 'before'}")
            continue
        for section in sorted(set(a) | set(b)):
            sa, sb = a.get(section), b.get(section)
            if sa == sb:
                continue
            if not (isinstance(sa, dict) and isinstance(sb, dict)):
                out.append(f"{stack}.{section}: {str(sa)[:300]} -> {str(sb)[:300]}")
                continue
            for lid in sorted(set(sa) | set(sb)):
                if sa.get(lid) != sb.get(lid):
                    va = json.dumps(sa.get(lid), sort_keys=True, default=str)[:400]
                    vb = json.dumps(sb.get(lid), sort_keys=True, default=str)[:400]
                    out.append(f"{stack}.{section}.{lid}:\n  before {va}\n  after  {vb}")
    return out[:limit] + ([f"... {len(out) - limit} more"] if len(out) > limit else [])


def test_template_diffs_names_the_stack_and_resource():
    before = {"S": {"Resources": {"A": {"Type": "X", "P": 1}, "B": {"Type": "Y"}}}}
    after = {"S": {"Resources": {"A": {"Type": "X", "P": 2}, "B": {"Type": "Y"}}}}
    (d,) = template_diffs(before, after)
    assert d.startswith("S.Resources.A:")
    assert '"P": 1' in d
    assert '"P": 2' in d
    assert template_diffs(before, before) == []


@pytest.mark.parametrize(
    "files",
    [
        {"infra.py": "def extend(*a, **k):\n    pass\n"},
        {"__init__.py": "\ndef extend(*a):\n    pass\n"},
    ],
    ids=["infra-module", "extend-export"],
)
def test_the_removed_extend_hook_fails_the_loader_pointing_at_functions(tmp_path, files):
    from infra.kit_manifest import ManifestError, load_manifests

    with pytest.raises(ManifestError, match="FUNCTIONS"):
        load_manifests(["k4"], pythonpath=[_overlay(tmp_path, files)], cache=False)


def test_no_kit_source_imports_cdk():
    """An early, friendly error only (the control is the loader boundary above)."""
    import ast

    root = REPO / "kits"
    offenders = []
    for py in root.glob("*/src/**/*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in ("aws_cdk", "constructs", "cdk_nag"):
                    offenders.append(f"{py.relative_to(root)}: {n}")
    assert list(root.glob("*/src/*")), "no kit sources found"
    assert offenders == []


# --- manifest schema ----------------------------------------------------------------------


def _k1_json():
    from infra.kit_manifest import _run_loader

    return _run_loader("k1", ())


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(extra=1), "unknown keys"),
        (lambda d: d.update(schema=2), "schema"),
        (lambda d: d.update(kit="k9"), "unknown kit"),
        (lambda d: d["tools"][0].update(kind="WRITE"), "READ or PROPOSE"),
        (lambda d: d["tools"][0].update(name="read_" + "x" * 60), "too long"),
        (lambda d: d["tools"][0].update(input_schema={"type": "array"}), "without items"),
        (lambda d: d["tools"][0].update(input_schema={"type": "date"}), "Gateway type"),
        (lambda d: d.update(reads=[]), "READS"),
        (
            lambda d: d.update(workflows={"x_only": next(iter(d["workflows"].values()))}),
            "names action|SPECS",
        ),
        (lambda d: next(iter(d["workflows"].values())).update(approval_ttl_s=True), "int"),
        (lambda d: next(iter(d["workflows"].values())).update(kit="k2"), "belongs to kit"),
        (
            lambda d: d.update(
                functions=[
                    {
                        "name": "fn_probe",
                        "handler": "awx_k2_purchase.x.h",
                        "grants": [],
                        "timeout_s": 30,
                        "memory_mb": 256,
                        "description": "",
                    }
                ]
            ),
            "outside the kit package",
        ),
        (
            lambda d: d.update(
                functions=[
                    {
                        "name": "fn_probe",
                        "handler": "awx_k1_treasury.x.h",
                        "grants": ["SECRET_READ"],
                        "timeout_s": 30,
                        "memory_mb": 256,
                        "description": "",
                    }
                ]
            ),
            "SECRET_READ",
        ),
        (
            lambda d: d.update(
                functions=[
                    {
                        "name": "fn_probe",
                        "handler": "awx_k1_treasury.x.h",
                        "grants": [],
                        "timeout_s": 900,
                        "memory_mb": 256,
                        "description": "",
                    }
                ]
            ),
            "timeout_s",
        ),
    ],
    ids=[
        "unknown-key",
        "schema",
        "kit",
        "kind",
        "name-length",
        "array-items",
        "type",
        "reads",
        "specs",
        "bool-ttl",
        "workflow-kit",
        "fn-package",
        "fn-grant",
        "fn-timeout",
    ],
)
def test_a_malformed_manifest_is_refused_before_any_construct(mutate, match):
    import copy

    from infra.kit_manifest import parse_manifest

    data = copy.deepcopy(_k1_json())
    mutate(data)
    with pytest.raises(ValueError, match=match):
        parse_manifest(data)


# --- declared kit functions ---------------------------------------------------------------


@pytest.mark.parametrize("kit", sorted(KITS))
def test_each_real_kit_stack_builds_exactly_its_declared_functions(app, symbols, kit):
    """Never skipped. Each real kit's declared functions (none today) are built, and each
    function role holds exactly the union of its grants' pairs plus its logs and X-Ray."""
    from role_check import pairs, role_statements

    from infra.constructs.kit_workflow import _camel

    built, _ = app
    ks = built.node.find_child(f"AwxKit{kit.upper()}")
    declared = {f.name: f for f in ks.manifest.function_specs()}
    template = _stacks(app)[ks.stack_name].template
    found = {
        r["Properties"]["Environment"]["Variables"]["AWX_FUNCTION"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::Lambda::Function"
        and "AWX_FUNCTION" in r["Properties"].get("Environment", {}).get("Variables", {})
    }
    assert found == set(declared)
    assert {r["Type"] for r in template["Resources"].values()} <= KIT_STACK_RESOURCE_TYPES
    sym = symbols[ks.stack_name]
    for name, spec in declared.items():
        expected = set(XRAY) | _logs(f"logs_fn_{kit}_{name}")
        for g in spec.grants:
            expected |= _grant_pairs(kit)[g]
        actual = pairs(role_statements(template, f"Fn{_camel(name)}Role", sym), sym)
        _assert_exact(actual, expected)


def _install(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    for k, v in attrs.items():
        setattr(module, k, v)
    monkeypatch.setitem(sys.modules, name, module)


def _manifest_with(monkeypatch, functions, kit="k4"):
    """A stand-in: the real kit plus a functions module, described IN-PROCESS (a test may run
    kit code). build_app receives the manifest; KitStack never sees the module."""
    from infra.kit_manifest import build_manifest
    from infra.stacks.kit_stack import kit_package

    _install(monkeypatch, f"{kit_package(kit)}.functions", FUNCTIONS=tuple(functions))
    return build_manifest(kit)


def _with_functions(monkeypatch, functions, kit="k4"):
    from aws_cdk import App

    from infra.app import build_app

    manifest = _manifest_with(monkeypatch, functions, kit)
    return build_app(App(), kits=(kit,), manifests={kit: manifest})


def _k4_upload_spec():
    from awx_common.functions import KitFunctionSpec, KitGrant

    return KitFunctionSpec(
        name="evidence_upload",
        handler="awx_k4_dispute.evidence.ingest_evidence",
        grants=(KitGrant.EVIDENCE_PUT, KitGrant.KIT_STORE),
        timeout_s=60,
        memory_mb=512,
        description="Kit 4 evidence upload: ingest, index, write evidence/k4/<tenant>/<id>",
    )


def _grant_pairs(kit):
    from awx_common.functions import KitGrant

    lc = _ledger_cond(kit)
    return {
        KitGrant.EVIDENCE_PUT: {
            ("s3:putobject", "{bucket}/evidence/" + kit + "/*", NONE),
            ("s3:abortmultipartupload", "{bucket}/evidence/" + kit + "/*", NONE),
            ("kms:generatedatakey", "{key}", NONE),
            ("kms:decrypt", "{key}", NONE),
        },
        KitGrant.KIT_STORE: {
            *(
                ("dynamodb:" + a, "{ledger}", lc)
                for a in ("getitem", "putitem", "updateitem", "query")
            ),
            ("kms:decrypt", "{key}", NONE),
            ("kms:describekey", "{key}", NONE),
            ("kms:generatedatakey", "{key}", NONE),
        },
        KitGrant.OWN_READER: {("lambda:invokefunction", "{reader_" + kit + "}", NONE)},
    }


def _function_role_pairs(built, kit, name):
    from role_check import pairs, role_statements

    from infra.constructs.kit_workflow import _camel

    ks = built.node.find_child(f"AwxKit{kit.upper()}")
    sym = symbols_by_stack(built)[ks.stack_name]
    template = next(s for s in built.synth().stacks if s.stack_name == ks.stack_name).template
    return template, pairs(role_statements(template, f"Fn{_camel(name)}Role", sym), sym)


def test_k4_upload_shape_synthesizes_with_exactly_its_pairs(monkeypatch):
    from aws_cdk.assertions import Annotations, Match
    from awx_common.functions import KitGrant

    built = _with_functions(monkeypatch, [_k4_upload_spec()])
    template, actual = _function_role_pairs(built, "k4", "evidence_upload")
    errors = Annotations.from_stack(built.node.find_child("AwxKitK4")).find_error(
        "*", Match.string_like_regexp(".*")
    )
    assert errors == [], [e.entry.data for e in errors][:3]
    grants = _grant_pairs("k4")
    expected = (
        grants[KitGrant.EVIDENCE_PUT]
        | grants[KitGrant.KIT_STORE]
        | XRAY
        | _logs("logs_fn_k4_evidence_upload")
    )
    _assert_exact(actual, expected)

    res = template["Resources"]
    (fn,) = [
        r
        for r in res.values()
        if r["Type"] == "AWS::Lambda::Function"
        and r["Properties"].get("Environment", {}).get("Variables", {}).get("AWX_FUNCTION")
    ]
    props = fn["Properties"]
    assert props["Handler"] == "awx_k4_dispute.evidence.ingest_evidence"
    assert props["Timeout"] == 60
    assert props["MemorySize"] == 512
    assert set(props["Environment"]["Variables"]) == {
        "AWX_KIT",
        "AWX_FUNCTION",
        "AWX_ARTIFACTS_BUCKET",
        "AWX_EVIDENCE_PREFIX",
        "AWX_LEDGER_TABLE",
    }
    assert props["Environment"]["Variables"]["AWX_EVIDENCE_PREFIX"] == "evidence/k4/"
    (role,) = [
        r
        for lid, r in res.items()
        if r["Type"] == "AWS::IAM::Role" and lid.startswith("FnEvidenceUploadRole")
    ]
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert [s["Principal"] for s in trust] == [{"Service": "lambda.amazonaws.com"}]
    assert "ManagedPolicyArns" not in role["Properties"]
    assert {r["Type"] for r in res.values()} <= KIT_STACK_RESOURCE_TYPES
    outputs = template.get("Outputs", {})
    named = [o for k, o in outputs.items() if k.startswith("FnEvidenceUploadName")]
    assert len(named) == 1
    assert "Export" not in named[0]


@pytest.mark.parametrize("grant", ["EVIDENCE_PUT", "KIT_STORE", "OWN_READER"])
def test_each_grant_maps_to_exactly_its_statements(monkeypatch, grant):
    from awx_common.functions import KitFunctionSpec, KitGrant

    g = KitGrant(grant)
    built = _with_functions(
        monkeypatch, [KitFunctionSpec("probe", "awx_k4_dispute.evidence.ingest_evidence", (g,))]
    )
    _, actual = _function_role_pairs(built, "k4", "probe")
    _assert_exact(actual, _grant_pairs("k4")[g] | XRAY | _logs("logs_fn_k4_probe"))


def test_a_function_with_no_grant_holds_only_its_own_logs_and_xray(monkeypatch):
    from awx_common.functions import KitFunctionSpec

    built = _with_functions(
        monkeypatch, [KitFunctionSpec("bare", "awx_k4_dispute.evidence.ingest_evidence")]
    )
    _, actual = _function_role_pairs(built, "k4", "bare")
    _assert_exact(actual, XRAY | _logs("logs_fn_k4_bare"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"grants": ("SECRET_READ",)},
        {"grants": ("EVIDENCE_PUT",)},  # a raw string, not the enum
        {"timeout_s": 0},
        {"timeout_s": 301},
        {"memory_mb": 64},
        {"memory_mb": 2048},
        {"handler": "not-dotted"},
    ],
    ids=[
        "unknown-grant",
        "string-grant",
        "timeout-0",
        "timeout-301",
        "mem-64",
        "mem-2048",
        "handler",
    ],
)
def test_a_bad_function_spec_is_refused(kwargs):
    from awx_common.functions import KitFunctionSpec

    base = {"name": "probe", "handler": "awx_k4_dispute.evidence.ingest_evidence"}
    with pytest.raises(ValueError, match=r"grant|timeout|memory|handler"):
        KitFunctionSpec(**{**base, **kwargs})


@pytest.mark.parametrize(
    "handler",
    [
        "awx_k2_purchase.workflow.handler",  # another kit's package
        "awx_spine_aws.handlers.reader.handler",  # the secret holder's code
        "awx_k4_dispute.handler",  # the package itself, not a module in it
        "awx_k4_dispute.no_such_module.handler",
    ],
)
def test_a_handler_outside_the_kit_package_is_refused_by_the_loader(monkeypatch, handler):
    from awx_common.functions import KitFunctionSpec

    with pytest.raises(ValueError, match=r"outside the kit package|does not exist"):
        _manifest_with(monkeypatch, [KitFunctionSpec("probe", handler)])


def test_kits_context_selects_a_subset_refuses_empty_and_warns_on_partial_agent():
    from aws_cdk import App
    from aws_cdk.assertions import Annotations, Match

    from infra.app import build_app

    app = build_app(App(context={"kits": "k1,k6"}))
    assert {s.stack_name for s in app.synth().stacks} == {
        "AwxSpine",
        "AwxKitK1",
        "AwxKitK6",
        "AwxAgentStack",
    }
    warnings = Annotations.from_stack(app.node.find_child("AwxAgentStack")).find_warning(
        "*", Match.string_like_regexp(".*removes the tools.*")
    )
    assert warnings
    for bad in ("k9", " ", ",", " , "):
        with pytest.raises(ValueError, match=r"unknown kits|names no kit"):
            build_app(App(context={"kits": bad}))


def test_the_retained_resources_are_exactly_the_runbook_list(app):
    """Counted per type, so a second retained
    bucket (or any new Retain resource) fails."""
    from collections import Counter

    retained = Counter(
        r["Type"]
        for s in _stacks(app).values()
        for r in s.template["Resources"].values()
        if r.get("DeletionPolicy") == "Retain"
    )
    assert retained == Counter(
        {
            "AWS::SecretsManager::Secret": 1,
            "AWS::DynamoDB::Table": 6,
            "AWS::Cognito::UserPool": 1,
            "AWS::S3::Bucket": 1,
            "AWS::KMS::Key": 1,
        }
    )


# --- the loader's output is untrusted --------------------------------------------------------


def _loader_json(kit):
    from infra.kit_manifest import _run_loader

    return _run_loader(kit, ())


def test_probe_a_another_kits_manifest_relabelled_is_refused():
    """k2's real manifest relabelled k4: the package is pinned to k4's, so it fails."""
    import copy

    from infra.kit_manifest import ManifestError, parse_manifest

    data = copy.deepcopy(_loader_json("k2"))
    data["kit"] = "k4"
    for wf in data["workflows"].values():
        node = wf
        while node is not None:
            node["kit"] = "k4"
            node = node["child"]
    with pytest.raises(ManifestError, match=r"awx_k2_purchase.*awx_k4_dispute"):
        parse_manifest(data, expected_kit="k4")


def test_probe_c_a_spine_package_with_a_spine_handler_is_refused():
    import copy

    from infra.kit_manifest import ManifestError, parse_manifest

    data = copy.deepcopy(_loader_json("k4"))
    data["package"] = "awx_spine_aws"
    data["functions"] = [
        {
            "name": "shim",
            "handler": "awx_spine_aws.handlers.reader.handler",
            "grants": [],
            "timeout_s": 30,
            "memory_mb": 256,
            "description": "",
        }
    ]
    with pytest.raises(ManifestError, match="package"):
        parse_manifest(data, expected_kit="k4")
    data["package"] = "awx_k4_dispute"  # with the right package the handler is outside it
    with pytest.raises(ValueError, match="outside the kit package"):
        parse_manifest(data, expected_kit="k4")


def test_probe_d_a_loader_that_prints_another_kits_manifest_is_refused(tmp_path):
    """A k4 package whose __init__ prints k2's manifest and exits: refused by the parent."""
    from infra.kit_manifest import ManifestError, load_manifests

    k2 = json.dumps(_loader_json("k2"))
    init = f"\nimport os as _os\n_os.write(1, {k2!r}.encode())\n_os._exit(0)\n"
    with pytest.raises(ManifestError, match="k4 loader returned a manifest for k2"):
        load_manifests(["k4"], pythonpath=[_overlay(tmp_path, {"__init__.py": init})], cache=False)
    # the same, relabelled as k4 in the printed JSON: refused on the package instead
    data = _loader_json("k2")
    data["kit"] = "k4"
    for wf in data["workflows"].values():
        wf["kit"] = "k4"
    init = f"\nimport os as _os\n_os.write(1, {json.dumps(data)!r}.encode())\n_os._exit(0)\n"
    with pytest.raises(ManifestError, match="package"):
        load_manifests(
            ["k4"], pythonpath=[_overlay(tmp_path / "b", {"__init__.py": init})], cache=False
        )


def _first_wf(data):
    return next(iter(data["workflows"].values()))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (  # B: `.*` on the platform list (and, next case, a tenant regex that does not compile)
            lambda d: _first_wf(d).update(platform_read_allowlist=[".*"]),
            "anchored",
        ),
        (lambda d: _first_wf(d).update(read_allowlist=["^/api/v1/($"]), "does not compile"),
        (  # B2: duplicate step names
            lambda d: _first_wf(d).update(steps=[*_first_wf(d)["steps"], _first_wf(d)["steps"][0]]),
            "unique",
        ),
        (  # B4: interval greater than timeout
            lambda d: _first_wf(d)["steps"].__setitem__(
                0, {"name": "s", "finality": {"timeout_s": 5, "interval_s": 60}}
            ),
            "interval_s",
        ),
        (  # B3: an action that is not snake_case
            lambda d: d.update(workflows={"Bad-Action": {**_first_wf(d), "action": "Bad-Action"}}),
            "snake_case|SPECS",
        ),
        (lambda d: _first_wf(d).update(fan_out=True), "fan_out and child"),
        (lambda d: _first_wf(d).update(fan_out="yes"), "bool"),
    ],
    ids=[
        "B-anchor",
        "B-compile",
        "B2-dup-steps",
        "B4-interval",
        "B3-action",
        "child-fanout",
        "fanout-type",
    ],
)
def test_the_parent_reruns_the_spec_rules_on_the_manifest(mutate, match):
    """The shared awx_common.spec_rules run on the loader's output."""
    import copy

    from infra.kit_manifest import parse_manifest

    data = copy.deepcopy(_loader_json("k3"))
    mutate(data)
    with pytest.raises(ValueError, match=match):
        parse_manifest(data, expected_kit="k3")


def test_the_spec_and_the_manifest_use_one_validator(monkeypatch):
    """Both KitWorkflowSpec and parse_manifest call awx_common.spec_rules.check_workflow_shape."""
    import awx_common.workflow as wf_mod

    from infra import kit_manifest

    calls = []
    real = wf_mod.check_workflow_shape

    def spy(**kw):
        calls.append(kw["action"])
        return real(**kw)

    monkeypatch.setattr(wf_mod, "check_workflow_shape", spy)
    monkeypatch.setattr(kit_manifest, "check_workflow_shape", spy)
    from datetime import timedelta

    from awx_common.workflow import KitWorkflowSpec, StepSpec

    spec = KitWorkflowSpec(
        kit="k3",
        action="probe_action",
        validate=lambda r: r,
        policy=lambda i, c: None,
        steps=(StepSpec(name="s", build=lambda c: ()),),
        approval_ttl=timedelta(hours=1),
        read_allowlist=("^/api/v1/x$",),
    )
    kit_manifest.parse_workflow(spec)
    assert calls == ["probe_action", "probe_action"]


def _role_key(stack_name, entry):
    """The role an inline policy attaches to, as (owning stack, logical id), following a
    cross-stack import back to its export (`<Stack>:ExportsOutputRef<LogicalId><hash>`)."""
    if isinstance(entry, dict) and "Ref" in entry:
        return (stack_name, entry["Ref"])
    if isinstance(entry, dict) and "Fn::ImportValue" in entry:
        name = entry["Fn::ImportValue"]
        owner, _, out = name.partition(":")
        assert out.startswith("ExportsOutputRef"), name
        return (owner, out.removeprefix("ExportsOutputRef")[:-8])  # drop CDK's 8-char hash
    raise AssertionError(f"{stack_name}: unexpected policy role {entry!r}")


def test_inline_policy_names_are_unique_per_role_across_every_stack(app):
    """IAM inline policy names are unique per role. k2 and k5 both have issue_card, and
    both kit stacks attach an ApprovalDescribe policy to the spine's one approval role; with a
    logical-id-derived name the second stack fails to create. Synth does not catch that, the
    deploy does, so pin it here."""
    seen = {}
    for name, stack in _stacks(app).items():
        for lid, res in stack.template.get("Resources", {}).items():
            if res["Type"] != "AWS::IAM::Policy":
                continue
            pname = res["Properties"]["PolicyName"]
            for entry in res["Properties"].get("Roles", []):
                key = (_role_key(name, entry), json.dumps(pname, sort_keys=True))
                assert key not in seen, f"{name}.{lid} and {seen[key]} share {key}"
                seen[key] = f"{name}.{lid}"
    approval = [k for k in seen if "ApprovalRole" in k[0][1]]
    assert len(approval) >= 30  # every kit action's ApprovalDescribe plus the spine's own


def test_the_gateway_waits_for_its_service_role_policy(app):
    """The Gateway depends on its service role's DefaultPolicy (the KMS grant it encrypts
    with); created before that policy exists, the Gateway fails to deploy."""
    tpl = _stacks(app)["AwxAgentStack"].template["Resources"]
    (gw,) = [r for r in tpl.values() if r["Type"] == "AWS::BedrockAgentCore::Gateway"]
    role_lid = gw["Properties"]["RoleArn"]["Fn::GetAtt"][0]
    (policy,) = [
        lid
        for lid, r in tpl.items()
        if r["Type"] == "AWS::IAM::Policy" and {"Ref": role_lid} in r["Properties"]["Roles"]
    ]
    assert policy in gw.get("DependsOn", [])
