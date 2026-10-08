"""Exact JSON on the wire.

Airwallex amounts are major units sent as JSON numbers. json.dumps cannot write a Decimal, and
going through float would turn 0.1 into 0.1000000000000000055. So the encoder writes every
Decimal as its exact plain-decimal text (10.00 stays 10.00), refuses floats and non-finite
values, and the decoder reads every JSON number with a fraction as a Decimal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from typing import Any

from awx_common import Money


def _encode(value: Any, out: list[str]) -> None:
    if isinstance(value, Enum):
        _encode(value.value, out)
    elif value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        raise TypeError("float is not allowed in an Airwallex request body; use Decimal")
    elif isinstance(value, Money):
        raise TypeError(
            "pass Money.amount (a Decimal) and the currency as separate provider fields"
        )
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal in a request body")
        text = format(value, "f")
        if text.startswith("-0") and value.is_zero():
            text = text[1:]
        out.append(text)
    elif isinstance(value, Mapping):
        out.append("{")
        for i, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be str")
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _encode(item, out)
        out.append("}")
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        raise TypeError(f"cannot encode {type(value).__name__} in a request body")


def encode_json(value: Any) -> bytes:
    out: list[str] = []
    _encode(value, out)
    return "".join(out).encode("utf-8")


def decode_json(data: bytes | str) -> Any:
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    if not data.strip():
        return None
    return json.loads(data, parse_float=Decimal, parse_constant=_refuse_constant)


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name} in a response")
