"""Verify a Cognito access token in-process with PyJWT and cryptography.

RS256 only, against the user pool's JWKS, then issuer, app client, token_use and expiry. The
algorithm list is fixed to ["RS256"] and every key is loaded as an RSA public key, so neither
`alg: none` nor algorithm confusion (HS256 keyed with the public key) can pass.

Before PyJWT sees the token: each of the three segments must be strict base64url
(^[A-Za-z0-9_-]+$) and a header carrying `crit` is refused (RFC 7515 s4.1.11; we understand no
extensions). Keys under 2048 bits are dropped when the JWKS loads.

Why verify here rather than trust the Gateway's inbound authorizer: the AgentCore docs do not
say the authorizer runs before a REQUEST interceptor, and a tool Lambda can be invoked directly
by any principal in the account that holds lambda:InvokeFunction. Both checks therefore run
where the identity is used.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from .identity import CallerIdentity, IdentityError

_JWKS_TTL_S = 3600
_MISS_REFETCH_S = 60  # refetch at most once a minute on an unknown kid (key rotation)
_FAILURE_CACHE_S = 30  # after a failed fetch, refuse at once for 30 s instead of re-fetching
_LEEWAY_S = 30
_MIN_MODULUS_BITS = 2048
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
ALGORITHMS = ["RS256"]


def _fetch_json(url: str) -> Mapping[str, Any]:
    if not url.startswith("https://"):
        raise IdentityError("JWKS URL must be https")
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - https only, above
        return json.loads(resp.read())


def _load_key(jwk: Mapping[str, Any]) -> RSAPublicKey | None:
    if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig":
        return None
    if jwk.get("alg", "RS256") != "RS256" or not jwk.get("kid"):
        return None
    try:
        key = RSAAlgorithm.from_jwk(dict(jwk))
    except (jwt.InvalidKeyError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(key, RSAPublicKey) or key.key_size < _MIN_MODULUS_BITS:
        return None
    e = key.public_numbers().e
    if e < 3 or e % 2 == 0:  # cryptography refuses these too; keep the rule visible here
        return None
    return key


@dataclass
class JwksCache:
    """Public keys of one user pool, cached, refetched on expiry or (rate-limited) unknown kid."""

    url: str
    fetch: Callable[[str], Mapping[str, Any]] = _fetch_json
    clock: Callable[[], float] = time.time
    _keys: dict[str, RSAPublicKey] = field(default_factory=dict)
    _fetched_at: float = -1e18
    _failed_at: float = -1e18
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _refresh(self) -> None:
        now = self.clock()
        if now - self._failed_at < _FAILURE_CACHE_S:
            raise IdentityError("the key set is unavailable (recent fetch failed)")
        try:
            doc = self.fetch(self.url)
        except Exception:
            self._failed_at = now  # an IdP outage must not add the fetch timeout to every call
            raise
        keys: dict[str, RSAPublicKey] = {}
        for jwk in doc.get("keys", []):
            key = _load_key(jwk)
            if key is not None:
                keys[str(jwk["kid"])] = key
        self._keys = keys
        self._fetched_at = self.clock()

    def key(self, kid: str) -> RSAPublicKey:
        with self._lock:
            age = self.clock() - self._fetched_at
            if age > _JWKS_TTL_S or (kid not in self._keys and age > _MISS_REFETCH_S):
                self._refresh()
            found = self._keys.get(kid)
        if found is None:
            raise IdentityError("token is signed with an unknown key")
        return found


@dataclass
class CognitoVerifier:
    issuer: str
    client_id: str
    jwks: JwksCache
    clock: Callable[[], float] = time.time

    @classmethod
    def for_issuer(cls, issuer: str, client_id: str, **kw: Any) -> CognitoVerifier:
        if not issuer.startswith("https://cognito-idp.") or issuer.endswith("/"):
            raise ValueError("issuer must be https://cognito-idp.<region>.amazonaws.com/<pool>")
        jwks = JwksCache(f"{issuer}/.well-known/jwks.json", **kw)
        return cls(issuer=issuer, client_id=client_id, jwks=jwks)

    def verify(self, token: str) -> CallerIdentity:
        """Verify and return the caller. Raises IdentityError with a non-sensitive message."""
        if token[:7].lower() == "bearer ":
            token = token[7:]
        token = token.strip()
        parts = token.split(".")
        if len(parts) != 3 or not all(_SEGMENT.match(p) for p in parts):
            raise IdentityError("not a compact JWT")
        try:
            raw = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
            header = json.loads(raw)
        except (binascii.Error, ValueError) as exc:
            raise IdentityError("token header is malformed") from exc
        if not isinstance(header, dict):
            raise IdentityError("token header is malformed")
        if "crit" in header:
            raise IdentityError("token header carries crit; no extensions are understood")
        if header.get("alg") not in ALGORITHMS or not isinstance(header.get("kid"), str):
            raise IdentityError("token must be RS256 with a kid")
        key = self.jwks.key(header["kid"])
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                options={
                    # PyJWT checks the signature; we check every claim below, explicitly.
                    "verify_signature": True,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
        except jwt.InvalidSignatureError as exc:
            raise IdentityError("token signature is invalid") from exc
        except jwt.PyJWTError as exc:
            raise IdentityError("token could not be verified") from exc
        return self.check_claims(claims)

    def check_claims(self, claims: Mapping[str, Any]) -> CallerIdentity:
        now = self.clock()
        if claims.get("iss") != self.issuer:
            raise IdentityError("token issuer is not this deployment's user pool")
        if claims.get("token_use") != "access":
            raise IdentityError("an access token is required")
        # Cognito access tokens carry client_id, not aud; accept aud only as a fallback.
        if claims.get("client_id", claims.get("aud")) != self.client_id:
            raise IdentityError("token was issued to a different app client")
        exp = claims.get("exp")
        if not isinstance(exp, int) or isinstance(exp, bool) or exp + _LEEWAY_S <= now:
            raise IdentityError("token has expired")
        iat = claims.get("iat")
        if not isinstance(iat, int) or isinstance(iat, bool) or iat - _LEEWAY_S > now:
            raise IdentityError("token iat is missing or in the future")
        if "nbf" in claims:
            nbf = claims["nbf"]
            if not isinstance(nbf, int) or isinstance(nbf, bool) or nbf - _LEEWAY_S > now:
                raise IdentityError("token is not valid yet (nbf)")
        return CallerIdentity.from_claims(claims)
