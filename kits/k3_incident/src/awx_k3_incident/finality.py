"""Transfer finality for kit 3: the allowlist that decides WAIT, REPLACE or ESCALATE.

The trap this module exists to prevent is treating anything that is not PAID as failed. A
failed Airwallex transfer ends in CANCELLED, never in a status called FAILED, and CANCELLED does
not mean a person cancelled it. FAILED is transient: Airwallex moves it to CANCELLED within
seconds. SENT is in flight. PAID can still move to FAILED after a bank return.

So exactly one combination permits a replacement: status CANCELLED with a failure code on the
replaceable allowlist. In the sandbox every read after a failure is CANCELLED carrying only
`failure: {code, details: {type}, message}`; the simulated failure_type is not echoed and
details.type is INCORRECT_ROUTING whatever was simulated (awx_spine FAKE-FIDELITY.md), so the
allowlist keys on failure.code. Everything else waits or goes to a person. An unknown status,
a missing code (a bare cancellation) or an unknown code is never replaceable; vocabulary drift
must fail closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class IncidentDecision(StrEnum):
    WAIT = "WAIT"
    REPLACE = "REPLACE"
    ESCALATE = "ESCALATE"


# Transfer statuses (Airwallex API 2025-06-30 state machine, docs "Transfer statuses").
WAIT_STATUSES = frozenset({"SCHEDULED", "PROCESSING", "SENT", "FAILED"})
ESCALATE_STATUSES = frozenset(
    {
        "IN_APPROVAL",  # needs an approver in the Airwallex web app
        "APPROVAL_RECALLED",
        "APPROVAL_REJECTED",
        "APPROVAL_BLOCKED",
        "OVERDUE",  # unfunded; funding is a person's decision
        "PAID",  # provider says paid; trace with the supplier, never replace
    }
)
TERMINAL_STATUS = "CANCELLED"
KNOWN_STATUSES = WAIT_STATUSES | ESCALATE_STATUSES | {TERMINAL_STATUS}

# failure_type values the sandbox simulation accepts, with the failure_reason code each one
# returns (Airwallex docs "Simulate transfer status transition").
FAILURE_REASON_CODES: dict[str, str] = {
    "INVALID_ACCOUNT_NAME_OR_NUMBER": "90101",
    "INVALID_BANK_OR_BRANCH_CODE": "90201",
    "INVALID_SWIFT_BIC_CODE": "90202",
    "INVALID_CORRESPONDENT_BANK_INFORMATION": "90203",
    "INVALID_BANK_INFORMATION": "90204",
    "BENEFICIARY_NAME_MISMATCH": "90301",
    "ACCOUNT_CURRENCY_MISMATCH": "90302",
    "INVALID_BENEFICIARY_DETAILS": "90401",
    "INVALID_SPECIAL_CHARACTER": "90402",
    "CHANNEL_POLICY": "90501",
    "TM_SUSPENDED": "90502",
    "ACCOUNT_CLOSED": "90701",
    "ACCOUNT_INACTIVE_OR_DORMANT": "90702",
    "ACCOUNT_UNDER_RESTRICTION": "90703",
    "BENEFICIARY_REQUESTED": "90801",
    "BENEFICIARY_BANK_RETURNED": "90802",
    "INBOUND_BENFICIARY_VALIDATION_ERROR": "90901",  # sic, Airwallex's spelling
    "INBOUND_ORDER_ERROR": "90902",
    "INBOUND_2B_CARD_NOT_SUPPORTED": "90903",
    "INBOUND_COMPANY_REGISTRATION_NUMBER_ERROR": "90904",
    "RECALL_REQUESTED": "91001",
    "CLIENT_REQUESTED": "91002",
    "INVALID_PAYMENT_PURPOSE": "91101",
    "CARD_ISSUER_ERROR": "91201",
    "SYSTEM_ERROR": "91401",
    "CHANNEL_TIMEOUT": "91402",
    "OTHER": "99902",
}
SIMULATION_FAILURE_TYPES = frozenset(FAILURE_REASON_CODES)

_TYPE_BY_CODE = {code: ftype for ftype, code in FAILURE_REASON_CODES.items()}

# The only failures after which sending the SAME payment to the SAME beneficiary again is
# sensible: the funds came back and nothing about the beneficiary or the instruction was wrong.
# Bad details, compliance holds, recalls and client or beneficiary requests all need a person.
# Keyed on failure.code, the only field the sandbox reads back (FAKE-FIDELITY "Transfers": the
# simulated failure_type is not echoed and failure.details.type is INCORRECT_ROUTING for every
# type). Only 90802 (BENEFICIARY_BANK_RETURNED: the beneficiary's bank sent the money back) is
# allowlisted. 91401 SYSTEM_ERROR and 91402 CHANNEL_TIMEOUT are left out: neither can
# be reproduced in the sandbox (the fake gives them 90000), and a channel timeout or system
# error can still be followed by a late credit at the beneficiary, so replacing it risks paying
# twice. Those go to a person.
REPLACEABLE_FAILURE_CODES = frozenset({"90802"})
REPLACEABLE_FAILURE_TYPES = frozenset(_TYPE_BY_CODE[c] for c in REPLACEABLE_FAILURE_CODES)

_REASON_CODE = re.compile(r"^\s*(\d{5})\b")
_CODE = re.compile(r"^\d{5}$")


@dataclass(frozen=True)
class Assessment:
    transfer_id: str
    status: str
    failure_code: str | None
    failure_type: str | None
    failure: Mapping[str, Any] | None  # the provider's failure object, as read
    decision: IncidentDecision
    reasons: tuple[str, ...]


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def failure_of(transfer: Mapping[str, Any]) -> Mapping[str, Any] | None:
    failure = transfer.get("failure")
    return failure if isinstance(failure, Mapping) else None


def failure_code_of(transfer: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """The five-digit failure code of a transfer, or None, plus any reason to distrust it.

    Primary source: failure.code, which every sandbox read after a failure carries. Secondary:
    the code at the start of a top-level failure_reason (the FAILED transition reply). Never
    failure.details.type or a top-level failure_type: the sandbox fills both with its own
    vocabulary (INCORRECT_ROUTING), not the type that was simulated. A bare cancellation has
    no code at all, so it never reaches the allowlist.
    """
    codes: list[str] = []
    failure = failure_of(transfer)
    if failure is not None:
        code = failure.get("code")
        code = str(code) if isinstance(code, int) and not isinstance(code, bool) else code
        if isinstance(code, str) and _CODE.match(code):
            codes.append(code)
    reason = _opt_str(transfer.get("failure_reason"))
    if reason is not None and (m := _REASON_CODE.match(reason)):
        codes.append(m.group(1))
    if len(set(codes)) > 1:
        return None, (f"failure codes disagree: {sorted(set(codes))}",)
    return (codes[0] if codes else None), ()


def failure_type_of(transfer: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """The documented failure_type name for the transfer's failure code, or None."""
    code, doubts = failure_code_of(transfer)
    return (_TYPE_BY_CODE.get(code) if code else None), doubts


