"""awx-deploy, the operator deploy runner, offline, against moto and FakeAirwallex.

Pinned: two rows on one account are refused (in a file and across kits); another kit's row is
refused at `run`; a replace is refused while an execution is RUNNING;
a placeholder is refused; kit 1's and kit 8's shipped deploy files apply in full from empty
tables and the kit's tools then work on those stores; a second apply is safe; plan writes
nothing; a trigger proposes with origin SCHEDULE and starts the root execution."""

from __future__ import annotations

import contextlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from awx_agent.testing import KitToolHarness
from awx_common import Money, Operation, OperationStatus, derive_request_id
from awx_common.transport import loads
from awx_spine.fake import FakeAirwallex
from awx_spine_aws import deploy
from awx_spine_aws.deploy import DeployError, Deployment, Runner, load_file, substitute

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy"
NOW = datetime.now(UTC).replace(microsecond=0)
ROLE = "arn:aws:iam::123456789012:role/sfn"


@pytest.fixture
def world(aws):
    sfn = boto3.client("stepfunctions", region_name="ap-southeast-2")
    fake = FakeAirwallex(balances={"USD": Money.of("42000", "USD"), "EUR": Money.of("2000", "EUR")})
    d = Deployment(
        tenants_table="tenants",
        ledger_table="ledger",
        secret_arn=None,
        ddb=aws.db,
        sfn=sfn,
        port=fake,
        clock=lambda: NOW,
    )
    lines: list[str] = []
    return d, Runner(d, out=lines.append), fake, lines


def _machine(d: Deployment, name: str) -> str:
    definition = json.dumps(
        {"StartAt": "W", "States": {"W": {"Type": "Wait", "Seconds": 600, "End": True}}}
    )
    return d.sfn.create_state_machine(name=name, definition=definition, roleArn=ROLE)[
        "stateMachineArn"
    ]


def _ledger(d: Deployment) -> list:
    items = d.ddb.scan(TableName="ledger")["Items"]
    return sorted(json.dumps(i, sort_keys=True) for i in items)


def _beneficiary(fake: FakeAirwallex, name: str) -> str:
    key = f"poc-i1-ben-{name}"
    rid = derive_request_id("k1", key, "create")
    res = fake.execute(
        Operation(
            kit="k1",
            name="beneficiaries.create",
            method="POST",
            path="/api/v1/beneficiaries/create",
            body={
                "request_id": rid,
                "nickname": key,
                "transfer_methods": ["LOCAL"],
                "beneficiary": {
                    "type": "BANK_ACCOUNT",
                    "entity_type": "COMPANY",
                    "address": {"country_code": "US", "state": "US-NY"},
                    "bank_details": {
                        "bank_account_category": "Checking",
                        "account_routing_type1": "aba",
                    },
                },
            },
            request_id=rid,
            business_key=key,
            intent_hash="b" * 64,
        )
    )
    assert res.status is OperationStatus.SUCCEEDED, res
    return str(res.provider_id)


def _k8_vars(fake: FakeAirwallex) -> dict[str, str]:
    return {
        f"K8_ACCT_{s.upper()}": fake.add_connected_account(
            f"poc-i8-{s}", {"USD": Money.of("0", "USD")}
        )
        for s in ("alice", "bob", "cara")
    }


# --- tenants put ------------------------------------------------------------------------------


def test_two_rows_on_one_account_are_refused_in_one_file(world):
    d, r, _, _ = world
    rows = deploy.parse_rows(
        [
            {"tenant_id": "a", "kit": "k8", "role": "customer", "connected_account_id": "acct_1"},
            {"tenant_id": "b", "kit": "k7", "role": "customer", "connected_account_id": "acct_1"},
        ]
    )
    with pytest.raises(DeployError, match="share one connected account"):
        r.tenants_put(rows)
    assert d.ddb.get_item(TableName="tenants", Key={"tenant_id": {"S": "a"}}).get("Item") is None


