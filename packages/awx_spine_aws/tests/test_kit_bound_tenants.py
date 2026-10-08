"""Kit-bound tenants rows: each belongs to one kit; one with no account is an operator.

Covers the resolver rules, the DynamoDB resolver (moto), malformed connected_account_id values
(refused on every role, never read as "no account"), the engine's and kit step's refusal of
another kit's tenant, and child tenants in their parent's kit.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from awx_common import Origin
from awx_common.errors import TenantScopeViolation
from awx_common.tenancy import (
    StaticTenantResolver,
    TenantContext,
    TenantRow,
    check_tenant_kit,
    check_tenant_scope,
    resolve_in_kit,
)
from awx_common.transport import encode
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import codec, engine
from awx_spine_aws.handlers import kit_step
from awx_spine_aws.local_runner import run_kit_workflow
from awx_spine_aws.repos import DynamoDbTenantResolver

import toy

# --- the deployed resolver (moto) ----------------------------------------------------------------


def _row(db, tenant_id, **attrs):
    item = {"tenant_id": {"S": tenant_id}}
    item.update({k: {"S": v} for k, v in attrs.items()})
    db.put_item(TableName="tenants", Item=item)


@pytest.fixture
def ddb(aws):
    _row(aws.db, "k6:operator", kit="k6", role="operator")
    _row(aws.db, "k6:employer:a", kit="k6", role="customer", connected_account_id="acct_a")
    _row(aws.db, "k7:borrower", kit="k7", connected_account_id="acct_b")  # role derived
    _row(aws.db, "no-kit", connected_account_id="acct_c")
    _row(aws.db, "no-kit-op", role="operator")
    _row(aws.db, "k6:no-role", kit="k6")  # no account and no role
    _row(aws.db, "k6:op-with-acct", kit="k6", role="operator", connected_account_id="acct_d")
    _row(aws.db, "k6:odd-role", kit="k6", role="admin", connected_account_id="acct_e")
    return DynamoDbTenantResolver("tenants", client=aws.db)


def test_the_dynamodb_resolver_resolves_rows_of_the_calling_kit(ddb):
    op = ddb.resolve("k6:operator", kit="k6")
    assert (op.connected_account_id, op.kit, op.role) == (None, "k6", "operator")
    emp = ddb.resolve("k6:employer:a", kit="k6")
    assert (emp.connected_account_id, emp.kit, emp.role) == ("acct_a", "k6", "customer")
    assert ddb.resolve("k7:borrower", kit="k7").role == "customer"


@pytest.mark.parametrize(
    ("tenant_id", "kit", "why"),
    [
        ("k6:operator", "k7", "another kit"),  # one kit's operator in another kit
        ("k6:operator", "k5", "another kit"),
        ("k6:employer:a", "k8", "another kit"),
        ("no-kit", "k6", "names no kit"),  # kit attribute missing: fail closed
        ("no-kit-op", "k7", "names no kit"),
        ("k6:no-role", "k6", "must be an operator"),
        ("k6:op-with-acct", "k6", "cannot be 'operator'"),
        ("k6:odd-role", "k6", "cannot be 'admin'"),
        ("unknown", "k6", "unknown tenant"),
        ("platform", "k6", "no platform tenant"),
        ("platform", "k8", "no platform tenant"),
    ],
)
def test_the_dynamodb_resolver_refuses(ddb, tenant_id, kit, why):
    with pytest.raises(TenantScopeViolation, match=why):
        ddb.resolve(tenant_id, kit=kit)


def test_platform_still_resolves_for_kits_1_to_4(ddb):
    for kit in ("k1", "k2", "k3", "k4"):
        t = ddb.resolve("platform", kit=kit)
        assert (t.tenant_id, t.connected_account_id, t.kit) == ("platform", None, kit)


def test_the_reader_lambda_resolves_in_its_own_kit(aws, monkeypatch):
    from awx_common.ops import ReadRequest
    from awx_common.transport import encode
    from awx_spine_aws.handlers import reader

    _row(aws.db, "k6:operator", kit="k6", role="operator")
    event = encode(
        {"kit": "k3", "read": ReadRequest("/api/v1/balances/current"), "tenant_id": "k6:operator"}
    )
    with pytest.raises(TenantScopeViolation, match="another kit"):
        reader.handler(event, None)
    assert aws.port.reads == []


# --- malformed accounts --------------------------------------------------------------------------


MALFORMED = {
    "empty": {"S": ""},
    "platform": {"S": "platform"},
    "whitespace": {"S": "   "},
    "padded": {"S": " acct_x "},
    "number": {"N": "12345"},
    "null": {"NULL": True},
}


@pytest.mark.parametrize("role", ["customer", "operator", None])
@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_a_malformed_account_refuses_the_row_on_every_role(aws, shape, role):
    item = {
        "tenant_id": {"S": "k6:bad"},
        "kit": {"S": "k6"},
        "connected_account_id": MALFORMED[shape],
    }
    if role is not None:
        item["role"] = {"S": role}
    aws.db.put_item(TableName="tenants", Item=item)
    with pytest.raises(TenantScopeViolation, match="malformed connected_account_id"):
        DynamoDbTenantResolver("tenants", client=aws.db).resolve("k6:bad", kit="k6")


def test_a_well_formed_account_still_resolves(aws):
    aws.db.put_item(
        TableName="tenants",
        Item={
            "tenant_id": {"S": "k6:employer:a"},
            "kit": {"S": "k6"},
            "role": {"S": "customer"},
            "connected_account_id": {"S": "acct_a"},
        },
    )
    t = DynamoDbTenantResolver("tenants", client=aws.db).resolve("k6:employer:a", kit="k6")
    assert (t.connected_account_id, t.role) == ("acct_a", "customer")


@pytest.mark.parametrize("account", ["", "platform", "  ", 7])
def test_a_test_resolver_refuses_a_malformed_account_too(account):
    r = StaticTenantResolver({"t": account}, kit="k6")
    with pytest.raises(TenantScopeViolation, match="malformed"):
        r.resolve("t", kit="k6")


def test_a_customer_context_needs_an_acct_account():
    with pytest.raises(TenantScopeViolation, match="acct_"):
        check_tenant_kit(TenantContext("t", "", kit="k6", role="customer"), "k6")


@pytest.mark.parametrize("bad", ["", "platform", " acct_1"])
def test_tenant_scope_refuses_an_empty_or_malformed_on_behalf_of(bad):
    op = toy.fee_op()
    with pytest.raises(TenantScopeViolation, match="malformed"):
        check_tenant_scope(replace(op, on_behalf_of=bad), TenantContext("t", bad))
    with pytest.raises(TenantScopeViolation, match="malformed"):
        check_tenant_scope(replace(op, on_behalf_of=bad), TenantContext("t", "acct_1"))


# --- test resolvers ------------------------------------------------------------------------------


def test_a_plain_test_entry_binds_to_its_resolvers_kit():
    r = StaticTenantResolver({"t": "acct_1", "op": None}, kit="k6")
    assert r.resolve("t", kit="k6").role == "customer"
    assert r.resolve("op", kit="k6").role == "operator"
    with pytest.raises(TenantScopeViolation, match="another kit"):
        r.resolve("op", kit="k7")


def test_a_tenant_row_binds_to_its_own_kit():
    r = StaticTenantResolver({"x": TenantRow("k6", None, "operator")}, kit="k7")
    with pytest.raises(TenantScopeViolation, match="another kit"):
        r.resolve("x", kit="k7")
    assert r.resolve("x", kit="k6").kit == "k6"
    with pytest.raises(TenantScopeViolation, match="must be an operator"):
        StaticTenantResolver({"y": TenantRow("k6", None, "customer")}).resolve("y", kit="k6")


def test_a_kit_less_resolver_binds_to_the_calling_kit():
    class Old:
        def resolve(self, tenant_id):
            return TenantContext(tenant_id, "acct_1")

    t = resolve_in_kit(Old(), "t", "k5")
    assert (t.kit, t.role) == ("k5", "customer")


def test_a_resolver_that_returns_another_kits_tenant_is_refused():
    class Lying:
        def resolve(self, tenant_id, *, kit):
            return TenantContext(tenant_id, None, kit="k6", role="operator")

    with pytest.raises(TenantScopeViolation, match="another kit"):
        resolve_in_kit(Lying(), "t", "k7")


def test_kit_and_role_travel_in_the_run_payload_but_not_in_equality():
    t = TenantContext("t", "acct", kit="k6", role="customer")
    assert (
        codec.tenant_from(
            {"tenant_id": "t", "connected_account_id": "acct", "kit": "k6", "role": "customer"}
        ).kit
        == "k6"
    )
    assert t == TenantContext("t", "acct")


# --- the engine, the runner and the kit step -----------------------------------------------------


def _run(spec, tenant, **kw):
    clock = toy.Clock()
    return run_kit_workflow(
        spec,
        kw.pop("raw", toy.RAW),
        proposed_by="requester-sub",
        tenant=tenant,
        port=kw.pop("port", toy.ToyPort()),
        store=InMemoryOperationStore(),
        decide=kw.pop("decide", toy.no_decision),
        now=clock.now,
        sleep=clock.sleep,
        origin=Origin.SCHEDULE,
        **kw,
    )


def test_a_tenant_resolved_in_another_kit_never_reaches_policy():
    calls: list = []
    other = StaticTenantResolver({"k6:operator": None}, kit="k6").resolve("k6:operator", kit="k6")
    port = toy.ToyPort()
    with pytest.raises(TenantScopeViolation, match="another kit"):
        _run(toy.toy_spec(calls=calls), other, port=port)  # the toy kit is k3
    assert calls == [] and port.reads == [] and port.sends == []


def test_engine_start_refuses_another_kits_tenant():
    spec = toy.toy_spec()
    t = TenantContext("t", "acct_1")
    out = _run(spec, t)
    execution = engine.execution_input(
        out.proposal,
        engine.PolicyDecision(out.proposal.risk_tier, out.proposal.reasons),
        toy.RAW,
        "t",
    )
    with pytest.raises(TenantScopeViolation, match="another kit"):
        engine.start(
            spec, execution, tenant=TenantContext("t", "acct_1", kit="k6", role="customer")
        )


def test_a_child_tenant_must_be_in_its_parents_kit():
    tenants = StaticTenantResolver(
        {"emp-1": "acct_1", "emp-2": TenantRow("k6", "acct_2", "customer")}, kit="k3"
    )
    spec = toy.fan_out_spec(
        child_tenant=lambda intent: {"ben_1": "emp-1", "ben_2": "emp-2"}[intent["payee"]]
    )
    port = toy.ToyPort()
    out = _run(
        spec,
        TenantContext("platform"),
        raw={"payees": ["ben_1", "ben_2"], "amount": "10.00"},
        decide=toy.approve_by("approver"),
        tenants=tenants,
        port=port,
    )
    assert out.state == "FAILED"
    assert "another kit" in out.detail
    assert port.sends == []


def test_kit_step_start_refuses_a_tenant_not_resolved_in_its_kit(aws, testkit, monkeypatch):
    """Start asserts the run tenant's kit is the root spec's kit, never None."""
    lenv = kit_step.LambdaEnv()
    spec = kit_step.root_spec()
    tenant = lenv.resolve_tenant("tenant-a")  # the harness row, kit k3
    proposal, decision = engine.propose(
        spec,
        (),
        toy.RAW,
        proposed_by="requester-sub",
        tenant=tenant,
        env=lenv,
        origin=Origin.SCHEDULE,
    )
    event = encode(
        {
            "phase": "start",
            "execution": engine.execution_input(proposal, decision, toy.RAW, "tenant-a"),
            "execution_name": proposal.proposal_id,
        }
    )
    monkeypatch.setattr(
        kit_step.LambdaEnv, "resolve_tenant", lambda self, tid: TenantContext(tid, "acct_A")
    )
    with pytest.raises(TenantScopeViolation, match="not resolved in this kit"):
        kit_step.handler(event, None)