def assess(transfer: Mapping[str, Any]) -> Assessment:
    """Decide WAIT, REPLACE or ESCALATE from one transfer read. Pure, fails closed."""
    transfer_id = str(transfer.get("id") or "")
    status = _opt_str(transfer.get("status")) or ""
    code, doubts = failure_code_of(transfer)
    ftype = _TYPE_BY_CODE.get(code) if code else None
    failure = failure_of(transfer)

    def out(decision: IncidentDecision, *reasons: str) -> Assessment:
        return Assessment(transfer_id, status, code, ftype, failure, decision, tuple(reasons))

    if status in WAIT_STATUSES:
        return out(IncidentDecision.WAIT, f"transfer is {status}, still in flight; never replace")
    if status in ESCALATE_STATUSES:
        return out(IncidentDecision.ESCALATE, f"transfer is {status}; a person must decide")
    if status != TERMINAL_STATUS:
        return out(IncidentDecision.ESCALATE, f"unknown transfer status {status!r}; fail closed")
    if doubts:
        return out(IncidentDecision.ESCALATE, *doubts)
    if code is None:
        message = failure.get("message") if failure else None
        return out(
            IncidentDecision.ESCALATE,
            f"CANCELLED without a failure code (message {message!r}); "
            "may be a deliberate cancellation",
        )
    if code not in REPLACEABLE_FAILURE_CODES:
        return out(
            IncidentDecision.ESCALATE,
            f"failure code {code} ({ftype or 'unknown'}) is not on the replacement allowlist",
        )
    return out(
        IncidentDecision.REPLACE, f"CANCELLED with replaceable failure code {code} ({ftype})"
    )
