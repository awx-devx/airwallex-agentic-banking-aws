"""Strict amount parsing for tool input and provider reads.

Every amount that enters kit 2 goes through `parse_money`. It refuses floats and bools, and it
refuses more decimal places than the currency allows, so an amount is never rounded silently.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from awx_common import Money

_AMOUNT = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,3})?$")

# JSON Schema fragment every tool uses for a non-negative amount string.
AMOUNT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": r"^[0-9]{1,12}(\.[0-9]{1,3})?$",
    "description": 'Decimal amount in major units as a string, for example "1500.00"',
}


def parse_money(value: Any, currency: str, *, field: str = "amount") -> Money:
    """Parse a provider or tool amount into exact Money. Fails closed on anything ambiguous."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field}: float or bool amount refused; amounts must be exact")
    if isinstance(value, str):
        if not _AMOUNT.match(value):
            raise ValueError(f"{field}: {value!r} is not a plain non-negative decimal string")
        dec = Decimal(value)
    elif isinstance(value, int | Decimal):
        dec = Decimal(value)
    else:
        raise TypeError(f"{field}: unsupported amount type {type(value).__name__}")
    try:
        return Money.of(dec, currency)
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"{field}: {exc}") from exc


def money_dict(m: Money) -> dict[str, str]:
    return m.to_dict()


def money_from_dict(d: Any, *, field: str = "amount") -> Money:
    if not isinstance(d, dict) or set(d) != {"amount", "currency"}:
        raise ValueError(f"{field}: expected {{amount, currency}}")
    return parse_money(d["amount"], d["currency"], field=field)
