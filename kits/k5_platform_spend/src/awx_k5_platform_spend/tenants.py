"""The tenant map and the only paths by which an operation reaches a connected account.

The tenant comes from the verified Cognito claim and the connected account from this map, both
server-side (report P0 "Make tenant context server-owned"). The model never sees or supplies an
`acct_` value. Two binding paths exist because Airwallex has two call styles:

- customer-scoped calls (issuing, simulations, balances) carry `x-on-behalf-of`, set only by
  `awx_common.bind_operation` from the resolved tenant;
- platform-level calls (`connected_account_transfers/create`, `charges/create`) carry the
  account in the body field `awx_common.PLATFORM_BODY_FIELDS` names and are sent as the
  platform with no header, through `awx_common.bind_platform_operation`.

Every bound operation is then checked with `awx_common.check_tenant_scope`, the same
deny-by-default check every runner applies (rule 4).

Both paths first verify the context against the map, and refuse any `acct_` string in the body
that is not the tenant's own account.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from awx_common import (
    PLATFORM_BODY_FIELDS,
    Operation,
    ReadRequest,
    TenantContext,
    bind_operation,
    bind_platform_operation,
    bind_read,
    check_tenant_scope,
)
from awx_common.errors import TenantScopeViolation

PLATFORM_TENANT_ID = "platform"
_ACCOUNT_PREFIX = "acct_"
_ACCOUNT_KEYS = frozenset(
    {"account_id", "connected_account_id", "destination", "source", "on_behalf_of", "owner_id"}
)


@dataclass(frozen=True)
class TenantRecord:
    tenant_id: str
    connected_account_id: str
    display_name: str
    active: bool = True

    def __post_init__(self) -> None:
        if not self.tenant_id or self.tenant_id == PLATFORM_TENANT_ID:
            raise ValueError("tenant_id is required and may not be the platform id")
        if not self.connected_account_id.startswith(_ACCOUNT_PREFIX):
            raise ValueError("connected_account_id must be an acct_ id")

    def context(self) -> TenantContext:
        return TenantContext(self.tenant_id, self.connected_account_id)


class TenantMap:
    """Implements awx_common.TenantResolver. One tenant, one connected account, both unique."""

    def __init__(self, records: Iterable[TenantRecord]) -> None:
        by_id: dict[str, TenantRecord] = {}
        owners: dict[str, str] = {}
        for r in records:
            if r.tenant_id in by_id:
                raise ValueError(f"duplicate tenant {r.tenant_id}")
            if r.connected_account_id in owners:
                raise ValueError("a connected account may belong to one tenant only")
            by_id[r.tenant_id] = r
            owners[r.connected_account_id] = r.tenant_id
        self._by_id = by_id
        self._owners = owners

    def resolve(self, tenant_id: str) -> TenantContext:
        record = self._by_id.get(tenant_id)
        if record is None:
            raise TenantScopeViolation("unknown tenant")
        return record.context()

    def record(self, tenant: TenantContext) -> TenantRecord:
        """The map's record for a context, refusing any context the map did not produce."""
        record = self._by_id.get(tenant.tenant_id)
        if record is None or record.connected_account_id != tenant.connected_account_id:
            raise TenantScopeViolation("tenant context does not match the server-side tenant map")
        return record

    def records(self) -> tuple[TenantRecord, ...]:
        return tuple(self._by_id[t] for t in sorted(self._by_id))

    def tenant_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))


def account_strings(value: Any) -> set[str]:
    """Every `acct_` string anywhere in a JSON-like value."""
    found: set[str] = set()
    if isinstance(value, str):
        if value.startswith(_ACCOUNT_PREFIX):
            found.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            found |= account_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= account_strings(item)
    return found


def assert_body_scope(body: Mapping[str, Any], tenant: TenantContext) -> None:
    stray = account_strings(body) - {tenant.connected_account_id}
    if stray:
        raise TenantScopeViolation("operation body names a connected account outside the tenant")


def bind_customer_operation(op: Operation, tenant: TenantContext, tenants: TenantMap) -> Operation:
    """A customer-scoped call. Refuses the platform context: a customer call without the header
    would run against the platform's own wallet and can still return success (report P0 3)."""
    record = tenants.record(tenant)
    assert_body_scope(op.body, tenant)
    if op.path in PLATFORM_BODY_FIELDS:
        raise TenantScopeViolation(f"{op.path} is a platform movement, not a customer call")
    bound = bind_operation(op, record.context())
    if bound.on_behalf_of != record.connected_account_id:
        raise TenantScopeViolation("customer-scoped operation is missing x-on-behalf-of")
    check_tenant_scope(bound, record.context())
    return bound


def bind_platform_movement(op: Operation, tenant: TenantContext, tenants: TenantMap) -> Operation:
    """A body-bound platform movement (`destination` or `source`, from PLATFORM_BODY_FIELDS).

    Sent as the platform with no header: on connected_account_transfers/create the header would
    make the named account the payer (awx_spine FAKE-FIDELITY.md, security finding)."""
    record = tenants.record(tenant)
    field = PLATFORM_BODY_FIELDS.get(op.path)
    if field is None:
        raise TenantScopeViolation(f"{op.path} is not a body-bound platform endpoint")
    if op.body.get(field) not in (None, record.connected_account_id):
        raise TenantScopeViolation(f"{field} is not the tenant's connected account")
    assert_body_scope(op.body, tenant)
    bound = bind_platform_operation(op, record.context())
    check_tenant_scope(bound, record.context())
    return bound


def customer_read(req: ReadRequest, tenant: TenantContext) -> ReadRequest:
    if tenant.connected_account_id is None:
        raise TenantScopeViolation("customer-scoped read needs a connected account")
    return bind_read(req, tenant)


def redact_accounts(value: Any) -> Any:
    """Strip account identifiers from anything a tool returns to the model."""
    if isinstance(value, Mapping):
        return {
            k: redact_accounts(v)
            for k, v in value.items()
            if k not in _ACCOUNT_KEYS and not (isinstance(v, str) and v.startswith(_ACCOUNT_PREFIX))
        }
    if isinstance(value, list | tuple):
        return [
            redact_accounts(v)
            for v in value
            if not (isinstance(v, str) and v.startswith(_ACCOUNT_PREFIX))
        ]
    return value
