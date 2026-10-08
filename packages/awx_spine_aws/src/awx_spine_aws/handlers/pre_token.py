"""Cognito Pre Token Generation V2 trigger: put tenant_id into the ACCESS token.

The approval API and the AgentCore Gateway authorise on the access token, which carries
cognito:groups but not custom attributes. This trigger copies the administrator-set,
immutable custom:tenant_id attribute into a `tenant_id` claim on both tokens. A user without
the attribute gets no claim, so every tenant-scoped API refuses them (401).
"""

from __future__ import annotations

from typing import Any

TENANT_ATTRIBUTE = "custom:tenant_id"
TENANT_CLAIM = "tenant_id"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    attrs = event.get("request", {}).get("userAttributes", {}) or {}
    tenant_id = attrs.get(TENANT_ATTRIBUTE)
    if not tenant_id:
        return event
    claims = {TENANT_CLAIM: tenant_id}
    event.setdefault("response", {})["claimsAndScopeOverrideDetails"] = {
        "idTokenGeneration": {"claimsToAddOrOverride": claims},
        "accessTokenGeneration": {"claimsToAddOrOverride": claims},
    }
    return event