def test_an_account_already_on_a_row_of_any_kit_is_refused(world):
    _d, r, _, _ = world  # the harness seeds tenant-a (kit k3) on acct_A
    rows = deploy.parse_rows(
        [{"tenant_id": "bob", "kit": "k8", "role": "customer", "connected_account_id": "acct_A"}]
    )
    with pytest.raises(DeployError, match="already on row tenant-a"):
        r.tenants_put(rows)
    first = deploy.parse_rows(
        [{"tenant_id": "x", "kit": "k8", "role": "customer", "connected_account_id": "acct_X"}]
    )
    assert r.tenants_put(first) == {"written": ["x"], "unchanged": []}
    again = deploy.parse_rows(
        [{"tenant_id": "y", "kit": "k7", "role": "customer", "connected_account_id": "acct_X"}]
    )
    with pytest.raises(DeployError, match="already on row x"):
        r.tenants_put(again)


def test_rows_follow_the_kit_bound_row_rule(world):
    _, r, _, _ = world
    for row, why in [
        (
            {"tenant_id": "a", "kit": "k8", "role": "customer", "connected_account_id": ""},
            "malformed",
        ),
        ({"tenant_id": "a", "kit": "k8", "role": "customer"}, "must be an operator"),
        (
            {"tenant_id": "a", "kit": "k8", "role": "operator", "connected_account_id": "acct_1"},
            "cannot be",
        ),
        (
            {
                "tenant_id": "a",
                "kit": "k8",
                "role": "customer",
                "connected_account_id": "acct_1\nx",
            },
            "malformed",
        ),
    ]:
        with pytest.raises(DeployError, match=why):
            r.tenants_put(deploy.parse_rows([row]))
    with pytest.raises(DeployError, match="kit must be"):
        deploy.parse_rows([{"tenant_id": "a", "kit": "k9"}])


def test_an_identical_row_is_unchanged_and_a_different_one_needs_replace(world):
    d, r, _, _ = world
    row = {"tenant_id": "bob", "kit": "k8", "role": "customer", "connected_account_id": "acct_B1"}
    assert r.tenants_put(deploy.parse_rows([row]))["written"] == ["bob"]
    assert r.tenants_put(deploy.parse_rows([row]))["unchanged"] == ["bob"]
    moved = {**row, "connected_account_id": "acct_B2"}
    with pytest.raises(DeployError, match="pass --replace"):
        r.tenants_put(deploy.parse_rows([moved]))
    assert r.tenants_put(deploy.parse_rows([moved]), replace_rows=True)["written"] == ["bob"]
    marker = d.ddb.get_item(TableName="tenants", Key={"tenant_id": {"S": "account#acct_B1"}})
    assert "Item" not in marker  # the old account is free again
    assert d.ddb.get_item(TableName="tenants", Key={"tenant_id": {"S": "account#acct_B2"}})["Item"]


def test_a_tenants_replace_is_refused_while_the_kit_is_running(world):
    d, r, _, _ = world
    row = {"tenant_id": "bob", "kit": "k8", "role": "customer", "connected_account_id": "acct_B1"}
    r.tenants_put(deploy.parse_rows([row]))
    arn = _machine(d, "awx-k8-settle_seller_cycle")
    d.sfn.start_execution(stateMachineArn=arn, name="busy", input="{}")
    moved = {**row, "connected_account_id": "acct_B2"}
    with pytest.raises(DeployError, match="RUNNING"):
        r.tenants_put(deploy.parse_rows([moved]), replace_rows=True)


# --- run --------------------------------------------------------------------------------------


def test_another_kits_row_is_refused_at_run(world):
    d, r, fake, _ = world
    r.tenants_put(deploy.parse_rows([{"tenant_id": "zed", "kit": "k6", "role": "operator"}]))
    acct = fake.add_connected_account("poc-i8-zed", {"USD": Money.of("0", "USD")})
    args = {
        "seller": "zed",
        "connected_account_id": acct,
        "currency": "USD",
        "tenure_days": 30,
        "trailing_refund_rate": "0.04",
    }
    before = _ledger(d)
    with pytest.raises(DeployError, match="another kit"):
        r.run_command("k8", "seed_seller", args)
    assert _ledger(d) == before  # nothing written


def test_a_replace_is_refused_while_an_execution_is_running(world):
    d, r, fake, _ = world
    args = {"beneficiaries": {"ob-shipping": _beneficiary(fake, "shipping")}, "book_version": "v1"}
    r.run_command("k1", "seed_treasury_book", args)
    arn = _machine(d, "awx-k1-execute_plan")
    d.sfn.start_execution(stateMachineArn=arn, name="busy", input="{}")
    before = _ledger(d)
    with pytest.raises(DeployError, match="RUNNING"):
        r.run_command("k1", "seed_treasury_book", {**args, "replace": True})
    assert _ledger(d) == before
    d.sfn.stop_execution(executionArn=arn.replace(":stateMachine:", ":execution:") + ":busy")
    assert r.run_command("k1", "seed_treasury_book", {**args, "replace": True})["replaced"] is True


