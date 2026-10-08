"""Exact money. An amount is always a Decimal at the currency's minor-unit scale, never a float."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

# ISO 4217 minor units that differ from 2. Every other code uses 2.
# The spine checks these against Airwallex in the sandbox (awx_spine FAKE-FIDELITY.md).
_MINOR_UNITS: dict[str, int] = {
    "BHD": 3,
    "CLP": 0,
    "IQD": 3,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "PYG": 0,
    "TND": 3,
    "UGX": 0,
    "VND": 0,
    "XAF": 0,
    "XOF": 0,
}


class CurrencyMismatch(ValueError):
    """Arithmetic or comparison between two different currencies."""


def minor_units(currency: str) -> int:
    return _MINOR_UNITS.get(currency, 2)


def check_currency(currency: object) -> str:
    if not (
        isinstance(currency, str)
        and len(currency) == 3
        and currency.isascii()
        and currency.isalpha()
        and currency.isupper()
    ):
        raise ValueError(f"currency must be a 3-letter upper-case ISO 4217 code, got {currency!r}")
    return currency


def _quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-minor_units(currency))


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in one currency, held at that currency's minor-unit scale.

    Build with Money.of("12.34", "AUD"). Too many decimal places raise unless you pass an
    explicit rounding mode, so a calculation never rounds silently.
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount must be a Decimal; build it with Money.of()")
        if not self.amount.is_finite():
            raise ValueError("Money.amount must be finite")
        check_currency(self.currency)
        quantum = _quantum(self.currency)
        exact = self.amount.quantize(quantum)
        if exact != self.amount:
            raise ValueError(
                f"{self.amount} has more decimal places than {self.currency} allows; "
                "pass rounding= to Money.of() to round on purpose"
            )
        if exact.is_zero():
            exact = exact.copy_abs()  # -0.00 and 0.00 must serialise and hash identically
        object.__setattr__(self, "amount", exact)

    @classmethod
    def of(
        cls, amount: str | int | Decimal, currency: str, *, rounding: str | None = None
    ) -> Money:
        if isinstance(amount, float | bool):
            raise TypeError("never build Money from a float or bool; pass a str, int or Decimal")
        value = amount if isinstance(amount, Decimal) else Decimal(amount)
        check_currency(currency)
        if rounding is not None:
            value = value.quantize(_quantum(currency), rounding=rounding)
        return cls(value, currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Decimal(0), currency)

    @classmethod
    def from_minor(cls, minor: int, currency: str) -> Money:
        if isinstance(minor, bool) or not isinstance(minor, int):
            raise TypeError("minor units must be an int")
        return cls(Decimal(minor).scaleb(-minor_units(check_currency(currency))), currency)

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Money:
        return cls.of(data["amount"], data["currency"])

    def to_minor(self) -> int:
        return int(self.amount.scaleb(minor_units(self.currency)))

    def to_dict(self) -> dict[str, str]:
        return {"amount": format(self.amount, "f"), "currency": self.currency}

    def scale(self, factor: Decimal | int, *, rounding: str) -> Money:
        """Multiply by an exact factor (fee rate, FX rate) and round explicitly."""
        if isinstance(factor, float | bool):
            raise TypeError("factor must be a Decimal or int, never a float")
        return Money.of(self.amount * Decimal(factor), self.currency, rounding=rounding)

    def allocate(self, weights: Sequence[Decimal | int]) -> list[Money]:
        """Split in proportion to weights. The parts always sum to exactly this amount.

        Uses largest remainder in minor units; ties go to the earlier weight.
        """
        if not weights:
            raise ValueError("allocate needs at least one weight")
        if any(isinstance(w, float | bool) for w in weights):
            raise TypeError("weights must be Decimal or int, never float")
        ws = [Decimal(w) for w in weights]
        if any(w < 0 for w in ws) or sum(ws) == 0:
            raise ValueError("weights must be non-negative and not all zero")
        total = self.to_minor()
        sign = -1 if total < 0 else 1
        total_abs = abs(total)
        wsum = sum(ws)
        raw = [Decimal(total_abs) * w / wsum for w in ws]
        parts = [int(r) for r in raw]
        remainder = total_abs - sum(parts)
        order = sorted(range(len(ws)), key=lambda i: (-(raw[i] - parts[i]), i))
        for i in order[:remainder]:
            parts[i] += 1
        return [Money.from_minor(sign * p, self.currency) for p in parts]

    def is_zero(self) -> bool:
        return self.amount == 0

    def is_negative(self) -> bool:
        return self.amount < 0

    def _same(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")
        return other

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + self._same(other).amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        return Money(self.amount - self._same(other).amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __lt__(self, other: Money) -> bool:
        return self.amount < self._same(other).amount

    def __le__(self, other: Money) -> bool:
        return self.amount <= self._same(other).amount

    def __gt__(self, other: Money) -> bool:
        return self.amount > self._same(other).amount

    def __ge__(self, other: Money) -> bool:
        return self.amount >= self._same(other).amount

    def __str__(self) -> str:
        return f"{format(self.amount, 'f')} {self.currency}"
