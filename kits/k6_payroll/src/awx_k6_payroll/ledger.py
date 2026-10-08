"""The kit's payroll subledger: planned entries, partitioned by employer tenant and pay period.

One partition per employer per period, matching one workflow execution per employer. An
entry never names another employer. Signed amounts: credit positive, debit negative, per
wallet currency. `plan` is assess_employer(...)["plan"]. The entries are
planned only: nothing writes them to the spine's ledger table yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from awx_common import Money

from .books import Keys
from .model import employer_tenant_id
from .pricing import SWIFT_FEES


@dataclass(frozen=True)
class LedgerEntry:
    partition: str
    entry_id: str  # the request_id of the operation the entry plans for
    kind: str  # PAYOUT | SWIFT_FEE | FX_BUY | FX_SELL_CAP | PLATFORM_FEE
    amount: Money
    contractor: str | None = None


def partition_key(employer: str, pay_period: str) -> str:
    return f"{employer_tenant_id(employer)}#{pay_period}"


def plan_entries(
    intent: Mapping[str, Any], plan: Mapping[str, Any], keys: Keys
) -> list[LedgerEntry]:
    employer, period = intent["employer"], intent["pay_period"]
    part = partition_key(employer, period)
    out: list[LedgerEntry] = []
    for c in intent["contractors"]:
        rid = keys.payout_rid(c["contractor"])
        out.append(LedgerEntry(part, rid, "PAYOUT", -c["amount"], c["contractor"]))
        if c["transfer_method"] == "SWIFT":
            fee = SWIFT_FEES[c["amount"].currency]
            out.append(LedgerEntry(part, rid, "SWIFT_FEE", -fee, c["contractor"]))
    for cv in plan["conversions"]:
        rid = keys.conversion_rid(cv["buy_currency"], cv["sell_currency"])
        out.append(LedgerEntry(part, rid, "FX_BUY", cv["buy_amount"]))
        out.append(LedgerEntry(part, rid, "FX_SELL_CAP", -cv["sell_cap"]))
    fee = intent["pricing"]["platform_fee"]
    out.append(LedgerEntry(part, keys.fee_rid(), "PLATFORM_FEE", -fee))
    return out


def net_by_currency(entries: list[LedgerEntry], *, kinds: frozenset[str]) -> dict[str, Money]:
    out: dict[str, Money] = {}
    for e in entries:
        if e.kind in kinds:
            ccy = e.amount.currency
            out[ccy] = out.get(ccy, Money.zero(ccy)) + e.amount
    return out