def test_another_kits_machines_do_not_block_a_replace(world):
    d, r, fake, _ = world
    args = {"beneficiaries": {"ob-shipping": _beneficiary(fake, "shipping")}, "book_version": "v1"}
    r.run_command("k1", "seed_treasury_book", args)
    other = _machine(d, "awx-k8-settle_seller_cycle")
    d.sfn.start_execution(stateMachineArn=other, name="busy", input="{}")
    assert r.run_command("k1", "seed_treasury_book", {**args, "replace": True})["replaced"] is True


@pytest.mark.parametrize("bad", ["<beneficiary id>", "${K1_BEN_SHIPPING}"])
def test_a_placeholder_is_refused(world, bad):
    d, r, _, _ = world
    with pytest.raises(DeployError, match="placeholders"):
        r.run_command(
            "k1",
            "seed_treasury_book",
            {"beneficiaries": {"ob-shipping": bad}, "book_version": "v1"},
        )
    with pytest.raises(DeployError, match="placeholders"):
        r.apply(load_file(str(DEPLOY / "k1.yaml")))  # the shipped template, no vars
    assert _ledger(d) == []


def test_the_kits_own_placeholder_refusal_still_applies(world):
    _, r, _, _ = world
    with pytest.raises(DeployError, match="refused"):
        r.run_command(
            "k1",
            "seed_treasury_book",
            {"beneficiaries": {"ob-shipping": "ben-poc-k1-x"}, "book_version": "v1"},
        )


def test_a_command_with_platform_reads_refuses_tenant_scope(world):
    _, r, _, _ = world
    ctx = r.context("k1")
    from awx_common.ops import ReadRequest

    with pytest.raises(Exception, match="platform scope"):
        ctx.read(ReadRequest("/api/v1/balances/current", on_behalf_of="acct_1"))


# --- deploy files -----------------------------------------------------------------------------


def _k1_vars(fake):
    return {
        "K1_BEN_SUPPLIER_EUR": _beneficiary(fake, "supplier"),
        "K1_BEN_SHIPPING": _beneficiary(fake, "shipping"),
    }


def test_kit_1s_deploy_file_applies_in_full_and_its_tools_work(world):
    d, r, fake, _lines = world
    doc = substitute(load_file(str(DEPLOY / "k1.yaml")), _k1_vars(fake))
    assert r.apply(doc) == ["1"]
    h = KitToolHarness.for_kit("k1", port=fake, clock=lambda: NOW)
    h.stores[("k1", "platform")] = d.store("k1", "platform")
    obs = h.call("read_obligations", {})["untrusted_data"]["obligations"]
    assert {o["obligation_id"] for o in obs} == {"ob-shipping", "ob-supplier-eur"}
    out = h.call("propose_execute_plan", {"obligation_ids": ["ob-shipping"]})
    assert out["risk_tier"] == "AUTO", out


def test_a_second_apply_is_safe(world):
    d, r, fake, _ = world
    doc = substitute(load_file(str(DEPLOY / "k1.yaml")), _k1_vars(fake))
    r.apply(doc)
    before = _ledger(d)
    with pytest.raises(DeployError, match=r"step 1 refused.*already seeded"):
        r.apply(doc)
    assert _ledger(d) == before


