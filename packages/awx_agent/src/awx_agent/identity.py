"""Who is calling a tool, taken from a verified Cognito access token, never from the model.

Lambda target events carry no caller identity of their own. The Gateway's REQUEST interceptor
(`awx_agent.handlers.interceptor`) verifies the caller's access token (signature against the
user pool JWKS, see awx_agent.jwtverify), deletes whatever the model put under IDENTITY_FIELD,
and writes the token itself there. The tool Lambda verifies that token again before acting,
so neither the order of the Gateway's authorizer and interceptor nor a direct invocation of the
tool Lambda can give a forged identity. IDENTITY_FIELD is declared in the Gateway tool schema
as an optional string the server fills, so it is a documented input property and not an
undeclared argument the Gateway might drop.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from awx_common.errors import TenantScopeViolation

IDENTITY_FIELD = "_awx_caller"
PLATFORM_TENANT = "platform"
CONNECTED_ACCOUNT_KITS = frozenset({"k5", "k6", "k7", "k8"})
REQUESTER_GROUP = "requester"
APPROVER_GROUP = "approver"


class IdentityError(Exception):
    """The caller identity is missing, malformed or not allowed to do this."""


@dataclass(frozen=True)
class CallerIdentity:
    sub: str
    username: str
    tenant_id: str | None
    groups: tuple[str, ...]

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> CallerIdentity:
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise IdentityError("token has no sub claim")
        username = claims.get("username") or claims.get("cognito:username") or sub
        # The tenant is the `tenant_id` claim only, as the approval API reads it. The Pre Token
        # Generation trigger writes it from custom:tenant_id; nothing reads custom:tenant_id here.
        tenant = claims.get("tenant_id")
        groups = claims.get("cognito:groups") or claims.get("groups") or ()
        if isinstance(groups, str):
            groups = (groups,)
        if "tenant_id" in claims and (not isinstance(tenant, str) or not tenant):
            raise IdentityError("tenant_id claim must be a non-empty string")
        return cls(
            sub=sub,
            username=str(username),
            tenant_id=tenant,
            groups=tuple(sorted(str(g) for g in groups)),
        )

    def run_tenant(self, kit: str) -> str:
        """The tenant a kit runs as: the claim for kits 5-8, PLATFORM_TENANT for kits 1-4.

        Kits 1-4 move the platform's own money, so they serve platform callers only: a caller
        whose tenant_id claim is not exactly PLATFORM_TENANT (another tenant, with or without a
        tenants-table row, or no claim at all) is refused with TenantScopeViolation. Kits 5-8
        refuse a caller with no tenant claim or the platform claim.
        """
        if kit not in CONNECTED_ACCOUNT_KITS:
            if self.tenant_id != PLATFORM_TENANT:
                raise TenantScopeViolation(
                    f"kit {kit} serves platform callers only; this caller's tenant is not "
                    f"{PLATFORM_TENANT!r}"
                )
            return PLATFORM_TENANT
        if not self.tenant_id or self.tenant_id == PLATFORM_TENANT:
            raise IdentityError(f"kit {kit} needs a tenant_id claim on the caller")
        return self.tenant_id

    def require_group(self, *allowed: str) -> None:
        if not set(allowed) & set(self.groups):
            raise IdentityError(f"caller is not in any of the groups {sorted(allowed)}")


def jwt_claims_unverified(token: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT checking its signature.

    Only for the AgentCore Runtime, whose inbound JWT authorizer verifies the token before the
    request reaches agent code (the Runtime has no unauthenticated path when an authorizer is
    configured). It keys conversation history and grants nothing. Every decision about what a
    caller may do uses awx_agent.jwtverify.CognitoVerifier instead.
    """
    if token.lower().startswith("bearer "):
        token = token[7:]
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise IdentityError("not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise IdentityError("JWT payload is not base64url JSON") from exc
    if not isinstance(claims, dict):
        raise IdentityError("JWT payload is not an object")
    return claims
