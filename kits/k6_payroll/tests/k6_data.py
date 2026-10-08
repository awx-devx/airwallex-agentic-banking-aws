"""Synthetic payroll inputs. Routing codes and the IBAN are real-format test identifiers with
made-up names; nothing here is a real person or account."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

NOW = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
PERIOD = "2026-W41"
RATES = {"EURUSD": Decimal("1.127611"), "GBPUSD": Decimal("1.2801")}  # USD per unit
ACCTS = {"a": "acct_employerA", "b": "acct_employerB", "c": "acct_employerC"}
TENANTS = {f"k6:employer:{k}": v for k, v in ACCTS.items()}


def us(code: str, amount: str) -> dict[str, Any]:
    return {
        "contractor": code,
        "first_name": "Sam",
        "last_name": "Example",
        "amount": {"amount": amount, "currency": "USD"},
        "transfer_method": "LOCAL",
        "address": {
            "street_address": "1 Test St",
            "city": "Austin",
            "state": "US-TX",
            "postcode": "78701",
            "country_code": "US",
        },
        "bank": {
            "account_name": "Sam Example",
            "account_number": "000123456789",
            "routing_number": "021000021",
            "category": "Checking",
        },
    }


def de(code: str, amount: str) -> dict[str, Any]:
    return {
        "contractor": code,
        "first_name": "Eva",
        "last_name": "Muster",
        "amount": {"amount": amount, "currency": "EUR"},
        "transfer_method": "SWIFT",
        "address": {
            "street_address": "Teststrasse 1",
            "city": "Berlin",
            "postcode": "10115",
            "country_code": "DE",
        },
        "bank": {
            "account_name": "Eva Muster",
            "iban": "DE89370400440532013000",
            "swift_code": "COBADEFFXXX",
        },
    }


def gb(code: str, amount: str) -> dict[str, Any]:
    return {
        "contractor": code,
        "first_name": "Ali",
        "last_name": "Sample",
        "amount": {"amount": amount, "currency": "GBP"},
        "transfer_method": "LOCAL",
        "address": {
            "street_address": "1 Test Road",
            "city": "London",
            "postcode": "EC1A 1BB",
            "country_code": "GB",
        },
        "bank": {
            "account_name": "Ali Sample",
            "account_number": "12345678",
            "sort_code": "040004",
            "bank_name": "Example Bank",
        },
    }


def employer(code: str, *contractors: dict[str, Any], funding: str = "USD") -> dict[str, Any]:
    return {"employer": code, "funding_currency": funding, "contractors": list(contractors)}


def child_raw(emp: dict[str, Any], period: str = PERIOD) -> dict[str, Any]:
    return {"pay_period": period, **emp}


def batch() -> dict[str, Any]:
    return {
        "pay_period": PERIOD,
        "employers": [
            employer("a", us("alice", "1200.00"), de("anna", "2000.00")),
            employer("b", us("bob", "900.00"), de("bernd", "1500.00"), gb("bea", "700.00")),
            employer("c", us("carl", "800.00")),
        ],
    }
