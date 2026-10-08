import base64
import hashlib
import hmac
import json

import pytest
from awx_agent.identity import IdentityError
from awx_agent.jwtverify import CognitoVerifier, JwksCache
from jwt_helpers import (
    CLIENT,
    ISS,
    KID,
    NOW,
    b64int,
    bad_signature_token,
    claims,
    jwks,
    public_pem,
    token,
    verifier,
)


def test_valid_token_yields_identity():
    who = verifier().verify("Bearer " + token(tenant_id="tenant-a"))
    assert (who.sub, who.tenant_id, who.groups) == ("user-requester", "tenant-a", ("requester",))


@pytest.mark.parametrize(
    "tok",
    [
        bad_signature_token(),
        token(exp=NOW - 31),
        token(iat=NOW + 120),
        token(token_use="id"),
        token(client_id=None, aud="other"),
        token(iss=ISS + "x"),
        token(alg="HS256"),
        "a.b",
    ],
)
def test_rejections(tok):
    with pytest.raises(IdentityError):
        verifier().verify(tok)


def test_tampered_payload_fails_signature():
    head, _body, sig = token().split(".")
    _h, evil, _s = token(**{"cognito:groups": ["approver"]}).split(".")
    with pytest.raises(IdentityError, match="signature"):
        verifier().verify(f"{head}.{evil}.{sig}")


def test_jwks_is_cached_and_refetch_on_unknown_kid_is_rate_limited():
    fetches = []
    v = verifier(fetches)
    v.verify(token())
    v.verify(token())
    assert len(fetches) == 1
    with pytest.raises(IdentityError, match="unknown key"):
        v.verify(token(kid="rotated"))
    assert len(fetches) == 1  # fetched within the last minute; not hammered


def test_jwks_ignores_short_and_non_rsa_keys():
    doc = jwks()
    doc["keys"].append({"kty": "EC", "kid": "ec"})
    doc["keys"].append({"kty": "RSA", "kid": "tiny", "n": "AQAB", "e": "AQAB"})
    cache = JwksCache("https://x", fetch=lambda u: doc, clock=lambda: NOW)
    assert cache.key("test-kid").key_size == 2048
    assert "tiny" not in cache._keys


def _forge(header: dict, claims_: dict, sig: bytes = b"") -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    sig_seg = base64.urlsafe_b64encode(sig).rstrip(b"=").decode() or "AA"
    return f"{enc(header)}.{enc(claims_)}.{sig_seg}"


def test_algorithm_confusion_hs256_keyed_with_public_key_fails():
    """HS256 signed with the PEM of the RSA public key must not verify."""
    pem = public_pem()
    header = {"alg": "HS256", "kid": KID, "typ": "JWT"}
    body = claims()
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    signing = f"{enc(header)}.{enc(body)}".encode()
    mac = hmac.new(pem, signing, hashlib.sha256).digest()
    tok = f"{signing.decode()}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    with pytest.raises(IdentityError):
        verifier().verify(tok)
    # Same forgery with the header claiming RS256 fails on the signature, not on alg.
    header["alg"] = "RS256"
    signing = f"{enc(header)}.{enc(body)}".encode()
    mac = hmac.new(pem, signing, hashlib.sha256).digest()
    tok = f"{signing.decode()}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    with pytest.raises(IdentityError, match="signature"):
        verifier().verify(tok)


def test_alg_none_fails():
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    for tok in (
        f"{enc({'alg': 'none', 'kid': KID})}.{enc(claims())}.",
        f"{enc({'alg': 'none', 'kid': KID})}.{enc(claims())}.AA",
    ):
        with pytest.raises(IdentityError):
            verifier().verify(tok)


def test_crit_header_is_refused_even_when_validly_signed():
    with pytest.raises(IdentityError, match="crit"):
        verifier().verify(token(extra_header={"crit": ["exp"], "exp": 1}))


@pytest.mark.parametrize("bad", ["+", "/", "=", "%", "!"])
def test_non_base64url_segment_is_refused(bad):
    head, body, sig = token().split(".")
    for tok in (
        f"{head}{bad}.{body}.{sig}",
        f"{head}.{body}{bad}.{sig}",
        f"{head}.{body}.{sig}{bad}",
    ):
        with pytest.raises(IdentityError, match="compact"):
            verifier().verify(tok)


def test_iat_is_required():
    with pytest.raises(IdentityError, match="iat"):
        verifier().verify(token(iat=None))


def test_jwks_drops_keys_under_2048_bits():
    from cryptography.hazmat.primitives.asymmetric import rsa

    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505 - the point of the test
    pub = small.public_key().public_numbers()
    doc = {
        "keys": [
            {"kty": "RSA", "kid": "small", "alg": "RS256", "n": b64int(pub.n), "e": b64int(pub.e)}
        ]
    }
    cache = JwksCache("https://x", fetch=lambda u: doc, clock=lambda: NOW)
    with pytest.raises(IdentityError, match="unknown key"):
        cache.key("small")


def test_for_issuer_requires_cognito_https():
    with pytest.raises(ValueError, match="issuer"):
        CognitoVerifier.for_issuer("http://evil/", CLIENT)


def test_future_nbf_is_refused_and_present_nbf_must_be_an_int():
    with pytest.raises(IdentityError, match="nbf"):
        verifier().verify(token(nbf=NOW + 120))
    with pytest.raises(IdentityError, match="nbf"):
        verifier().verify(token(nbf="soon"))
    assert verifier().verify(token(nbf=NOW + 10)).sub == "user-requester"  # within leeway


def test_jwks_failure_is_cached_for_30_seconds():
    """An IdP outage refuses at once instead of re-fetching on every call."""
    clock = [float(NOW)]
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("jwks timed out")
        return jwks()

    cache = JwksCache("https://x", fetch=fetch, clock=lambda: clock[0])
    v = CognitoVerifier(issuer=ISS, client_id=CLIENT, jwks=cache, clock=lambda: clock[0])
    with pytest.raises(TimeoutError):
        v.verify(token())
    for _ in range(3):
        with pytest.raises(IdentityError, match="unavailable"):
            v.verify(token())
    assert len(calls) == 1
    clock[0] += 31
    assert v.verify(token()).sub == "user-requester"
    assert len(calls) == 2
