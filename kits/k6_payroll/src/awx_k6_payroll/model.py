"""Payroll intents: parsing, corridor rules and the canonical shapes that get hashed.

Tool input arrives as plain JSON from a synthetic payroll file. `parse_batch` turns it into a
canonical batch; `parse_employer` turns one employer's slice into the canonical child intent
that one child workflow run executes. Both refuse anything they do not understand, because an
approval binds to exactly this canonical form.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from awx_common import Money

KIT = "k6"
LABEL = "poc-i6"  # every sandbox object this kit creates carries this prefix

PAY_PERIOD = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")  # ISO week, e.g. 2026-W41
US_STATE = re.compile(r"^US-[A-Z]{2}$")
CODE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
MAX_EMPLOYERS = 20
MAX_CONTRACTORS = 50

LOCAL = "LOCAL"
SWIFT = "SWIFT"


class PayrollInputError(ValueError):
    """The payroll input is not something this kit will execute."""


@dataclass(frozen=True)
class Corridor:
    country: str
    currency: str
    method: str
    bank_fields: tuple[str, ...]  # required keys of the contractor's `bank` object


# Only the corridors the guide documents ("Beneficiary payloads"). Everything else is refused
# until someone has checked POST /beneficiaries/schema for it.
CORRIDORS: dict[tuple[str, str, str], Corridor] = {
    c[:3]: Corridor(*c)
    for c in (
        ("US", "USD", LOCAL, ("account_name", "account_number", "routing_number", "category")),
        ("GB", "GBP", LOCAL, ("account_name", "account_number", "sort_code", "bank_name")),
        ("DE", "EUR", SWIFT, ("account_name", "iban", "swift_code")),
    )
}
ADDRESS_FIELDS = ("street_address", "city", "postcode", "country_code")
US_ADDRESS_FIELDS = (*ADDRESS_FIELDS, "state")
ACCOUNT_CATEGORIES = ("Checking", "Savings")  # exact casing; other casing fails with code 016


def employer_tenant_id(employer: str) -> str:
    """The tenant_id the server-side TenantResolver resolves for one employer.

    The model names an employer by its platform code. It never sees or picks the connected
    account; the resolver maps this id to the acct_ id from the platform's tenant map.
    """
    return f"{KIT}:employer:{employer}"


def _code(value: Any, what: str) -> str:
    if not isinstance(value, str) or not CODE.match(value):
        raise PayrollInputError(f"{what} must match {CODE.pattern}")
    return value


def _str(value: Any, what: str, *, max_len: int = 140) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise PayrollInputError(f"{what} must be a non-empty string of at most {max_len} chars")
    return value


def _keys(obj: Any, what: str, required: Sequence[str], optional: Sequence[str] = ()) -> None:
    if not isinstance(obj, Mapping):
        raise PayrollInputError(f"{what} must be an object")
    missing = [k for k in required if k not in obj]
    extra = [k for k in obj if k not in required and k not in optional]
    if missing:
        raise PayrollInputError(f"{what} is missing {missing}")
    if extra:
        raise PayrollInputError(f"{what} has unknown fields {sorted(extra)}")


def parse_money(value: Any, what: str) -> Money:
    if isinstance(value, Money):
        money = value
    else:
        _keys(value, what, ("amount", "currency"))
        amount = value["amount"]
        if isinstance(amount, float | bool) or not isinstance(amount, str | int | Decimal):
            raise PayrollInputError(f"{what}.amount must be a decimal string")
        try:
            money = Money.of(amount, value["currency"])
        except (ValueError, ArithmeticError) as exc:
            raise PayrollInputError(f"{what}: {exc}") from exc
    if money.amount <= 0:
        raise PayrollInputError(f"{what} must be positive")
    return money


def parse_contractor(raw: Any, where: str) -> dict[str, Any]:
    _keys(
        raw,
        where,
        ("contractor", "first_name", "last_name", "amount", "transfer_method", "address", "bank"),
    )
    contractor = _code(raw["contractor"], f"{where}.contractor")
    amount = parse_money(raw["amount"], f"{where}.amount")
    method = raw["transfer_method"]
    if method not in (LOCAL, SWIFT):
        # The guide: set it explicitly, never infer it from the absence of a fee.
        raise PayrollInputError(f"{where}.transfer_method must be LOCAL or SWIFT")
    address = raw["address"]
    country = address.get("country_code") if isinstance(address, Mapping) else None
    corridor = CORRIDORS.get((country, amount.currency, method))  # type: ignore[arg-type]
    if corridor is None:
        raise PayrollInputError(
            f"{where}: corridor {country}/{amount.currency}/{method} is not one this kit supports"
        )
    addr_fields = US_ADDRESS_FIELDS if country == "US" else ADDRESS_FIELDS
    _keys(address, f"{where}.address", addr_fields, ("state",))
    bank = raw["bank"]
    _keys(bank, f"{where}.bank", corridor.bank_fields)
    if country == "US" and not US_STATE.match(str(address.get("state"))):
        # The sandbox needs an ISO US state (US-NY); NY fails with code 016 (FAKE-FIDELITY.md).
        raise PayrollInputError(f"{where}.address.state must be an ISO code such as US-NY")
    if "category" in corridor.bank_fields and bank["category"] not in ACCOUNT_CATEGORIES:
        raise PayrollInputError(f"{where}.bank.category must be exactly Checking or Savings")
    return {
        "contractor": contractor,
        "first_name": _str(raw["first_name"], f"{where}.first_name", max_len=60),
        "last_name": _str(raw["last_name"], f"{where}.last_name", max_len=60),
        "amount": amount,
        "transfer_method": method,
        "address": {k: _str(address[k], f"{where}.address.{k}") for k in sorted(address)},
        "bank": {k: _str(bank[k], f"{where}.bank.{k}") for k in sorted(bank)},
    }


def parse_employer(raw: Any, pay_period: str) -> dict[str, Any]:
    """One employer's payroll for one period, canonical. Contractors sorted by code."""
    where = "employer"
    _keys(raw, where, ("employer", "funding_currency", "contractors"))
    employer = _code(raw["employer"], "employer")
    where = f"employer {employer}"
    funding = raw["funding_currency"]
    try:
        Money.zero(funding)
    except (TypeError, ValueError) as exc:
        raise PayrollInputError(f"{where}.funding_currency: {exc}") from exc
    lines = raw["contractors"]
    if not isinstance(lines, list | tuple) or not 1 <= len(lines) <= MAX_CONTRACTORS:
        raise PayrollInputError(f"{where} needs 1 to {MAX_CONTRACTORS} contractors")
    contractors = [parse_contractor(c, f"{where}.contractors[{i}]") for i, c in enumerate(lines)]
    codes = [c["contractor"] for c in contractors]
    if len(set(codes)) != len(codes):
        raise PayrollInputError(f"{where} lists a contractor twice")
    return {
        "pay_period": pay_period,
        "employer": employer,
        "funding_currency": funding,
        "contractors": sorted(contractors, key=lambda c: c["contractor"]),
    }


def parse_pay_period(value: Any) -> str:
    if not isinstance(value, str) or not PAY_PERIOD.match(value):
        raise PayrollInputError("pay_period must be an ISO week such as 2026-W41")
    return value


def parse_batch(raw: Mapping[str, Any]) -> dict[str, Any]:
    _keys(raw, "batch", ("pay_period", "employers"))
    period = parse_pay_period(raw["pay_period"])
    employers = raw["employers"]
    if not isinstance(employers, list | tuple) or not 1 <= len(employers) <= MAX_EMPLOYERS:
        raise PayrollInputError(f"a batch needs 1 to {MAX_EMPLOYERS} employers")
    parsed = [parse_employer(e, period) for e in employers]
    codes = [e["employer"] for e in parsed]
    if len(set(codes)) != len(codes):
        raise PayrollInputError("a batch lists an employer twice")
    return {"pay_period": period, "employers": sorted(parsed, key=lambda e: e["employer"])}
