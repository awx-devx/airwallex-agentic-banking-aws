"""Canonical JSON, intent hashes and deterministic request IDs.

An approval binds to intent_hash(...). An Airwallex write is idempotent on request_id, so the
same business action must always produce the same request_id: derive_request_id(kit,
business_key, step). A genuinely new attempt (for example replacing a CANCELLED transfer)
uses a new step such as "replacement-1".
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .money import Money

REQUEST_ID_NAMESPACE = uuid.UUID("5d3c1f0a-8b2e-4c6d-9f47-1a2b3c4d5e6f")
_SEPARATOR = "\x1f"


def to_jsonable(value: Any) -> Any:
    """Convert to plain JSON types deterministically.

    Floats, sets and naive datetimes are refused.
    """
    if isinstance(value, Enum):  # before str: str-based enums must become their value
        return to_jsonable(value.value)
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        raise TypeError("float is not allowed here; use Decimal or Money")
    if isinstance(value, Money):
        return value.to_dict()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime; attach a timezone")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("mapping keys must be str")
            out[key] = to_jsonable(item)
        return out
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set | frozenset):
        raise TypeError("sets have no canonical order; use a sorted list")
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def intent_hash(value: Any) -> str:
    """SHA-256 hex of the canonical JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_request_id(kit: str, business_key: str, step: str = "") -> str:
    if not kit or not business_key:
        raise ValueError("kit and business_key are required")
    for part in (kit, business_key, step):
        if _SEPARATOR in part:
            raise ValueError("request id parts may not contain the unit separator")
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, _SEPARATOR.join((kit, business_key, step))))
