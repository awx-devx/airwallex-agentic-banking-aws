"""Reading amounts out of provider JSON without ever touching a float."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from awx_common import Money


def provider_decimal(value: Any) -> Decimal:
    """Airwallex amounts are major units. The spine decodes JSON numbers to Decimal; a float
    here means precision was already lost upstream, so refuse it."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("provider amount arrived as a float or bool; refusing it")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("provider amount is not a number") from exc
    else:
        raise TypeError(f"provider amount has type {type(value).__name__}")
    if not result.is_finite():
        raise ValueError("provider amount is not finite")
    return result


def provider_money(value: Any, currency: str) -> Money:
    return Money.of(provider_decimal(value), currency)


def items_of(body: Mapping[str, Any] | Sequence[Any]) -> list[Mapping[str, Any]]:
    """List endpoints return {"items": [...]}; `balances/current` returns a bare array. Accept
    both, since the spine's read() contract does not say which it hands back for an array."""
    if isinstance(body, Mapping):
        raw = body.get("items")
        if raw is None:
            raise ValueError("list body has no items")
    else:
        raw = body
    if not isinstance(raw, list | tuple) or not all(isinstance(i, Mapping) for i in raw):
        raise ValueError("list body items are not objects")
    return list(raw)


def available_balance(body: Mapping[str, Any] | Sequence[Any], currency: str) -> Money:
    """`available_amount` for one currency from `GET /balances/current`; zero if absent."""
    found = [b for b in items_of(body) if b.get("currency") == currency]
    if len(found) > 1:
        raise ValueError(f"balances list has {currency} twice")
    if not found:
        return Money.zero(currency)
    return provider_money(found[0].get("available_amount", 0), currency)
