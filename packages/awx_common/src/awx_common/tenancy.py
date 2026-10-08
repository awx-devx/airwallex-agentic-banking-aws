"""Server-owned tenant context for connected-account kits (5 to 8)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Protocol

from .errors import TenantScopeViolation
from .ops import Operation, ReadRequest

# Platform money movements that name the connected account in a body field instead of
# x-on-behalf-of. Fields per the guide's "Platform money movement" section, confirmed in the
# sandbox (awx_spine FAKE-FIDELITY.md); this map is the only place they change.
PLATFORM_BODY_FIELDS = MappingProxyType(
    {
        "/api/v1/charges/create": "source",
        "/api/v1/connected_account_transfers/create": "destination",
    }
)


@dataclass(frozen=True)
class TenantContext:
    """tenant_id comes from the verified Cognito claim. connected_account_id is looked up
    server-side; None means the platform's own Airwallex account.

    `kit` is the kit the tenants-table row belongs to and `role` its role (ROLE_OPERATOR
    for a row with no connected account, ROLE_CUSTOMER for one with an account). Both are set
    by a resolver and are None on a context a kit or a test builds by hand. They are not part of
    equality, so two contexts compare by tenant and account only; the resolvers and the engine
    check them."""

    tenant_id: str
    connected_account_id: str | None = None
    kit: str | None = field(default=None, compare=False)
    role: str | None = field(default=None, compare=False)


# Kits whose runs are tenants with a connected account (or their kit's operator), and the
# roles a tenants-table row can have. Every row belongs to one kit and one role.
CONNECTED_ACCOUNT_KITS = frozenset({"k5", "k6", "k7", "k8"})
ROLE_OPERATOR = "operator"
ROLE_CUSTOMER = "customer"
PLATFORM_TENANT_ID = "platform"


@dataclass(frozen=True)
class TenantRow:
    """One tenants-table row as a test resolver holds it: the kit it belongs to, its
    connected account (None for an operator) and its role (derived from the account if
    None)."""

    kit: str
    connected_account_id: str | None = None
    role: str | None = None


class TenantResolver(Protocol):
    def resolve(self, tenant_id: str, *, kit: str) -> TenantContext:
        """Look up the tenant server-side for the calling kit. Raises TenantScopeViolation for
        an unknown tenant, a row of another kit or without a kit, and a row whose role does
        not fit its account."""
        ...


ACCOUNT_ID = re.compile(r"acct_[A-Za-z0-9_-]+")


def is_account_id(value: object) -> bool:
    """A connected account id as the provider issues them, `^acct_[A-Za-z0-9_-]+$`.
    It refuses "acct_", padding and every control character, since the value
    becomes an HTTP header (x-on-behalf-of)."""
    return isinstance(value, str) and ACCOUNT_ID.fullmatch(value) is not None


def tenant_from_row(
    tenant_id: str,
    *,
    kit: str,
    row_kit: str | None,
    account: object,
    role: str | None,
) -> TenantContext:
    """The one rule every resolver applies to a tenants-table row.

    `account` is None only when the row has no account attribute at all. Any value that is
    present must be an `acct_...` id (is_account_id): empty, whitespace, "platform", another
    type or another shape refuses the row on every role, and is never read as "no account"
    (that would run a customer's money on the platform's account)."""
    if not row_kit:
        raise TenantScopeViolation("tenant row names no kit")
    if row_kit != kit:
        raise TenantScopeViolation(f"tenant belongs to another kit, not {kit}")
    if account is not None and not is_account_id(account):
        raise TenantScopeViolation("tenant row has a malformed connected_account_id")
    if account is None:
        if role != ROLE_OPERATOR:
            raise TenantScopeViolation("a tenant with no connected account must be an operator")
    elif role not in (None, ROLE_CUSTOMER):
        raise TenantScopeViolation(f"a tenant with a connected account cannot be {role!r}")
    return TenantContext(
        tenant_id,
        account,  # type: ignore[arg-type]  # None or an acct_ id, checked above
        kit=kit,
        role=ROLE_OPERATOR if account is None else ROLE_CUSTOMER,
    )


def platform_tenant(kit: str) -> TenantContext:
    """The platform tenant resolves only for kits 1 to 4; kits 5 to 8 run as a tenant."""
    if kit in CONNECTED_ACCOUNT_KITS:
        raise TenantScopeViolation(f"kit {kit} has no platform tenant")
    return TenantContext(PLATFORM_TENANT_ID, None, kit=kit, role=ROLE_OPERATOR)


def check_tenant_kit(tenant: TenantContext, kit: str) -> None:
    """A resolved tenant must be the calling kit's. A hand-built context (kit None) is
    not checked here: it never came from the tenants table."""
    if tenant.kit is not None and tenant.kit != kit:
        raise TenantScopeViolation(f"tenant belongs to another kit, not {kit}")
    if tenant.role == ROLE_OPERATOR and tenant.connected_account_id is not None:
        raise TenantScopeViolation("an operator tenant has no connected account")
    if tenant.role == ROLE_CUSTOMER and not is_account_id(tenant.connected_account_id):
        raise TenantScopeViolation("a customer tenant needs an acct_ connected account")


def resolve_in_kit(resolver: Any, tenant_id: str, kit: str) -> TenantContext:
    """Resolve through any resolver for the calling kit.

    A resolver whose `resolve` takes `kit` applies the row rule itself. A resolver whose
    `resolve` takes only the tenant id (a kit test's own resolver, `resolve(tenant_id)`) is
    treated like a plain test entry: its tenants bind to the calling kit, with the role
    derived from the account. The result is checked either way."""
    method = resolver.resolve
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        params = {}
    takes_kit = "kit" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    tenant = method(tenant_id, kit=kit) if takes_kit else method(tenant_id)
    if not isinstance(tenant, TenantContext):
        raise TenantScopeViolation("the resolver did not return a TenantContext")
    if tenant.kit is None:
        role = ROLE_OPERATOR if tenant.connected_account_id is None else ROLE_CUSTOMER
        tenant = replace(tenant, kit=kit, role=tenant.role or role)
    check_tenant_kit(tenant, kit)
    return tenant


class StaticTenantResolver:
    """The test resolver. An entry is either a plain connected account (str, or None
    for an operator), which binds to `kit` (or, with kit None, to whichever kit asks), or a
    TenantRow, which binds to its own kit. "platform" resolves for kits 1 to 4 only."""

    def __init__(
        self,
        tenants: Mapping[str, str | TenantRow | None] | None = None,
        *,
        kit: str | None = None,
    ) -> None:
        self.tenants = dict(tenants or {})
        self.kit = kit

    def resolve(self, tenant_id: str | None, *, kit: str) -> TenantContext:
        tenant_id = tenant_id or PLATFORM_TENANT_ID
        if tenant_id == PLATFORM_TENANT_ID and tenant_id not in self.tenants:
            return platform_tenant(kit)
        if tenant_id not in self.tenants:
            raise TenantScopeViolation("unknown tenant")
        entry = self.tenants[tenant_id]
        if isinstance(entry, TenantRow):
            row_kit, account, role = entry.kit, entry.connected_account_id, entry.role
        elif isinstance(entry, TenantContext):
            row_kit, account, role = (
                entry.kit or self.kit or kit,
                entry.connected_account_id,
                entry.role,
            )
        else:
            row_kit, account, role = self.kit or kit, entry, None
        if role is None:
            role = ROLE_OPERATOR if account is None else ROLE_CUSTOMER
        return tenant_from_row(tenant_id, kit=kit, row_kit=row_kit, account=account, role=role)


def bind_operation(op: Operation, tenant: TenantContext) -> Operation:
    """Set x-on-behalf-of from the resolved tenant; refuse an op that already names another."""
    if op.on_behalf_of not in (None, tenant.connected_account_id):
        raise TenantScopeViolation("operation names a different connected account than the tenant")
    return replace(op, on_behalf_of=tenant.connected_account_id)


def bind_read(req: ReadRequest, tenant: TenantContext) -> ReadRequest:
    if req.on_behalf_of not in (None, tenant.connected_account_id):
        raise TenantScopeViolation("read names a different connected account than the tenant")
    return replace(req, on_behalf_of=tenant.connected_account_id)


def bind_platform_operation(
    op: Operation, tenant: TenantContext, *, body_field: str | None = None
) -> Operation:
    """Bind a platform money movement that names the connected account in its body.

    charges/create and connected_account_transfers/create run as the platform and carry the
    tenant's connected account in a body field (PLATFORM_BODY_FIELDS), not in x-on-behalf-of.
    The field comes from that map; a body_field argument must agree with it. The adapter never
    binds tenants; kit step code does, with this or bind_operation.
    """
    expected = PLATFORM_BODY_FIELDS.get(op.path)
    if expected is None:
        raise TenantScopeViolation(f"{op.path} is not a platform body-bound endpoint")
    if body_field is not None and body_field != expected:
        raise TenantScopeViolation(
            f"{op.path} names the account in {expected!r}, not {body_field!r}"
        )
    if tenant.connected_account_id is None:
        raise TenantScopeViolation("platform operation needs a tenant with a connected account")
    if op.on_behalf_of is not None:
        raise TenantScopeViolation("platform operation must not send x-on-behalf-of")
    current = op.body.get(expected)
    if current not in (None, tenant.connected_account_id):
        raise TenantScopeViolation(f"body {expected} names a different connected account")
    return replace(op, body={**op.body, expected: tenant.connected_account_id})


def check_tenant_scope(op: Operation, tenant: TenantContext) -> None:
    """Deny-by-default check every runner applies to every operation before it executes.

    A platform tenant (no connected account) may send only operations with no on_behalf_of
    to endpoints outside PLATFORM_BODY_FIELDS; a body-bound platform movement must run in the
    context of the tenant it moves money for. A connected-account tenant's operation must
    either carry on_behalf_of equal to the tenant's connected account (and not be a
    body-bound endpoint), or be a body-bound endpoint with no on_behalf_of and its body field
    equal to that account. Anything else raises TenantScopeViolation, so an operation a kit
    forgot to bind can never act on the platform's own account by accident.
    """
    field = PLATFORM_BODY_FIELDS.get(op.path)
    account = tenant.connected_account_id
    if op.on_behalf_of is not None and not is_account_id(op.on_behalf_of):
        raise TenantScopeViolation("operation names a malformed x-on-behalf-of")
    if account is not None and not is_account_id(account):
        raise TenantScopeViolation("tenant has a malformed connected account")
    if account is None:
        if op.on_behalf_of is not None:
            raise TenantScopeViolation("platform context sent x-on-behalf-of")
        if field is not None:
            raise TenantScopeViolation(f"{op.path} must run in the context of its tenant")
        return
    if field is not None:
        if op.on_behalf_of is not None:
            raise TenantScopeViolation(f"{op.path} must not send x-on-behalf-of")
        if op.body.get(field) != account:
            raise TenantScopeViolation(f"{op.path} body {field} is not the tenant's account")
        return
    if op.on_behalf_of != account:
        raise TenantScopeViolation("operation is not bound to the tenant's connected account")
