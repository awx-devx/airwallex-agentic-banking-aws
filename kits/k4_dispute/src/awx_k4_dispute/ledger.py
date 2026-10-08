"""Case outcome ledger and typed closure reasons.

Two kinds of entry, never mixed: PROVIDER entries are money Airwallex moved (the disputed
amount lost or kept), MODELLED entries are the merchant's own costs (dispute fee, handling)
that the sandbox does not deduct. Reconciliation against a provider balance uses PROVIDER
entries only: mixing the two is the easiest way to get a dispute ledger wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from awx_common import Money

from .config import DisputeConfig
from .provider import DisputeStatus, DisputeView


class EntrySource(StrEnum):
    PROVIDER = "PROVIDER"
    MODELLED = "MODELLED"


class EntryKind(StrEnum):
    DISPUTED_AMOUNT_LOST = "DISPUTED_AMOUNT_LOST"
    DISPUTED_AMOUNT_RETAINED = "DISPUTED_AMOUNT_RETAINED"
    DISPUTE_FEE = "DISPUTE_FEE"
    HANDLING_COST = "HANDLING_COST"


class CloseReason(StrEnum):
    WON_EVIDENCE_ACCEPTED = "WON_EVIDENCE_ACCEPTED"
    LOST_ISSUER_REJECTED_EVIDENCE = "LOST_ISSUER_REJECTED_EVIDENCE"
    ACCEPTED_LOW_VALUE = "ACCEPTED_LOW_VALUE"
    ACCEPTED_INSUFFICIENT_EVIDENCE = "ACCEPTED_INSUFFICIENT_EVIDENCE"
    ACCEPTED_MERCHANT_ERROR = "ACCEPTED_MERCHANT_ERROR"
    ACCEPTED_AFTER_ISSUER_REJECTION = "ACCEPTED_AFTER_ISSUER_REJECTION"
    EXPIRED_NO_RESPONSE = "EXPIRED_NO_RESPONSE"


CLOSE_REASONS_FOR_STATUS: dict[DisputeStatus, frozenset[CloseReason]] = {
    DisputeStatus.WON: frozenset({CloseReason.WON_EVIDENCE_ACCEPTED}),
    DisputeStatus.REVERSED: frozenset({CloseReason.WON_EVIDENCE_ACCEPTED}),
    DisputeStatus.LOST: frozenset({CloseReason.LOST_ISSUER_REJECTED_EVIDENCE}),
    DisputeStatus.ACCEPTED: frozenset(
        {
            CloseReason.ACCEPTED_LOW_VALUE,
            CloseReason.ACCEPTED_INSUFFICIENT_EVIDENCE,
            CloseReason.ACCEPTED_MERCHANT_ERROR,
            CloseReason.ACCEPTED_AFTER_ISSUER_REJECTION,
        }
    ),
    DisputeStatus.EXPIRED: frozenset({CloseReason.EXPIRED_NO_RESPONSE}),
}


@dataclass(frozen=True)
class LedgerEntry:
    dispute_id: str
    kind: EntryKind
    source: EntrySource
    amount: Money
    note: str


def outcome_entries(view: DisputeView, config: DisputeConfig) -> tuple[LedgerEntry, ...]:
    """Entries for a terminal dispute. Contested outcomes (WON, REVERSED, LOST) carry the fee
    and the contest handling cost; ACCEPTED and EXPIRED carry the accept handling cost and the
    fee only when fee_on_accept is set."""
    if not view.is_terminal:
        raise ValueError(f"dispute {view.dispute_id} is not terminal ({view.raw_status})")
    terms = config.terms_for(view.amount.currency)
    if terms is None:
        raise ValueError(f"no economics configured for {view.amount.currency}")
    d = view.dispute_id
    entries: list[LedgerEntry] = []
    contested = view.status in (DisputeStatus.WON, DisputeStatus.REVERSED, DisputeStatus.LOST)
    if view.status in (DisputeStatus.WON, DisputeStatus.REVERSED):
        entries.append(
            LedgerEntry(
                d,
                EntryKind.DISPUTED_AMOUNT_RETAINED,
                EntrySource.PROVIDER,
                view.amount,
                f"dispute ended {view.status.value}; funds stay with the merchant",
            )
        )
    else:
        entries.append(
            LedgerEntry(
                d,
                EntryKind.DISPUTED_AMOUNT_LOST,
                EntrySource.PROVIDER,
                view.amount,
                f"dispute ended {view.status.value}",
            )
        )
    if contested or config.fee_on_accept:
        entries.append(
            LedgerEntry(
                d,
                EntryKind.DISPUTE_FEE,
                EntrySource.MODELLED,
                terms.dispute_fee,
                "configured dispute fee; not deducted by the sandbox",
            )
        )
    handling = terms.contest_handling_cost if contested else terms.accept_handling_cost
    entries.append(
        LedgerEntry(
            d,
            EntryKind.HANDLING_COST,
            EntrySource.MODELLED,
            handling,
            "configured handling cost; modelled",
        )
    )
    return tuple(entries)


def total(entries: Sequence[LedgerEntry], source: EntrySource, currency: str) -> Money:
    """Sum of the cost entries of one source. RETAINED is not a cost and is excluded."""
    out = Money.zero(currency)
    for e in entries:
        if e.source is source and e.kind is not EntryKind.DISPUTED_AMOUNT_RETAINED:
            out = out + e.amount
    return out
