"""Scrub credentials and tokens out of anything that may be logged or raised.

Two layers: exact replacement of the secret values the client holds (client id, API key,
current bearer token), and pattern rules for anything that looks like an auth header or a
bearer token in provider text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "***"
_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\",\s]+(\s+[^'\",\s]+)?"),
    re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)[^'\",\s]+"),
    re.compile(r"(?i)(x-client-id['\"]?\s*[:=]\s*['\"]?)[^'\",\s]+"),
    re.compile(r"(?i)(['\"]?token['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._\-~+/=]{8,}"),
)
SENSITIVE_HEADERS = frozenset({"authorization", "x-api-key", "x-client-id", "cookie"})


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: set[str] = {s for s in secrets if s and len(s) >= 4}

    def add(self, *secrets: str | None) -> None:
        for s in secrets:
            if s and len(s) >= 4:
                self._secrets.add(s)

    def text(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, MASK)
        for pattern in _PATTERNS:
            value = pattern.sub(lambda m: m.group(1) + MASK, value)
        return value

    def value(self, value: Any) -> Any:
        """Redact recursively through dicts and lists (for error details)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {
                k: (MASK if str(k).lower() in SENSITIVE_HEADERS else self.value(v))
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.value(v) for v in value]
        return value


def safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """A loggable copy of request headers."""
    return {k: (MASK if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}
