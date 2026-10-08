"""Tenant binding and the deny-by-default scope check every runner applies (rule 4)."""

from dataclasses import replace
from decimal import Decimal

import pytest
from awx_common import Operation, TenantContext, bind_operation, derive_request_id
from awx_common.errors import TenantScopeViolation
from awx_common.tenancy import PLATFORM_BODY_FIELDS, bind_platform_operation, check_tenant_scope

PLATFORM = TenantContext("platform")
SELLER = TenantContext("seller-1", connected_account_id="acct_1")


def op(path="/api/v1/transfers/create", **body):
    rid = derive_request_id("k8", f"{path}:1")
    return Operation(
        kit="k8",
        name=path.rsplit("/", 2)[-2] + ".create",
        method="POST",
        path=path,
        body={"request_id": rid, "amount": Decimal("1.00"), **body},
        request_id=rid,
        business_key=f"{path}:1",
        intent_hash="d" * 64,
    )


CHARGE = "/api/v1/charges/create"
CAT = "/api/v1/connected_account_transfers/create"


def test_map_is_read_only_and_covers_both_endpoints():
    assert dict(PLATFORM_BODY_FIELDS) == {CHARGE: "source", CAT: "destination"}
    with pytest.raises(TypeError):
        PLATFORM_BODY_FIELDS["/api/v1/x"] = "y"


def test_platform_context():
    check_tenant_scope(op(), PLATFORM)
    with pytest.raises(TenantScopeViolation, match="x-on-behalf-of"):
        check_tenant_scope(replace(op(), on_behalf_of="acct_1"), PLATFORM)
    with pytest.raises(TenantScopeViolation, match="context of its tenant"):
        check_tenant_scope(op(CHARGE, source="acct_1"), PLATFORM)


def test_connected_account_context_accepts_only_bound_operations():
    check_tenant_scope(bind_operation(op(), SELLER), SELLER)
    check_tenant_scope(bind_platform_operation(op(CAT), SELLER), SELLER)
    check_tenant_scope(bind_platform_operation(op(CHARGE), SELLER), SELLER)
    with pytest.raises(TenantScopeViolation, match="not bound"):
        check_tenant_scope(op(), SELLER)  # the kit forgot to bind it
    with pytest.raises(TenantScopeViolation, match="not bound"):
        check_tenant_scope(replace(op(), on_behalf_of="acct_2"), SELLER)
    with pytest.raises(TenantScopeViolation, match="must not send"):
        check_tenant_scope(replace(op(CAT, destination="acct_1"), on_behalf_of="acct_1"), SELLER)
    with pytest.raises(TenantScopeViolation, match="not the tenant's account"):
        check_tenant_scope(op(CAT, destination="acct_2"), SELLER)
    with pytest.raises(TenantScopeViolation, match="not the tenant's account"):
        check_tenant_scope(op(CAT), SELLER)


def test_bind_platform_operation_uses_the_map():
    bound = bind_platform_operation(op(CHARGE), SELLER)
    assert bound.body["source"] == "acct_1"
    assert bound.on_behalf_of is None
    assert (
        bind_platform_operation(op(CAT), SELLER, body_field="destination").body["destination"]
        == "acct_1"
    )
    with pytest.raises(TenantScopeViolation, match="names the account in"):
        bind_platform_operation(op(CAT), SELLER, body_field="source")
    with pytest.raises(TenantScopeViolation, match="not a platform body-bound endpoint"):
        bind_platform_operation(op(), SELLER)


def platform_op(**body):
    rid = derive_request_id("k8", "payout:seller-1:cycle-41")
    return Operation(
        kit="k8",
        name="connected_account_transfers.create",
        method="POST",
        path="/api/v1/connected_account_transfers/create",
        body={"request_id": rid, "amount": Decimal("10.00"), **body},
        request_id=rid,
        business_key="payout:seller-1:cycle-41",
        intent_hash="c" * 64,
    )


def test_platform_binding_sets_body_field_and_never_the_header():
    tenant = TenantContext(tenant_id="seller-1", connected_account_id="acct_1")
    bound = bind_platform_operation(platform_op(), tenant, body_field="destination")
    assert bound.body["destination"] == "acct_1"
    assert bound.on_behalf_of is None
    same = bind_platform_operation(
        platform_op(destination="acct_1"), tenant, body_field="destination"
    )
    assert same.body["destination"] == "acct_1"
    with pytest.raises(TenantScopeViolation, match="different"):
        bind_platform_operation(platform_op(destination="acct_2"), tenant, body_field="destination")
    with pytest.raises(TenantScopeViolation, match="x-on-behalf-of"):
        bind_platform_operation(
            replace(platform_op(), on_behalf_of="acct_1"), tenant, body_field="destination"
        )
    with pytest.raises(TenantScopeViolation, match="connected account"):
        bind_platform_operation(platform_op(), TenantContext("platform"), body_field="destination")
