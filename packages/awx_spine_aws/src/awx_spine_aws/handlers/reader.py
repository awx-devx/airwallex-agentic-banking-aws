"""Reader Lambda: Airwallex GETs only, per-kit allowlists, server-resolved tenant.

Input  {"kit": str (must equal AWX_READER_KIT), "read": ReadRequest, "tenant_id": str or null}
Output {"body": object}

Neither the kit nor the read scope is the caller's choice:

- Kit. Each KitWorkflow invokes its own per-kit reader (SpineStack.reader_for(kit)), whose
  AWX_READER_KIT names the kit from deployment configuration and whose AWX_READ_ALLOWLISTS
  holds only that kit's patterns. A payload naming another kit is refused. IAM grants invoke
  on a per-kit reader only to that kit's step roles, so a kit can never read under another
  kit's allowlist. The agent plane's tool Lambda for a kit invokes that same per-kit reader.
  There is no shared reader: a reader deployed without AWX_READER_KIT refuses every request.
- Mode. It is derived, never read from the payload (a "mode" field is ignored). The tenant is
  resolved server-side from the tenants table. Then, the same routing as the runners (rule 5):
  a request carrying on_behalf_of is a tenant read: bind_read refuses any account but the
  tenant's own, and the path must match read_allowlist. A headerless request in a tenant with
  a connected account is a platform read and must match platform_read_allowlist. A headerless
  request for the platform tenant reads the platform's own account: it must match the kit's
  platform_read_allowlist, or read_allowlist for a kit that declares none (kits 1 to 4,
  platform only), so naming the platform tenant never widens a connected-account kit's reach.
- tenant_id is still supplied by the caller. IAM cannot bind a tenant to a caller: the step
  Lambda passes the tenant the workflow resolved at Start and stored on the proposal, and the
  agent plane's tool Lambda passes the verified Cognito claim. Within its kit's allowlist a
  caller can read any registered tenant; it can never widen the paths or read outside the kit.

The port's read() is a GET; this Lambda has no code path to a write.
"""

from __future__ import annotations

import functools
import json
from typing import Any

from awx_common.errors import AwxError, TenantScopeViolation
from awx_common.kits import KITS
from awx_common.tenancy import bind_read
from awx_common.transport import decode, encode

from .. import codec
from ..credentials import SecretsManagerCredentials
from ..engine import own_account_patterns, path_allowed, read_once
from ..repos import DynamoDbTenantResolver
from ._common import boto_client, env, log


@functools.cache
def _client() -> Any:
    from awx_spine.client import AirwallexClient

    return AirwallexClient(SecretsManagerCredentials(env("AWX_SECRET_ARN")))


def _allowlist(kit: str, mode: str) -> tuple[str, ...]:
    table = json.loads(env("AWX_READ_ALLOWLISTS"))
    return tuple(table.get(kit, {}).get(mode, ()))


def _kit(payload: dict[str, Any]) -> str:
    import os

    fixed = os.environ.get("AWX_READER_KIT")
    if fixed not in KITS:
        raise TenantScopeViolation("this reader has no AWX_READER_KIT; there is no shared reader")
    asked = payload.get("kit")
    if asked not in (None, fixed):
        raise TenantScopeViolation(f"this reader serves {fixed}, not {asked}")
    return fixed


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        return _handle(event)
    except AwxError as exc:
        codec.with_error_fields(exc)  # http_status and details reach the kit step
        raise


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    payload = decode(event)
    kit = _kit(payload)
    req = codec.read_request_from(payload["read"])
    resolver = DynamoDbTenantResolver(env("AWX_TENANTS_TABLE"), client=boto_client("dynamodb"))
    tenant = resolver.resolve(payload.get("tenant_id"), kit=kit)  # this kit's row only
    if tenant.connected_account_id is None:
        if req.on_behalf_of is not None:
            raise TenantScopeViolation("the platform tenant may not send x-on-behalf-of")
        mode = "platform"
        patterns = own_account_patterns(_allowlist(kit, "tenant"), _allowlist(kit, "platform"))
    elif req.on_behalf_of is None:
        mode = "platform"
        patterns = _allowlist(kit, "platform")
    else:
        mode = "tenant"
        req = bind_read(req, tenant)  # refuses any account but the tenant's own
        patterns = _allowlist(kit, "tenant")
    if not path_allowed(patterns, req.path):  # canonical path, full match
        raise TenantScopeViolation(f"{kit} may not read {req.path} ({mode} read)")
    body = read_once(_client(), req)  # the engine is the one retry layer
    log("reader.read", kit=kit, mode=mode, path=req.path, tenant_id=tenant.tenant_id)
    return encode({"body": body})
