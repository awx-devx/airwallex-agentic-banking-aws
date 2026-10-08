"""Lossless JSON for payloads that pass through Step Functions, Lambda or DynamoDB.

Step Functions treats JSON numbers as doubles, so Decimal and Money travel as tagged strings:
Decimal -> {"$decimal": "12.34"}, Money -> {"$money": ["12.34", "AUD"]},
datetime -> {"$datetime": "2026-10-06T04:00:00Z"}. Dataclasses become plain dicts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .money import Money

_TAGS = ("$decimal", "$money", "$datetime")


def encode(value: Any) -> Any:
    if isinstance(value, Enum):  # before str: str-based enums must become their value
        return encode(value.value)
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        raise TypeError("float is not allowed in transport payloads; use Decimal or Money")
    if isinstance(value, Money):
        return {"$money": [format(value.amount, "f"), value.currency]}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal")
        return {"$decimal": format(value, "f")}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime; attach a timezone")
        return {"$datetime": value.astimezone(UTC).isoformat().replace("+00:00", "Z")}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("mapping keys must be str")
            if key in _TAGS:
                raise ValueError(f"key {key!r} is reserved for transport tags")
            out[key] = encode(item)
        return out
    if isinstance(value, list | tuple):
        return [encode(item) for item in value]
    raise TypeError(f"cannot encode {type(value).__name__}")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if len(value) == 1:
            ((key, item),) = value.items()
            if key == "$decimal":
                return Decimal(item)
            if key == "$money":
                return Money.of(item[0], item[1])
            if key == "$datetime":
                return datetime.fromisoformat(item.replace("Z", "+00:00"))
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, float):
        raise TypeError("float found in a transport payload; an amount lost precision upstream")
    return value


def dumps(value: Any) -> str:
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def loads(text: str) -> Any:
    return decode(json.loads(text, parse_float=_refuse_float))


def _refuse_float(text: str) -> Any:
    raise TypeError(f"float literal {text} in a transport payload")
