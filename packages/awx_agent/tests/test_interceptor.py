from awx_agent.handlers.interceptor import intercept
from awx_agent.identity import IDENTITY_FIELD
from jwt_helpers import NOW, bad_signature_token, token, verifier


def _event(method="tools/call", tok=None, arguments=None):
    headers = {"authorization": f"Bearer {tok}"} if tok else {}
    body = {"jsonrpc": "2.0", "id": 7, "method": method}
    if method == "tools/call":
        body["params"] = {"name": "k1___read_balances", "arguments": arguments or {}}
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {"gatewayRequest": {"headers": headers, "body": body}},
    }


def _run(e):
    return intercept(e, verifier=verifier())


def test_writes_verified_token_and_overwrites_model_supplied_value():
    tok = token()
    out = _run(_event(tok=tok, arguments={"x": "1", IDENTITY_FIELD: "model-made-this-up"}))
    args = out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert args == {"x": "1", IDENTITY_FIELD: tok}


def test_list_tools_passes_through():
    e = _event(method="tools/list")
    assert _run(e)["mcp"]["transformedGatewayRequest"]["body"] == e["mcp"]["gatewayRequest"]["body"]


def _denied(out):
    assert "transformedGatewayRequest" not in out["mcp"]
    return out["mcp"]["transformedGatewayResponse"]["body"]["error"]["code"] == -32001


def test_denies_bad_signature_even_with_perfect_claims():
    """Does not rely on the Gateway authorizer having run before the interceptor."""
    assert _denied(_run(_event(tok=bad_signature_token())))


def test_denies_missing_token_and_wrong_claims():
    for tok in (
        None,
        token(iss="https://cognito-idp.ap-southeast-2.amazonaws.com/other"),
        token(client_id="other"),
        token(token_use="id"),
        token(exp=NOW - 60),
        token(kid="unknown-kid"),
        token(alg="none"),
    ):
        assert _denied(_run(_event(tok=tok)))


def test_verifier_crash_or_jwks_timeout_is_an_explicit_deny(caplog):
    """Any exception denies; only the exception type is logged."""
    import logging
    import urllib.error

    from awx_agent.jwtverify import CognitoVerifier, JwksCache
    from jwt_helpers import CLIENT, ISS

    def boom(url):
        raise urllib.error.URLError("SECRET host detail")

    cache = JwksCache(f"{ISS}/.well-known/jwks.json", fetch=boom, clock=lambda: NOW)
    v = CognitoVerifier(issuer=ISS, client_id=CLIENT, jwks=cache, clock=lambda: NOW)
    with caplog.at_level(logging.INFO):
        out = intercept(_event(tok=token()), verifier=v)
    assert _denied(out)
    assert "URLError" in caplog.text
    assert "SECRET" not in caplog.text
    assert "SECRET" not in str(out)


def test_misconfigured_interceptor_denies_tool_calls_and_passes_listing(monkeypatch):
    from awx_agent.handlers import interceptor

    monkeypatch.delenv("AWX_EXPECTED_ISSUER", raising=False)
    interceptor.verifier_from_env.cache_clear()
    assert _denied(interceptor.handler(_event(tok=token()), None))
    listing = interceptor.handler(_event(method="tools/list"), None)
    assert "transformedGatewayRequest" in listing["mcp"]


def test_a_batch_or_non_object_body_is_denied_not_raised():
    """A JSON-RPC batch could carry tools/call; deny it explicitly."""
    for body in (
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"arguments": {}}}],
        "tools/call",
        7,
    ):
        e = {
            "mcp": {
                "gatewayRequest": {"headers": {"authorization": f"Bearer {token()}"}, "body": body}
            }
        }
        out = _run(e)
        assert _denied(out)
