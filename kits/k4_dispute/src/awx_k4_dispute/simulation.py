"""Sandbox-only setup and simulation operations, behind one interface (guide: "Put
sandbox-only simulation calls behind one interface").

These build the three demo cases: a payment intent paid with the public test card, then a
dispute staged on it, then the issuer's escalation and resolution. They are test-harness
writes, not agent tools: no ToolSpec exposes them. Every object carries ``poc-k4-`` in
merchant_order_id or metadata, so tests only ever touch objects they created. Body shapes
beyond the guide's endpoint list are assumptions not yet confirmed against the live sandbox;
each lives in one function here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from awx_common import Money, Operation, derive_request_id, intent_hash

from .cases import check_book_id
from .config import KIT
from .provider import PAYMENT_INTENTS_PATH

SIMULATION_DISPUTES_PATH = "/api/v1/simulation/pa/payment_disputes"
TEST_CARD_NUMBER = "4035501000000008"  # public sandbox test card from the guide
CREATE_STAGES = ("RFI", "CHARGEBACK")  # PRE_CHARGEBACK auto-accepts in the same second
VISA_CODES = ("10.4", "13.1", "13.6")
RESOLUTIONS = ("WON", "LOST")


@dataclass(frozen=True)
class DemoCase:
    case_key: str  # e.g. "strong-fraud"; becomes part of every poc-k4- label
    amount: Money
    reason_code: str
    stage: str
    response_window: timedelta = timedelta(days=7)  # due_at = staging time + this
    prefix: str = "poc-k4"  # the live sandbox scenario uses "poc-i4"

    def __post_init__(self) -> None:
        if self.reason_code not in VISA_CODES:
            raise ValueError(f"use Visa reason codes {VISA_CODES}")
        if self.stage not in CREATE_STAGES:
            raise ValueError(f"create disputes at {CREATE_STAGES}; PRE_CHARGEBACK auto-accepts")
        if not self.amount.amount > 0:
            raise ValueError("amount must be positive")

    def label(self, book_id: str) -> str:
        """Rule M2: the case key is kit-local, so every label and request_id carries the book
        id (cases.ensure_book), and a re-run on the shared sandbox never adopts old objects."""
        return f"{self.prefix}-{check_book_id(book_id)}-{self.case_key}"


DEMO_CASES: tuple[DemoCase, ...] = (
    DemoCase("strong-fraud", Money.of("900.00", "AUD"), "10.4", "RFI"),
    DemoCase("small-not-received", Money.of("15.00", "AUD"), "13.1", "CHARGEBACK"),
    DemoCase("credit-not-processed", Money.of("180.00", "AUD"), "13.6", "RFI"),
)


def _op(name: str, path: str, body: dict, business_key: str, step: str) -> Operation:
    rid = derive_request_id(KIT, business_key, step)
    full = {"request_id": rid, **body}
    return Operation(
        kit=KIT,
        name=name,
        method="POST",
        path=path,
        body=full,
        request_id=rid,
        business_key=business_key,
        intent_hash=intent_hash({"simulation": name, "business_key": business_key, "body": body}),
    )


def create_payment_intent(case: DemoCase, book_id: str) -> Operation:
    return _op(
        "payment_intents.create",
        f"{PAYMENT_INTENTS_PATH}/create",
        {
            "amount": case.amount.amount,
            "currency": case.amount.currency,
            "merchant_order_id": case.label(book_id),
            "metadata": {"poc": case.label(book_id)},
        },
        case.label(book_id),
        "payment-intent",
    )


def confirm_payment_intent(
    case: DemoCase, book_id: str, payment_intent_id: str, expiry_year: int
) -> Operation:
    return _op(
        "payment_intents.confirm",
        f"{PAYMENT_INTENTS_PATH}/{payment_intent_id}/confirm",
        {
            "payment_method": {
                "type": "card",
                "card": {
                    "number": TEST_CARD_NUMBER,
                    "expiry_month": "12",
                    "expiry_year": str(expiry_year),
                    "cvc": "123",
                    "name": case.label(book_id),
                },
            }
        },
        case.label(book_id),
        "confirm",
    )


def stage_dispute(case: DemoCase, book_id: str, payment_intent_id: str, now: datetime) -> Operation:
    """Stage a dispute on a CONFIRMED (paid) payment intent. Airwallex needs payment_intent_id
    and due_at (400 'Missing body parameter "due_at"'), and returns 500 operation_failed on an
    unpaid intent. A 500 is OutcomeUnknown: read GET /pa/payment_disputes back before any
    resend. due_at is code-derived from `now` and the case's response window, ISO 8601 UTC."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    due_at = (now + case.response_window).astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _op(
        "simulation.payment_disputes.create",
        f"{SIMULATION_DISPUTES_PATH}/create",
        {
            "payment_intent_id": payment_intent_id,
            "amount": case.amount.amount,
            "reason_code": case.reason_code,
            "stage": case.stage,
            "due_at": due_at,
        },
        case.label(book_id),
        "stage-dispute",
    )


def escalate_dispute(case: DemoCase, book_id: str, dispute_id: str) -> Operation:
    """RFI -> CHARGEBACK, landing back in REQUIRES_RESPONSE: the issuer's rejection beat.
    Escalating from CHARGEBACK fails with 'Dispute transition is not supported'."""
    return _op(
        "simulation.payment_disputes.escalate",
        f"{SIMULATION_DISPUTES_PATH}/{dispute_id}/escalate",
        {},
        case.label(book_id),
        "escalate",
    )


def resolve_dispute(case: DemoCase, book_id: str, dispute_id: str, outcome: str) -> Operation:
    if outcome not in RESOLUTIONS:
        raise ValueError(f"outcome must be one of {RESOLUTIONS}")
    return _op(
        "simulation.payment_disputes.resolve",
        f"{SIMULATION_DISPUTES_PATH}/{dispute_id}/resolve",
        {"outcome": outcome},
        case.label(book_id),
        f"resolve:{outcome}",
    )


def is_simulation(op: Operation) -> bool:
    return op.path.startswith("/api/v1/simulation/") or (
        op.name.startswith("payment_intents.") and op.path.startswith(PAYMENT_INTENTS_PATH)
    )
