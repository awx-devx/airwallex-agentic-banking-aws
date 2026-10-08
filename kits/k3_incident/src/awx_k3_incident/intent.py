"""Tool input validation. Turns raw tool input into a canonical intent or raises IntentInvalid.

No intent carries an amount the model chose for a replacement: a replacement intent names only
the original transfer, and code copies amount, currency and beneficiary from Airwallex's own
record of it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from awx_common import Money

from .finality import SIMULATION_FAILURE_TYPES

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,64}$"
_ID = re.compile(ID_PATTERN)
_KEY = re.compile(KEY_PATTERN)
TRANSFER_METHODS = ("LOCAL", "SWIFT")


class IntentInvalid(ValueError):
    """Tool input that cannot become an intent. Nothing is proposed."""


def _only(
    raw: Mapping[str, Any], required: set[str], optional: frozenset[str] | set[str] = frozenset()
) -> None:
    if not isinstance(raw, Mapping):
        raise IntentInvalid("tool input must be an object")
    keys = set(raw)
    if missing := required - keys:
        raise IntentInvalid(f"missing fields {sorted(missing)}")
    if extra := keys - required - optional:
        raise IntentInvalid(f"unexpected fields {sorted(extra)}")


def _match(raw: Mapping[str, Any], field: str, pattern: re.Pattern[str]) -> str:
    value = raw[field]
    if not isinstance(value, str) or not pattern.match(value):
        raise IntentInvalid(f"{field} must match {pattern.pattern}")
    return value


def validate_transfer_ref(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Input for read tools that look at one transfer."""
    _only(raw, {"transfer_id"})
    return {"transfer_id": _match(raw, "transfer_id", _ID)}


def validate_replace(raw: Mapping[str, Any]) -> dict[str, Any]:
    _only(raw, {"original_transfer_id", "incident_ref"})
    return {
        "original_transfer_id": _match(raw, "original_transfer_id", _ID),
        "incident_ref": _match(raw, "incident_ref", _KEY),
    }


def validate_send(raw: Mapping[str, Any]) -> dict[str, Any]:
    _only(
        raw,
        {"payment_key", "beneficiary_id", "amount", "currency", "transfer_method"},
        {"stage_for_incident"},
    )
    stage = raw.get("stage_for_incident", False)
    if not isinstance(stage, bool):
        raise IntentInvalid("stage_for_incident must be a boolean")
    currency = raw["currency"]
    amount_raw = raw["amount"]
    if isinstance(amount_raw, float | bool) or not isinstance(amount_raw, str):
        raise IntentInvalid("amount must be a decimal string such as '100.00'")
    try:
        amount = Money.of(Decimal(amount_raw), currency)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise IntentInvalid(f"amount/currency invalid: {exc}") from exc
    if amount.amount <= 0:
        raise IntentInvalid("amount must be positive")
    method = raw["transfer_method"]
    if method not in TRANSFER_METHODS:
        raise IntentInvalid(f"transfer_method must be one of {TRANSFER_METHODS}")
    return {
        "payment_key": _match(raw, "payment_key", _KEY),
        "beneficiary_id": _match(raw, "beneficiary_id", _ID),
        "amount": amount,
        "transfer_method": method,
        "stage_for_incident": stage,
    }


def validate_close(raw: Mapping[str, Any]) -> dict[str, Any]:
    _only(raw, {"original_transfer_id"})
    return {"original_transfer_id": _match(raw, "original_transfer_id", _ID)}


def validate_simulate(raw: Mapping[str, Any]) -> dict[str, Any]:
    _only(raw, {"transfer_id", "failure_type"})
    ftype = raw["failure_type"]
    if ftype not in SIMULATION_FAILURE_TYPES:
        raise IntentInvalid("failure_type is not one the sandbox simulation accepts")
    return {"transfer_id": _match(raw, "transfer_id", _ID), "failure_type": ftype}