def test_kit_8s_deploy_file_applies_in_full_and_its_tools_work(world):
    d, r, fake, _ = world
    acct = _k8_vars(fake)
    doc = substitute(load_file(str(DEPLOY / "k8.yaml")), acct)
    assert r.apply(doc) == [str(n) for n in range(1, 8)]
    tenants = {s: acct[f"K8_ACCT_{s.upper()}"] for s in ("alice", "bob", "cara")}
    h = KitToolHarness.for_kit("k8", port=fake, tenants=tenants)
    for t in ("platform", *tenants):
        h.stores[("k8", t)] = d.store("k8", t)
    ledger = h.call("read_seller_ledger", {}, tenant_id="bob")["untrusted_data"]
    assert ledger["identity_residual"]["amount"] == "0.00"
    out = h.call("propose_settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    assert out["status"] == "AWAITING_APPROVAL", out
    # a second apply: the tenants rows are unchanged, the first seed refuses, nothing changes
    before = _ledger(d)
    with pytest.raises(DeployError, match="step 2 refused"):
        r.apply(doc)
    assert _ledger(d) == before
    # feeds re-run from where they are no-ops
    assert r.apply(doc, start=5) == ["5", "6", "7"]
    assert _ledger(d) == before


def test_plan_prints_every_step_and_writes_nothing(world):
    d, _, _fake, _ = world
    lines: list[str] = []
    code = deploy.main(["plan", str(DEPLOY / "k8.yaml")], deployment=d, out=lines.append)
    assert code == 2  # unresolved account ids
    text = "\n".join(lines)
    assert "tenants put (3 rows)" in text and "run k8 seed_seller" in text and "UNRESOLVED" in text
    vars_ = [f"--var=K8_ACCT_{s.upper()}=acct_{s}" for s in ("alice", "bob", "cara")]
    assert deploy.main(["plan", str(DEPLOY / "k8.yaml"), *vars_], out=lines.append) == 0
    assert _ledger(d) == []
    assert "Items" not in d.ddb.get_item(TableName="tenants", Key={"tenant_id": {"S": "bob"}})


@pytest.mark.parametrize("kit", ["k1", "k5", "k6", "k7", "k8"])
def test_every_template_plans_cleanly_once_its_variables_are_given(kit):
    doc = load_file(str(DEPLOY / f"{kit}.yaml"))
    names = sorted({m for m in deploy._VAR.findall(json.dumps(doc))})
    assert names, f"{kit}.yaml has no variables"
    values = {n: (f"acct_{n.lower()}" if "ACCT" in n else f"v-{n.lower()}") for n in names}
    lines: list[str] = []
    assert Runner(deploy._NoDeployment(), out=lines.append).plan(substitute(doc, values)) == 0
    assert Runner(deploy._NoDeployment(), out=lines.append).plan(doc) > 0


# --- trigger ----------------------------------------------------------------------------------


def test_a_trigger_proposes_with_origin_schedule_and_starts_the_root(world):
    d, r, fake, _ = world
    acct = _k8_vars(fake)
    r.apply(substitute(load_file(str(DEPLOY / "k8.yaml")), acct))
    arn = _machine(d, "awx-k8-settle_seller_cycle")
    out = r.trigger("k8", "settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    assert out["started"] is True
    (execution,) = d.sfn.list_executions(stateMachineArn=arn)["executions"]
    assert execution["name"] == out["proposal_id"]
    doc = loads(d.sfn.describe_execution(executionArn=execution["executionArn"])["input"])
    assert doc["origin"] == "SCHEDULE" and doc["tenant_id"] == "bob"
    again = r.trigger("k8", "settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    assert again["proposal_id"] == out["proposal_id"]  # the same run, never a second one
    assert len(d.sfn.list_executions(stateMachineArn=arn)["executions"]) == 1


def test_only_schedule_driven_actions_can_be_triggered(world):
    _, r, _, _ = world
    with pytest.raises(DeployError, match="not schedule-driven"):
        r.trigger("k8", "recover_shortfall", {}, tenant_id="bob")
    with pytest.raises(DeployError, match="not schedule-driven"):
        r.trigger("k1", "execute_plan", {}, tenant_id="platform")


def test_a_trigger_refuses_another_kits_tenant(world):
    d, r, _, _ = world
    r.tenants_put(
        deploy.parse_rows([{"tenant_id": "k6:operator", "kit": "k6", "role": "operator"}])
    )
    _machine(d, "awx-k8-reconcile_seller_money")
    with pytest.raises(DeployError, match="another kit"):
        r.trigger("k8", "reconcile_seller_money", {}, tenant_id="k6:operator")


# --- discovery and the CLI --------------------------------------------------------------------


class _Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, **kw):
        self.calls.append(kw)
        return iter(self.pages)


class _Cfn:
    def __init__(self):
        self.pager = _Pages(
            [
                {
                    "StackResourceSummaries": [
                        {
                            "LogicalResourceId": f"Fn{i}",
                            "ResourceType": "AWS::Lambda::Function",
                            "PhysicalResourceId": f"fn-{i}",
                        }
                        for i in range(100)
                    ]
                },
                {
                    "StackResourceSummaries": [
                        {
                            "LogicalResourceId": "LedgerTableABC123",
                            "ResourceType": "AWS::DynamoDB::Table",
                            "PhysicalResourceId": "AwxSpine-LedgerTable-2",
                        },
                        {
                            "LogicalResourceId": "TenantsTableDEF",
                            "ResourceType": "AWS::DynamoDB::Table",
                            "PhysicalResourceId": "AwxSpine-TenantsTable-1",
                        },
                    ]
                },
            ]
        )

    def describe_stacks(self, StackName):
        assert StackName == "AwxSpine"
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": "TenantsTableName", "OutputValue": "AwxSpine-TenantsTable-1"},
                        {
                            "OutputKey": "AirwallexSecretArn",
                            "OutputValue": "arn:aws:secretsmanager:x:1:secret:s",
                        },
                    ]
                }
            ]
        }

    def get_paginator(self, name):
        assert name == "list_stack_resources"
        return self.pager

    def describe_stack_resources(self, StackName):
        raise AssertionError("first 100 resources only: use the list_stack_resources paginator")


