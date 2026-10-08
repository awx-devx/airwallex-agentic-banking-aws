"""Airwallex sandbox credentials, loaded inside the process and never printed.

Every error message here names the source (a path or an environment variable name) and never
a value. AirwallexCredentials redacts both fields in repr and str, so a credential that ends up
in a traceback, a log line or an f-string shows as '***'.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_CREDENTIALS_PATH = Path("~/.config/airwallex-sandbox/credentials.env")
CLIENT_ID_VAR = "AWX_CLIENT_ID"
API_KEY_VAR = "AWX_API_KEY"


class CredentialsUnavailable(Exception):
    """No usable credentials. The message names where we looked, never what we found."""


@dataclass(frozen=True)
class AirwallexCredentials:
    client_id: str
    api_key: str

    def __post_init__(self) -> None:
        if not self.client_id or not self.api_key:
            raise CredentialsUnavailable("client_id and api_key must both be non-empty")

    def __repr__(self) -> str:
        return "AirwallexCredentials(client_id='***', api_key='***')"

    __str__ = __repr__

    def secrets(self) -> tuple[str, str]:
        """Both values, for the redactor only."""
        return (self.client_id, self.api_key)


class CredentialsProvider(Protocol):
    def get(self) -> AirwallexCredentials: ...


def _parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


class EnvFileCredentials:
    """KEY=VALUE file with AWX_CLIENT_ID and AWX_API_KEY (the awx-sandbox format).

    Read once on first get() and cached for the life of the object.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = (path or DEFAULT_CREDENTIALS_PATH).expanduser()
        self._lock = threading.Lock()
        self._cached: AirwallexCredentials | None = None

    def __repr__(self) -> str:
        return f"EnvFileCredentials(path={str(self._path)!r})"

    def get(self) -> AirwallexCredentials:
        with self._lock:
            if self._cached is None:
                try:
                    text = self._path.read_text(encoding="utf-8")
                except OSError:
                    raise CredentialsUnavailable(
                        f"cannot read credentials file {self._path}"
                    ) from None
                values = _parse_env_file(text)
                client_id = values.get(CLIENT_ID_VAR, "")
                api_key = values.get(API_KEY_VAR, "")
                if not client_id or not api_key:
                    raise CredentialsUnavailable(
                        f"{self._path} must set {CLIENT_ID_VAR} and {API_KEY_VAR}"
                    )
                self._cached = AirwallexCredentials(client_id, api_key)
            return self._cached


class EnvVarCredentials:
    """AWX_CLIENT_ID and AWX_API_KEY from the process environment, read on each get()."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ

    def __repr__(self) -> str:
        return "EnvVarCredentials()"

    def get(self) -> AirwallexCredentials:
        env = self._environ if self._environ is not None else os.environ
        client_id = env.get(CLIENT_ID_VAR, "")
        api_key = env.get(API_KEY_VAR, "")
        if not client_id or not api_key:
            raise CredentialsUnavailable(f"set {CLIENT_ID_VAR} and {API_KEY_VAR}")
        return AirwallexCredentials(client_id, api_key)


class StaticCredentials:
    """Fixed credentials, for tests."""

    def __init__(self, credentials: AirwallexCredentials) -> None:
        self._credentials = credentials

    def __repr__(self) -> str:
        return "StaticCredentials(***)"

    def get(self) -> AirwallexCredentials:
        return self._credentials
