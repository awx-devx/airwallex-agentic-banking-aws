"""Operation builders. Pure: they turn canonical payroll data into provider-shaped writes.

Builders never set `on_behalf_of`; workflow code binds every customer-scoped op to the
server-resolved tenant through `awx_common.bind_operation`. Amounts are Decimal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common import Money, Operation

from . import ids
from .books import Keys
from .model import KIT, LABEL

TRANSFER_REASON = "wages_salary"
FEE_REASON = "professional_business_services"  # the guide: there is no platform_fee reason


def bank_details(c: Mapping[str, Any]) -> dict[str, Any]:
    bank = c["bank"]
    country = c["address"]["country_code"]
    currency = c["amount"].currency
    base = {
        "account_name": bank["account_name"],
        "account_currency": currency,
        "bank_country_code": country,
    }
    if country == "US":
        return {
            **base,
            "account_number": bank["account_number"],
            "account_routing_type1": "aba",
            "account_routing_value1": bank["routing_number"],
            "bank_account_category": bank["category"],
        }
    if country == "GB":
        return {
            **base,
            "account_number": bank["account_number"],
            "account_routing_type1": "sort_code",  # lowercase; SORT_CODE fails with code 011
            "account_routing_value1": bank["sort_code"],
            "bank_name": bank["bank_name"],
        }
    # DE / EUR / SWIFT: IBAN plus SWIFT code, no routing fields.
    return {**base, "iban": bank["iban"], "swift_code": bank["swift_code"]}


def beneficiary_payload(c: Mapping[str, Any]) -> dict[str, Any]:
    """The {"beneficiary": ..., "transfer_methods": [...]} wrapper the guide prescribes."""
    return {
        "beneficiary": {
            "entity_type": "PERSONAL",
            "first_name": c["first_name"],
            "last_name": c["last_name"],
            "address": dict(c["address"]),
            "bank_details": bank_details(c),
        },
        "transfer_methods": [c["transfer_method"]],
    }


def validate_beneficiary(k: Keys, c: Mapping[str, Any], ih: str) -> Operation:
    rid = ids.beneficiary_request_id(k.book, k.employer, c["contractor"], k.period, "validate")
    return Operation(
        kit=KIT,
        name="beneficiaries.validate",
        method="POST",
        path="/api/v1/beneficiaries/validate",
        body=beneficiary_payload(c),
        request_id=rid,
        business_key=ids.beneficiary_key(k.book, k.employer, c["contractor"]),
        intent_hash=ih,
    )


def create_beneficiary(k: Keys, c: Mapping[str, Any], ih: str) -> Operation:
    rid = ids.beneficiary_request_id(k.book, k.employer, c["contractor"], k.period, "create")
    body = {
        **beneficiary_payload(c),
        "request_id": rid,  # the spine sends bodies exactly as built
        "nickname": k.nickname(c["contractor"]),
    }
    return Operation(
        kit=KIT,
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body=body,
        request_id=rid,
        business_key=ids.beneficiary_key(k.book, k.employer, c["contractor"]),
        intent_hash=ih,
    )


# Required (400 field_required without it). HR_1 is the longest validity the sandbox accepted
# (MIN_1, MIN_15, HR_1); the rate lock is taken just before approval, so approve within it.
QUOTE_VALIDITY = "HR_1"


def create_quote(
    rid: str, k: Keys, buy: Money, sell: str, ih: str, validity: str = QUOTE_VALIDITY
) -> Operation:
    # No request_id in the body: fx/quotes/create ignores it (FAKE-FIDELITY). `rid` names the
    # quote in the operation store only.
    return Operation(
        kit=KIT,
        name="fx.quotes.create",
        method="POST",
        path="/api/v1/fx/quotes/create",
        body={
            "buy_currency": buy.currency,
            "sell_currency": sell,
            "buy_amount": buy.amount,
            "validity": validity,
        },
        request_id=rid,
        business_key=k.conversion_key(buy.currency, sell),
        intent_hash=ih,
    )


def create_conversion(
    rid: str, k: Keys, buy: Money, sell: str, quote_id: str, ih: str
) -> Operation:
    """`rid` is the conversion attempt's id (Keys.conversion_rid), fixed before sending."""
    return Operation(
        kit=KIT,
        name="fx.conversions.create",
        method="POST",
        path="/api/v1/fx/conversions/create",
        body={
            "request_id": rid,
            "buy_currency": buy.currency,
            "sell_currency": sell,
            "buy_amount": buy.amount,
            "quote_id": quote_id,
        },
        request_id=rid,
        business_key=k.conversion_key(buy.currency, sell),
        intent_hash=ih,
    )


def payout_reference(period: str, contractor: str) -> str:
    return f"{LABEL}-{period}-{contractor}"


def create_transfer(k: Keys, c: Mapping[str, Any], beneficiary_id: str, ih: str) -> Operation:
    rid = k.payout_rid(c["contractor"])
    amount: Money = c["amount"]
    return Operation(
        kit=KIT,
        name="transfers.create",
        method="POST",
        path="/api/v1/transfers/create",
        body={
            "request_id": rid,
            "beneficiary_id": beneficiary_id,
            "transfer_currency": amount.currency,
            "transfer_amount": amount.amount,
            # Pay from the wallet in the payout currency, so a missing conversion fails on
            # funds rather than silently converting from another balance.
            "source_currency": amount.currency,
            "transfer_method": c["transfer_method"],
            "reason": TRANSFER_REASON,
            "reference": payout_reference(k.period, c["contractor"]),
        },
        request_id=rid,
        business_key=k.payout_key(c["contractor"]),
        intent_hash=ih,
    )


def create_fee_charge(k: Keys, fee: Money, ih: str) -> Operation:
    """Platform call: customer to platform. bind_platform_operation sets `source`; the call
    must not carry x-on-behalf-of (400 "Permission denied." with it)."""
    rid = k.fee_rid()
    return Operation(
        kit=KIT,
        name="charges.create",
        method="POST",
        path="/api/v1/charges/create",
        body={
            "request_id": rid,
            "amount": fee.amount,
            "currency": fee.currency,
            "reason": FEE_REASON,
            "reference": f"{LABEL}-fee-{k.period}",
        },
        request_id=rid,
        business_key=k.fee_key(),
        intent_hash=ih,
    )