def test_discovery_reads_the_spine_stack():
    cfn = _Cfn()
    d = deploy.discover(cfn, ddb=None, sfn=None)
    assert cfn.pager.calls == [{"StackName": "AwxSpine"}]  # past the first 100 resources
    assert (d.tenants_table, d.ledger_table) == (
        "AwxSpine-TenantsTable-1",
        "AwxSpine-LedgerTable-2",
    )
    assert d.secret_arn.endswith(":secret:s")
    assert deploy.discover(_Cfn(), ddb=None, sfn=None, ledger_table="L").ledger_table == "L"


def test_a_missing_spine_stack_names_the_stack_and_the_region_searched():
    from botocore.exceptions import ClientError

    class Missing(_Cfn):
        meta = SimpleNamespace(region_name="eu-west-1")

        def describe_stacks(self, StackName):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": f"Stack with id {StackName} does not exist",
                    }
                },
                "DescribeStacks",
            )

    with pytest.raises(deploy.DeployError) as caught:
        deploy.discover(Missing(), ddb=None, sfn=None)
    message = str(caught.value)
    assert "AwxSpine not found in eu-west-1" in message
    assert "pass --region" in message


def test_the_cli_runs_put_run_and_apply(world, tmp_path):
    d, _, fake, _ = world
    rows = tmp_path / "rows.json"
    rows.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "tenant_id": "bob",
                        "kit": "k8",
                        "role": "customer",
                        "connected_account_id": "${ACCT}",
                    }
                ]
            }
        )
    )
    lines: list[str] = []
    assert (
        deploy.main(
            ["tenants", "put", str(rows), "--var", "ACCT=acct_cli"], deployment=d, out=lines.append
        )
        == 0
    )
    assert (
        deploy.main(
            ["tenants", "put", str(rows), "--var", "ACCT=acct_A"], deployment=d, out=lines.append
        )
        == 1
    )
    assert lines[-1].startswith("refused:")
    args = tmp_path / "a.json"
    args.write_text(
        json.dumps(
            {"beneficiaries": {"ob-shipping": _beneficiary(fake, "shipping")}, "book_version": "v1"}
        )
    )
    assert (
        deploy.main(["run", "k1", "seed_treasury_book", str(args)], deployment=d, out=lines.append)
        == 0
    )
    assert json.loads(lines[-1])["book_id"]
    assert (
        deploy.main(["run", "k1", "seed_treasury_book", str(args)], deployment=d, out=lines.append)
        == 1
    )


def test_the_console_script_is_declared():
    text = (ROOT / "packages" / "awx_spine_aws" / "pyproject.toml").read_text()
    assert 'awx-deploy = "awx_spine_aws.deploy:main"' in text
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        deploy.main(["--help"])
    assert "tenants" in buf.getvalue() and "trigger" in buf.getvalue()


# --- clients, paging, triggers and the shipped deploy files -----------------------------------


