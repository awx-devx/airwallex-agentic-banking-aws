"""Test RSA keys, a JWKS document and signed Cognito-shaped access tokens."""

from __future__ import annotations

import base64
import json

from awx_agent.jwtverify import CognitoVerifier, JwksCache
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ISS = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_TEST"
CLIENT = "client123"
NOW = 1_800_000_000
KID = "test-kid"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _seg(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int(n: int) -> str:
    return _seg(n.to_bytes((n.bit_length() + 7) // 8, "big"))


b64int = _int


def public_pem() -> bytes:
    return _KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def jwks() -> dict:
    pub = _KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "alg": "RS256",
                "use": "sig",
                "n": _int(pub.n),
                "e": _int(pub.e),
            }
        ]
    }


def claims(**over) -> dict:
    base = {
        "iss": ISS,
        "token_use": "access",
        "client_id": CLIENT,
        "exp": NOW + 600,
        "iat": NOW - 10,
        "sub": "user-requester",
        "username": "requester1",
        "cognito:groups": ["requester"],
        "tenant_id": "platform",  # kits 1-4 serve platform callers only; tenant_id=None drops it
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def token(*, key=None, kid=KID, alg="RS256", extra_header=None, **over) -> str:
    header = _seg(json.dumps({"alg": alg, "kid": kid, **(extra_header or {})}).encode())
    body = _seg(json.dumps(claims(**over)).encode())
    signing = f"{header}.{body}".encode()
    sig = (key or _KEY).sign(signing, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_seg(sig)}"


def bad_signature_token(**over) -> str:
    return token(key=_OTHER, **over)


def verifier(fetches: list | None = None) -> CognitoVerifier:
    def fetch(url):
        if fetches is not None:
            fetches.append(url)
        return jwks()

    cache = JwksCache(f"{ISS}/.well-known/jwks.json", fetch=fetch, clock=lambda: NOW)
    return CognitoVerifier(issuer=ISS, client_id=CLIENT, jwks=cache, clock=lambda: NOW)
