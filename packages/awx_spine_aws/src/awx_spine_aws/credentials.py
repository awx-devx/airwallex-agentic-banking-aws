"""Airwallex sandbox credentials from AWS Secrets Manager.

The secret is created empty by SpineStack and seeded out of band as JSON:
{"client_id": "...", "api_key": "..."}. Only the reader and adapter Lambdas may read it.
Values are cached for five minutes so a rotation lands without a redeploy. No error message
here ever contains the secret's value.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

CACHE_SECONDS = 300.0


class CredentialsUnavailable(Exception):
    """The secret is missing, unseeded or malformed. The message names the problem only."""


def _credentials_type() -> Any:
    from awx_spine.credentials import AirwallexCredentials

    return AirwallexCredentials


class SecretsManagerCredentials:
    """Implements awx_spine.credentials.CredentialsProvider."""

    def __init__(
        self,
        secret_arn: str,
        *,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        if not secret_arn:
            raise ValueError("secret_arn is required")
        if client is None:
            import boto3

            client = boto3.client("secretsmanager")
        self._arn = secret_arn
        self._client = client
        self._clock = clock
        self._ttl = cache_seconds
        self._lock = threading.Lock()
        self._cached: Any = None
        self._fetched_at = 0.0

    def __repr__(self) -> str:
        return f"SecretsManagerCredentials({self._arn!r})"

    def get(self) -> Any:
        with self._lock:
            if self._cached is not None and self._clock() - self._fetched_at < self._ttl:
                return self._cached
            self._cached = self._fetch()
            self._fetched_at = self._clock()
            return self._cached

    def invalidate(self) -> None:
        """Drop the cache, for example after a 401 that survived a token refresh."""
        with self._lock:
            self._cached = None

    def _fetch(self) -> Any:
        try:
            resp = self._client.get_secret_value(SecretId=self._arn)
        except self._client.exceptions.ResourceNotFoundException:
            raise CredentialsUnavailable("the Airwallex secret does not exist") from None
        text = resp.get("SecretString")
        if not text:
            raise CredentialsUnavailable("the Airwallex secret has not been seeded")
        try:
            data = json.loads(text)
        except ValueError:
            raise CredentialsUnavailable("the Airwallex secret is not JSON") from None
        if not isinstance(data, dict):
            raise CredentialsUnavailable("the Airwallex secret is not a JSON object")
        client_id = data.get("client_id") or data.get("AWX_CLIENT_ID")
        api_key = data.get("api_key") or data.get("AWX_API_KEY")
        if not (isinstance(client_id, str) and client_id and isinstance(api_key, str) and api_key):
            raise CredentialsUnavailable("the Airwallex secret needs client_id and api_key")
        return _credentials_type()(client_id=client_id, api_key=api_key)