class _Secrets:
    """A secretsmanager client: the one the --profile/--region session made."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self):
        self.calls = []

    def get_secret_value(self, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": json.dumps({"client_id": "cid", "api_key": "key"})}


def test_the_secret_is_read_with_the_profiles_client(monkeypatch):
    made: list[tuple] = []
    secrets = _Secrets()

    class _Session:
        def __init__(self, profile_name=None, region_name=None):
            made.append(("session", profile_name, region_name))

        def client(self, name, *a, **kw):
            made.append((name, a, kw))
            return {"cloudformation": _Cfn(), "secretsmanager": secrets}.get(name, object())

    def _default_client(*a, **kw):
        raise AssertionError(f"a default-session client was made: {a}")

    monkeypatch.setattr(boto3, "Session", _Session)
    monkeypatch.setattr(boto3, "client", _default_client)
    args = deploy.parser().parse_args(
        ["--profile", "awx", "--region", "ap-southeast-2", "plan", "x"]
    )
    d = deploy.deployment_from_args(args)
    assert made[0] == ("session", "awx", "ap-southeast-2")
    client = d.reader()
    assert client._credentials.get() is not None
    assert secrets.calls == ["arn:aws:secretsmanager:x:1:secret:s"]


def test_region_comes_from_the_flag_then_the_outputs_file_then_the_default(tmp_path):
    from awx_common.regions import DEFAULT_REGION

    outputs = tmp_path / "cdk-outputs.json"
    assert deploy.resolve_region(None, outputs) == DEFAULT_REGION
    outputs.write_text(json.dumps({"AwxAgentStack": {"AwxRegion": "eu-west-1"}}))
    assert deploy.resolve_region(None, outputs) == "eu-west-1"
    assert deploy.resolve_region("ca-central-1", outputs) == "ca-central-1"
    outputs.write_text(json.dumps({"AwxSpine": {}}))
    with pytest.raises(deploy.DeployError, match="pass --region"):
        deploy.resolve_region(None, outputs)


def test_the_session_uses_the_outputs_files_region_with_no_flag(monkeypatch, tmp_path):
    outputs = tmp_path / "cdk-outputs.json"
    outputs.write_text(json.dumps({"AwxAgentStack": {"AwxRegion": "eu-west-2"}}))
    monkeypatch.setattr(deploy, "OUTPUTS_FILE", outputs)
    made: list[tuple] = []

    class _Session:
        def __init__(self, profile_name=None, region_name=None):
            made.append((profile_name, region_name))

        def client(self, name, *a, **kw):
            return {"cloudformation": _Cfn(), "secretsmanager": _Secrets()}.get(name, object())

    monkeypatch.setattr(boto3, "Session", _Session)
    deploy.deployment_from_args(deploy.parser().parse_args(["plan", "x"]))
    assert made == [(None, "eu-west-2")]


class _PagedSfn:
    """An empty first page with a nextToken, a RUNNING execution on page 2."""

    def __init__(self):
        self.calls = []

    def get_paginator(self, name):
        return _Pages(
            [{"stateMachines": [{"name": "awx-k1-execute_plan", "stateMachineArn": "arn:m"}]}]
        )

    def list_executions(self, **kw):
        self.calls.append(kw)
        if kw.get("nextToken") is None:
            return {"executions": [], "nextToken": "t2"}
        if kw["nextToken"] == "t2":
            return {"executions": [{"executionArn": "arn:e:busy"}], "nextToken": "t3"}
        raise AssertionError("read past the first RUNNING hit")


def test_the_quiet_check_follows_next_token_and_stops_at_the_first_hit():
    sfn = _PagedSfn()
    d = Deployment(tenants_table="t", ledger_table="l", secret_arn=None, ddb=None, sfn=sfn)
    assert d.running("k1") == ["arn:e:busy"]
    assert [c.get("nextToken") for c in sfn.calls] == [None, "t2"]
    assert all(c["statusFilter"] == "RUNNING" for c in sfn.calls)
    with pytest.raises(DeployError, match="RUNNING"):
        Runner(d, out=lambda _: None).require_quiet("k1", "replacing the book")


def test_a_trigger_reports_an_earlier_run_of_the_same_intent_in_another_window(world):
    d, r, fake, _ = world
    r.apply(substitute(load_file(str(DEPLOY / "k8.yaml")), _k8_vars(fake)))
    arn = _machine(d, "awx-k8-settle_seller_cycle")
    first = r.trigger("k8", "settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    assert first["started"] is True
    d.clock = lambda: NOW + timedelta(days=1)  # a new approval window: a new proposal id
    later = r.trigger("k8", "settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    assert later["proposal_id"] != first["proposal_id"]
    assert later["started"] is False and later["existing_status"] == "RUNNING"
    assert later["existing_execution"].endswith(":" + first["proposal_id"])
    assert len(d.sfn.list_executions(stateMachineArn=arn)["executions"]) == 1


def test_the_marker_refuses_a_put_whose_scan_predates_the_first_write(world, monkeypatch):
    d, r, _, _ = world
    stale = r._existing_rows()  # read before either put writes
    monkeypatch.setattr(r, "_existing_rows", lambda: stale)
    one = {"tenant_id": "bob", "kit": "k8", "role": "customer", "connected_account_id": "acct_R"}
    two = {"tenant_id": "ann", "kit": "k7", "role": "customer", "connected_account_id": "acct_R"}
    assert r.tenants_put(deploy.parse_rows([one]))["written"] == ["bob"]
    with pytest.raises(DeployError, match="taken meanwhile"):
        r.tenants_put(deploy.parse_rows([two]))  # its scan does not show bob; the marker does
    assert "Item" not in d.ddb.get_item(TableName="tenants", Key={"tenant_id": {"S": "ann"}})
    marker = d.ddb.get_item(TableName="tenants", Key={"tenant_id": {"S": "account#acct_R"}})
    assert marker["Item"]["owner"]["S"] == "bob"


def test_kit_7s_deploy_notes_name_its_operator_row():
    k7 = (DEPLOY / "k7.yaml").read_text()
    readme = (ROOT / "infra" / "README.md").read_text()
    assert "reconcile_portfolio runs as k7:operator" in k7
    assert (
        "--tenant k7:operator" in readme
        and "`reconcile_portfolio` runs as the `k7:operator`" in readme
    )
    doc = load_file(str(DEPLOY / "k7.yaml"))
    assert {"tenant_id": "k7:operator", "kit": "k7", "role": "operator"} in doc["tenants"]


def test_a_finished_run_of_the_same_intent_does_not_block_a_new_start(world):
    d, r, fake, _ = world
    r.apply(substitute(load_file(str(DEPLOY / "k8.yaml")), _k8_vars(fake)))
    arn = _machine(d, "awx-k8-settle_seller_cycle")
    first = r.trigger("k8", "settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    ex = arn.replace(":stateMachine:", ":execution:") + ":" + first["proposal_id"]
    d.sfn.stop_execution(executionArn=ex)
    d.clock = lambda: NOW + timedelta(days=1)
    real = d.sfn.list_executions

    def as_succeeded(**kw):  # an EXPIRED or BLOCKED run ends as a SUCCEEDED execution
        if kw.get("statusFilter") == "SUCCEEDED":
            return real(**{**kw, "statusFilter": "ABORTED"})
        return real(**kw)

    d.sfn.list_executions = as_succeeded
    later = r.trigger("k8", "settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    assert later["started"] is True and later["proposal_id"] != first["proposal_id"]
    assert len(real(stateMachineArn=arn)["executions"]) == 2


def test_a_running_execution_with_unreadable_input_refuses_the_trigger_cleanly(world):
    d, r, fake, _ = world
    r.apply(substitute(load_file(str(DEPLOY / "k8.yaml")), _k8_vars(fake)))
    arn = _machine(d, "awx-k8-settle_seller_cycle")
    d.sfn.start_execution(stateMachineArn=arn, name="junk", input='{"no": "proposal"}')
    with pytest.raises(DeployError, match=r"RUNNING execution .*:junk has unreadable input"):
        r.trigger("k8", "settle_seller_cycle", {"cycle_id": "c1"}, tenant_id="bob")
    assert len(d.sfn.list_executions(stateMachineArn=arn)["executions"]) == 1


def test_kit_6s_deploy_file_carries_three_employers():
    doc = load_file(str(DEPLOY / "k6.yaml"))
    ids = [row["tenant_id"] for row in doc["tenants"]]
    assert ids == ["k6:operator", "k6:employer:a", "k6:employer:b", "k6:employer:c"]
    assert doc["tenants"][3]["connected_account_id"] == "${K6_EMPLOYER_C_ACCT}"
