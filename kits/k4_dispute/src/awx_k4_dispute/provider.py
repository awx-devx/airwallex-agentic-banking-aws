"""Everything that knows Airwallex's dispute shapes lives here, so a shape change touches one file.

Shapes follow the spine's sandbox and API-reference evidence (awx_spine FAKE-FIDELITY.md, rows
"Dispute statuses", "Accept body", "Challenge body", "simulation/pa/payment_disputes/create",
"Files"):

- statuses REQUIRES_RESPONSE, CHALLENGED, ACCEPTED, REVERSED, WON, LOST, PENDING_CLOSURE,
  EXPIRED, PENDING_DECISION; the response deadline is `due_at`
- accept and challenge REQUIRE `request_id` in the body; this kit sends one fixed request_id
  per dispute and action, ever
- the accept and challenge reason enums and `supporting_documents.documents[{type, file_ids}]`
- `product_type` is required on a challenge at CHARGEBACK; this kit always sends it

Still unconfirmed (documented, not provoked live because the sandbox account cannot yet take
card payments): the `product_type` enum value, and the field that shows a prior challenge.
Unknown statuses fail closed: neither respondable nor terminal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from awx_common import Money, Operation, ReadRequest, derive_request_id
from awx_common.evidence import FILES_UPLOAD_OP, FILES_UPLOAD_PATH, EvidenceRef

from .config import KIT

DISPUTES_PATH = "/api/v1/pa/payment_disputes"
REFUNDS_PATH = "/api/v1/pa/refunds"
PAYMENT_INTENTS_PATH = "/api/v1/pa/payment_intents"

READ_ALLOWLIST: tuple[str, ...] = (
    r"^/api/v1/pa/payment_disputes$",
    r"^/api/v1/pa/payment_disputes/[A-Za-z0-9_-]{1,64}$",
    r"^/api/v1/pa/refunds$",
    r"^/api/v1/pa/payment_intents/[A-Za-z0-9_-]{1,64}$",
)

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class DisputeStatus(StrEnum):
    REQUIRES_RESPONSE = "REQUIRES_RESPONSE"
    CHALLENGED = "CHALLENGED"
    ACCEPTED = "ACCEPTED"
    PENDING_DECISION = "PENDING_DECISION"  # challenged; the issuer has not decided yet
    WON = "WON"
    REVERSED = "REVERSED"  # funds returned to the merchant; closes as a win
    LOST = "LOST"
    PENDING_CLOSURE = "PENDING_CLOSURE"  # outcome fixed but not final; close waits for it
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"  # anything the table does not know; fails closed


TERMINAL_STATUSES = frozenset(
    {
        DisputeStatus.ACCEPTED,
        DisputeStatus.WON,
        DisputeStatus.REVERSED,
        DisputeStatus.LOST,
        DisputeStatus.EXPIRED,
    }
)
RESPONDABLE_STATUSES = frozenset({DisputeStatus.REQUIRES_RESPONSE})
STAGES = ("RFI", "PRE_CHARGEBACK", "CHARGEBACK", "PRE_ARBITRATION", "ARBITRATION")


class ReasonFamily(StrEnum):
    FRAUD_CARD_ABSENT = "FRAUD_CARD_ABSENT"  # Visa 10.4
    NOT_RECEIVED = "NOT_RECEIVED"  # Visa 13.1
    CREDIT_NOT_PROCESSED = "CREDIT_NOT_PROCESSED"  # Visa 13.6
    UNKNOWN = "UNKNOWN"


VISA_REASON_FAMILIES: dict[str, ReasonFamily] = {
    "10.4": ReasonFamily.FRAUD_CARD_ABSENT,
    "13.1": ReasonFamily.NOT_RECEIVED,
    "13.6": ReasonFamily.CREDIT_NOT_PROCESSED,
}


class AcceptReason(StrEnum):
    """Typed reason recorded with an accept. The guide says the provider enum has a low-value
    reason; the provider strings below are unconfirmed and live only in PROVIDER_ACCEPT_REASON."""

    LOW_VALUE = "LOW_VALUE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    MERCHANT_ERROR = "MERCHANT_ERROR"
    AFTER_ISSUER_REJECTION = "AFTER_ISSUER_REJECTION"


PROVIDER_ACCEPT_REASONS = frozenset(
    {
        "AGREEMENT_REACHED_WITH_CUSTOMER",
        "CUSTOMER_RELATIONSHIP_MAINTENANCE",
        "LOW_VALUE_TRANSACTION",
        "VALID_CUSTOMER_DISPUTE",
        "COLLABORATION_ACCEPTED_MANUAL",
        "OTHERS",
    }
)
PROVIDER_ACCEPT_REASON: dict[AcceptReason, str] = {
    AcceptReason.LOW_VALUE: "LOW_VALUE_TRANSACTION",
    AcceptReason.INSUFFICIENT_EVIDENCE: "OTHERS",
    AcceptReason.MERCHANT_ERROR: "VALID_CUSTOMER_DISPUTE",
    AcceptReason.AFTER_ISSUER_REJECTION: "OTHERS",
}
# Code-owned description sent with every accept, so OTHERS always says which case it is.
ACCEPT_DESCRIPTION: dict[AcceptReason, str] = {
    AcceptReason.LOW_VALUE: "Accepted: the dispute costs more to contest than to accept.",
    AcceptReason.INSUFFICIENT_EVIDENCE: "Accepted: the merchant lacks evidence to contest.",
    AcceptReason.MERCHANT_ERROR: "Accepted: the merchant agrees the customer is owed.",
    AcceptReason.AFTER_ISSUER_REJECTION: "Accepted after the issuer rejected the evidence.",
}


class ChallengeReason(StrEnum):
    CARDHOLDER_AUTHORISED = "CARDHOLDER_AUTHORISED"
    GOODS_DELIVERED = "GOODS_DELIVERED"
    CREDIT_ALREADY_PROCESSED = "CREDIT_ALREADY_PROCESSED"
    REFUND_NOT_OWED = "REFUND_NOT_OWED"


PROVIDER_CHALLENGE_REASONS = frozenset(
    {
        "CUSTOMER_WITHDRAWN",
        "CUSTOMER_REFUNDED",
        "PRODUCT_RECEIVED",
        "PURCHASE_HISTORY",
        "NOT_ENTITLED",
        "SEPARATE_PRODUCT",
        "AUTHENTIC_PRODUCT",
        "OTHER_REASONS",
    }
)
PROVIDER_CHALLENGE_REASON: dict[ChallengeReason, str] = {
    ChallengeReason.CARDHOLDER_AUTHORISED: "PURCHASE_HISTORY",
    ChallengeReason.GOODS_DELIVERED: "PRODUCT_RECEIVED",
    ChallengeReason.CREDIT_ALREADY_PROCESSED: "CUSTOMER_REFUNDED",
    ChallengeReason.REFUND_NOT_OWED: "NOT_ENTITLED",
}
DOCUMENT_TYPES = frozenset({"PRIMARY", "ORDER", "CUSTOMER", "OTHER"})

# A challenge reason only fits some reason families.
CHALLENGE_REASON_FAMILIES: dict[ChallengeReason, frozenset[ReasonFamily]] = {
    ChallengeReason.CARDHOLDER_AUTHORISED: frozenset({ReasonFamily.FRAUD_CARD_ABSENT}),
    ChallengeReason.GOODS_DELIVERED: frozenset(
        {ReasonFamily.NOT_RECEIVED, ReasonFamily.FRAUD_CARD_ABSENT}
    ),
    ChallengeReason.CREDIT_ALREADY_PROCESSED: frozenset({ReasonFamily.CREDIT_NOT_PROCESSED}),
    ChallengeReason.REFUND_NOT_OWED: frozenset({ReasonFamily.CREDIT_NOT_PROCESSED}),
}


class ProviderShapeError(ValueError):
    """A provider read is missing a field the kit cannot work without, or carries a float."""


def parse_decimal(value: Any, *, what: str) -> Decimal:
    """Exact Decimal from a provider value. A float means precision was lost upstream."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ProviderShapeError(f"{what} arrived as {type(value).__name__}; need exact JSON")
    if isinstance(value, Decimal | int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ProviderShapeError(f"{what} is not a number") from exc
    raise ProviderShapeError(f"{what} is missing or not a number")


def parse_time(value: Any) -> datetime | None:
    """ISO 8601 to an aware UTC datetime. A naive value is read as UTC (Airwallex sends UTC)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"  # +0000 -> +00:00
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_DEADLINE_KEYS = ("due_at", "respond_by", "response_due_at", "evidence_due_by", "deadline")
# The provider's record of each response, one entry per stage: `accept_details` and
# `challenge_details` lists of {stage, reason, ...} (awx_spine FakeAirwallex; the sandbox shape is
# unconfirmed because no dispute can be staged live yet). Older guesses are kept so a populated
# variant still counts as a prior challenge.
_RESPONSE_KEYS = {"accept": ("accept_details",), "challenge": ("challenge_details",)}
_PRIOR_CHALLENGE_KEYS = ("challenge_details", "challenges", "challenge_history", "challenge")


@dataclass(frozen=True)
class DisputeView:
    """The normalised dispute the kit decides on."""

    dispute_id: str
    payment_intent_id: str | None
    amount: Money
    reason_code: str | None
    reason_family: ReasonFamily
    stage: str | None
    status: DisputeStatus
    raw_status: str | None
    created_at: datetime | None
    provider_deadline: datetime | None
    prior_challenge_seen: bool  # the provider shows any challenge on this dispute
    responses: frozenset[tuple[str, str]] = frozenset()  # (action, stage) the provider records

    def responded(self, action: str, stage: str | None) -> bool:
        """The provider's own record of `action` at `stage`."""
        return stage is not None and (action, stage) in self.responses

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_respondable(self) -> bool:
        return self.status in RESPONDABLE_STATUSES


def _reason_code(body: Mapping[str, Any]) -> str | None:
    reason = body.get("reason")
    candidates = [
        body.get("reason_code"),
        body.get("network_reason_code"),
        reason.get("original_code") if isinstance(reason, Mapping) else None,
        reason.get("code") if isinstance(reason, Mapping) else None,
        reason.get("reason_code") if isinstance(reason, Mapping) else None,
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


def parse_dispute(body: Mapping[str, Any]) -> DisputeView:
    dispute_id = body.get("id")
    if not isinstance(dispute_id, str) or not dispute_id:
        raise ProviderShapeError("dispute read has no id")
    currency = body.get("currency")
    if not isinstance(currency, str):
        raise ProviderShapeError("dispute read has no currency")
    amount = Money.of(parse_decimal(body.get("amount"), what="dispute amount"), currency)
    raw_status = body.get("status") if isinstance(body.get("status"), str) else None
    try:
        status = DisputeStatus(raw_status) if raw_status else DisputeStatus.UNKNOWN
    except ValueError:
        status = DisputeStatus.UNKNOWN
    code = _reason_code(body)
    deadline = None
    for key in _DEADLINE_KEYS:
        deadline = parse_time(body.get(key))
        if deadline is not None:
            break
    prior = any(bool(body.get(k)) for k in _PRIOR_CHALLENGE_KEYS)
    responses = frozenset(
        (action, entry["stage"])
        for action, keys in _RESPONSE_KEYS.items()
        for key in keys
        for entry in (body.get(key) or [])
        if isinstance(entry, Mapping) and isinstance(entry.get("stage"), str)
    )
    stage = body.get("stage") if isinstance(body.get("stage"), str) else None
    pi = body.get("payment_intent_id")
    return DisputeView(
        dispute_id=dispute_id,
        payment_intent_id=pi if isinstance(pi, str) and pi else None,
        amount=amount,
        reason_code=code,
        reason_family=VISA_REASON_FAMILIES.get(code or "", ReasonFamily.UNKNOWN),
        stage=stage,
        status=status,
        raw_status=raw_status,
        created_at=parse_time(body.get("created_at")),
        provider_deadline=deadline,
        prior_challenge_seen=prior,
        responses=responses,
    )


def dispute_read(dispute_id: str) -> ReadRequest:
    return ReadRequest(path=f"{DISPUTES_PATH}/{dispute_id}")


def disputes_list_read(status: str | None = None, page_size: int = 50) -> ReadRequest:
    query = {"page_size": str(page_size)}
    if status:
        query["status"] = status
    return ReadRequest(path=DISPUTES_PATH, query=query)


def refunds_read(payment_intent_id: str) -> ReadRequest:
    return ReadRequest(path=REFUNDS_PATH, query={"payment_intent_id": payment_intent_id})


def payment_intent_read(payment_intent_id: str) -> ReadRequest:
    return ReadRequest(path=f"{PAYMENT_INTENTS_PATH}/{payment_intent_id}")


def items_of(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = body.get("items", [])
    if not isinstance(items, list):
        raise ProviderShapeError("list read has no items array")
    return [i for i in items if isinstance(i, Mapping)]


# ---- operations -------------------------------------------------------------------------------
# One request_id per dispute and action, ever. A second challenge on the same dispute derives
# the SAME request_id, so the spine's operation store returns the first result (same body) or
# refuses it (different body): a re-challenge cannot reach Airwallex through this kit.


def business_key(dispute_id: str) -> str:
    return f"dispute:{dispute_id}"


def _stage(stage: str | None) -> str:
    if stage not in STAGES:
        raise ProviderShapeError(f"dispute stage {stage!r} is not one this kit can answer")
    return stage


def _segment(action: str, stage: str | None, attempt: int) -> str:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive int")
    base = f"{action}:{_stage(stage)}"
    return base if attempt == 1 else f"{base}:{attempt}"


def accept_request_id(dispute_id: str, stage: str | None, attempt: int = 1) -> str:
    """One per dispute, action, stage and attempt. A stage that lapsed without our response is a
    definitive non-landing, so the next stage's response gets its own key; a response a person
    cleared after a fresh empty read-back gets the next attempt's key (rule M1)."""
    return derive_request_id(KIT, business_key(dispute_id), _segment("accept", stage, attempt))


def challenge_request_id(dispute_id: str, stage: str | None, attempt: int = 1) -> str:
    return derive_request_id(KIT, business_key(dispute_id), _segment("challenge", stage, attempt))


def upload_request_id(dispute_id: str, evidence_id: str, book_id: str, run_id: str) -> str:
    """Per run: an upload that failed definitively is retried by the next run under its own key
    (rule M1). evidence_id is kit-local, so the key carries the book id (M2)."""
    return derive_request_id(
        KIT, business_key(dispute_id), f"upload:{book_id}:{run_id}:{evidence_id}"
    )


ACCEPT_OP = "disputes.accept"  # awx_spine endpoint names
CHALLENGE_OP = "disputes.challenge"


def accept_operation(
    dispute_id: str,
    stage: str | None,
    reason: AcceptReason,
    intent_hash: str,
    attempt: int = 1,
) -> Operation:
    rid = accept_request_id(dispute_id, stage, attempt)
    return Operation(
        kit=KIT,
        name=ACCEPT_OP,
        method="POST",
        path=f"{DISPUTES_PATH}/{dispute_id}/accept",
        body={
            "request_id": rid,
            "reason": PROVIDER_ACCEPT_REASON[reason],
            "description": ACCEPT_DESCRIPTION[reason],
        },
        request_id=rid,
        business_key=business_key(dispute_id),
        intent_hash=intent_hash,
    )


def upload_operation(
    dispute_id: str,
    evidence_id: str,
    ref: EvidenceRef,
    intent_hash: str,
    *,
    book_id: str,
    run_id: str,
) -> Operation:
    """A file upload. The body carries an awx_common EvidenceRef, never bytes: the executor
    loads the stored object, checks its size and SHA-256 and posts it to the files host.
    `ref` comes from the code-owned evidence index (evidence.py), never from the model."""
    return Operation(
        kit=KIT,
        name=FILES_UPLOAD_OP,
        method="POST",
        path=FILES_UPLOAD_PATH,
        body={"evidence_ref": ref.to_body(), "notes": f"poc-k4-{dispute_id}-{evidence_id}"},
        request_id=upload_request_id(dispute_id, evidence_id, book_id, run_id),
        business_key=business_key(dispute_id),
        intent_hash=intent_hash,
    )


def challenge_operation(
    dispute_id: str,
    stage: str | None,
    reason: ChallengeReason,
    documents: list[tuple[str, str]],  # (document type, file_id)
    product_type: str,
    product_description: str,
    intent_hash: str,
    attempt: int = 1,
) -> Operation:
    grouped: dict[str, list[str]] = {}
    for doc_type, file_id in documents:
        if doc_type not in DOCUMENT_TYPES:
            raise ValueError(f"document type {doc_type!r} is not one Airwallex takes")
        grouped.setdefault(doc_type, []).append(file_id)
    rid = challenge_request_id(dispute_id, stage, attempt)
    return Operation(
        kit=KIT,
        name=CHALLENGE_OP,
        method="POST",
        path=f"{DISPUTES_PATH}/{dispute_id}/challenge",
        body={
            "request_id": rid,
            "reason": PROVIDER_CHALLENGE_REASON[reason],
            "supporting_documents": {
                "documents": [
                    {"type": t, "file_ids": sorted(ids)} for t, ids in sorted(grouped.items())
                ]
            },
            "product_type": product_type,
            "product_description": product_description,
        },
        request_id=rid,
        business_key=business_key(dispute_id),
        intent_hash=intent_hash,
    )


def file_id_of(body: Mapping[str, Any] | None, provider_id: str | None) -> str | None:
    if provider_id:
        return provider_id
    if body:
        for key in ("file_id", "id"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return None
