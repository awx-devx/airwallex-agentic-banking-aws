"""x-on-behalf-of validation. An account id is `^acct_[A-Za-z0-9_-]+$`. The client sends the
header whenever on_behalf_of is set, and "acct_", an empty value, a control character, a
header-splitting value and padding are refused by the tenants-table row rule, by
check_tenant_scope and by AirwallexClient (read and execute) before any HTTP call, the login
included. A well-formed id still resolves and is still sent."""

from __future__ import annotations

from dataclasses import replace

import pytest
from awx_common import OperationStatus, ReadRequest
from awx_common.errors import TenantScopeViolation
from awx_common.tenancy import (
    TenantContext,
    check_tenant_scope,
    is_account_id,
    tenant_from_row,
)
from awx_spine.client import AirwallexClient
from awx_spine.testing import ScriptedTransport, creds, login_ok, resp, transfer_op

BAD = ["acct_", "acct_1\nx", "acct_1\r\nX-Evil: 1", " acct_1", "acct_1 ", "acct_1\t", "acct_ä"]
GOOD = "acct_Ab1-_x"
BALANCE = "/api/v1/balances/current"


def client(transport):
    return AirwallexClient(
        creds(), transport=transport, clock=lambda: 1_000.0, sleep=lambda s: None
    )


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_on_behalf_of_never_reaches_the_transport(bad):
    t = ScriptedTransport([])  # any request at all, the login included, fails the test
    c = client(t)
    with pytest.raises(TenantScopeViolation):
        c.read(ReadRequest("/api/v1/balances/current", on_behalf_of=bad))
    result = c.execute(replace(transfer_op(), on_behalf_of=bad))
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "TenantScopeViolation"
    assert t.sent == []


def test_an_on_behalf_of_is_sent_whenever_it_is_set():
    t = ScriptedTransport([login_ok(), resp(200, {"items": []})])
    client(t).read(ReadRequest("/api/v1/balances/current", on_behalf_of="acct_1"))
    assert t.api_calls()[0].headers["x-on-behalf-of"] == "acct_1"


def _client(transport):
    return AirwallexClient(
        creds(), transport=transport, clock=lambda: 1_000.0, sleep=lambda s: None
    )


@pytest.mark.parametrize("bad", BAD)
def test_the_account_shape_refuses(bad):
    assert not is_account_id(bad)


@pytest.mark.parametrize("bad", BAD)
@pytest.mark.parametrize("role", ["customer", "operator", None])
def test_the_row_rule_refuses_a_malformed_account(bad, role):
    with pytest.raises(TenantScopeViolation, match="malformed"):
        tenant_from_row("t", kit="k6", row_kit="k6", account=bad, role=role)


@pytest.mark.parametrize("bad", BAD)
def test_tenant_scope_refuses_a_malformed_on_behalf_of(bad):
    op = replace(transfer_op(), on_behalf_of=bad)
    with pytest.raises(TenantScopeViolation, match="malformed"):
        check_tenant_scope(op, TenantContext("t", GOOD))


@pytest.mark.parametrize("bad", BAD)
def test_the_client_refuses_before_any_http_call(bad):
    t = ScriptedTransport([])  # any request, the login included, fails the test
    c = _client(t)
    with pytest.raises(TenantScopeViolation):
        c.read(ReadRequest(BALANCE, on_behalf_of=bad))
    result = c.execute(replace(transfer_op(), on_behalf_of=bad))
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "TenantScopeViolation"
    assert t.sent == []


def test_a_well_formed_id_still_resolves_and_is_still_sent():
    t = tenant_from_row("t", kit="k6", row_kit="k6", account=GOOD, role="customer")
    assert t.connected_account_id == GOOD
    check_tenant_scope(replace(transfer_op(), on_behalf_of=GOOD), t)
    transport = ScriptedTransport([login_ok(), resp(200, {"items": []})])
    _client(transport).read(ReadRequest(BALANCE, on_behalf_of=GOOD))
    assert transport.api_calls()[0].headers["x-on-behalf-of"] == GOOD
