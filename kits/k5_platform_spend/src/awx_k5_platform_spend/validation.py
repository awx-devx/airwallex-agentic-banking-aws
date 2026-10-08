"""Tool input -> canonical intent. Strict: unknown keys, server-owned keys and non-strings fail."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from awx_common import SERVER_OWNED_FIELDS, Money

REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
CARD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PERIOD = re.compile(r"^(20[0-9]{2})-(0[1-9]|1[0-2])$")
AMOUNT = re.compile(r"^[0-9]{1,9}(\.[0-9]{1,3})?$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
MCC = re.compile(r"^[0-9]{4}$")
NAME = re.compile(r"^[A-Za-z][A-Za-z' -]{0,49}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
RATIONALE_MAX = 500


class InvalidIntent(ValueError):
    """The tool input does not describe a valid intent."""


def strict(
    raw: Mapping[str, Any], required: Mapping[str, re.Pattern[str]], optional: Mapping[str, int]
) -> dict[str, str]:
    """`required` keys must match their pattern; `optional` keys are free text up to N chars."""
    if not isinstance(raw, Mapping):
        raise InvalidIntent("input must be an object")
    owned = set(raw) & SERVER_OWNED_FIELDS
    if owned:
        raise InvalidIntent(f"input carries server-owned fields {sorted(owned)}")
    unknown = set(raw) - set(required) - set(optional)
    if unknown:
        raise InvalidIntent(f"unknown fields {sorted(unknown)}")
    out: dict[str, str] = {}
    for key, pattern in required.items():
        value = raw.get(key)
        if not isinstance(value, str) or not pattern.match(value):
            raise InvalidIntent(f"{key} is missing or malformed")
        out[key] = value
    for key, limit in optional.items():
        if key in raw:
            value = raw[key]
            if not isinstance(value, str) or len(value) > limit:
                raise InvalidIntent(f"{key} must be a string of at most {limit} characters")
            out[key] = value.strip()
    return out


def amount_of(text: str, currency: str) -> Money:
    """A decimal string at the currency's scale. Money refuses extra places."""
    try:
        return Money.of(text, currency)
    except ValueError as exc:
        raise InvalidIntent(str(exc)) from exc
