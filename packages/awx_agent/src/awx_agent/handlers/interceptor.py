"""AgentCore Gateway REQUEST interceptor: put the verified caller's token into every tools/call.

Gateway Lambda targets receive only the tool arguments and Gateway metadata, not the caller's
identity. This interceptor runs on every Gateway request with passRequestHeaders enabled. For a
`tools/call` it:

1. reads the Authorization header and VERIFIES it in-process (RS256 signature against the user
   pool JWKS, issuer, app client, token_use=access, expiry; awx_agent.jwtverify), so it does
   not depend on the Gateway's inbound authorizer having run first,
2. deletes any IDENTITY_FIELD the model put in the arguments and writes the verified token
   there, for the tool Lambda to verify again before it acts.

A call that fails verification is answered here with a JSON-RPC error and never reaches the
target. Other MCP methods (initialize, tools/list) pass through unchanged. The token is never
logged.
"""

from __future__ import annotations

import copy
import functools
import logging
import os
from collections.abc import Mapping
from typing import Any

from ..identity import IDENTITY_FIELD, IdentityError
from ..jwtverify import CognitoVerifier

log = logging.getLogger(__name__)
_VERSION = "1.0"


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return str(value)
    return None


def _deny(request_id: Any, message: str) -> dict[str, Any]:
    return {
        "interceptorOutputVersion": _VERSION,
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32001, "message": message},
                },
            }
        },
    }


def _pass(body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "interceptorOutputVersion": _VERSION,
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def intercept(event: Mapping[str, Any], *, verifier: CognitoVerifier) -> dict[str, Any]:
    request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body") or {}
    if not isinstance(body, Mapping):  # a JSON-RPC batch (list) or other non-object body
        return _deny(None, "batch or non-object requests are not supported")
    if body.get("method") != "tools/call":
        return _pass(body)
    request_id = body.get("id")
    token = _header(request.get("headers") or {}, "Authorization")
    if not token:
        return _deny(request_id, "no caller token reached the interceptor")
    try:
        verifier.verify(token)
    except IdentityError as exc:
        log.info("tools/call denied: %s", exc)
        return _deny(request_id, "caller not authorised")
    except Exception as exc:  # JWKS fetch failure, timeout, a bug: fail closed, log the type only
        log.warning("tools/call denied: verifier raised %s", type(exc).__name__)
        return _deny(request_id, "caller could not be verified")
    raw = token[7:] if token[:7].lower() == "bearer " else token
    new_body = copy.deepcopy(dict(body))
    params = dict(new_body.get("params") or {})
    arguments = dict(params.get("arguments") or {})
    arguments.pop(IDENTITY_FIELD, None)
    arguments[IDENTITY_FIELD] = raw.strip()
    params["arguments"] = arguments
    new_body["params"] = params
    return _pass(new_body)


@functools.cache
def verifier_from_env() -> CognitoVerifier:
    return CognitoVerifier.for_issuer(
        os.environ["AWX_EXPECTED_ISSUER"], os.environ["AWX_EXPECTED_CLIENT_ID"]
    )


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    try:
        verifier = verifier_from_env()
    except Exception as exc:  # misconfiguration: deny every call rather than error
        log.error("interceptor misconfigured: %s", type(exc).__name__)
        body = ((event.get("mcp") or {}).get("gatewayRequest") or {}).get("body") or {}
        if isinstance(body, Mapping) and body.get("method") != "tools/call":
            return _pass(body)
        request_id = body.get("id") if isinstance(body, Mapping) else None
        return _deny(request_id, "caller could not be verified")
    return intercept(event, verifier=verifier)
