"""The evidence pack: files, the model's cited facts, and what code makes of them.

The model reads JPG and PDF evidence and returns typed facts, each citing an evidence file and
a quote. It never scores, counts or weighs. Code checks every citation, counts distinct prior
orders, derives a strength band from the verified facts, and checks the minimum evidence for
the reason family. Text inside a document is data: instruction-like text is flagged for the
approver and changes nothing else.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .config import BAND_ORDER, DisputeConfig, StrengthBand
from .provider import ReasonFamily

EVIDENCE_ID_PATTERN = r"^ev_[A-Za-z0-9_-]{1,60}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
_EVIDENCE_ID = re.compile(EVIDENCE_ID_PATTERN)
_SHA256 = re.compile(SHA256_PATTERN)

CONTENT_TYPES: dict[str, tuple[str, ...]] = {
    "image/jpeg": (".jpg", ".jpeg"),
    "application/pdf": (".pdf",),
}
_MAGIC: dict[str, bytes] = {"image/jpeg": b"\xff\xd8\xff", "application/pdf": b"%PDF-"}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class EvidenceKind(StrEnum):
    DELIVERY_PROOF = "DELIVERY_PROOF"
    ORDER_HISTORY = "ORDER_HISTORY"
    DEVICE_IP_LOG = "DEVICE_IP_LOG"
    CUSTOMER_COMMUNICATION = "CUSTOMER_COMMUNICATION"
    REFUND_POLICY = "REFUND_POLICY"
    OTHER = "OTHER"


# Airwallex supporting_documents type for each evidence kind (FAKE-FIDELITY "Challenge body").
DOCUMENT_TYPE: dict[EvidenceKind, str] = {
    EvidenceKind.DELIVERY_PROOF: "PRIMARY",
    EvidenceKind.DEVICE_IP_LOG: "PRIMARY",
    EvidenceKind.ORDER_HISTORY: "ORDER",
    EvidenceKind.CUSTOMER_COMMUNICATION: "CUSTOMER",
    EvidenceKind.REFUND_POLICY: "OTHER",
    EvidenceKind.OTHER: "OTHER",
}


class FactKind(StrEnum):
    SIGNED_DELIVERY = "SIGNED_DELIVERY"
    DELIVERY_SCAN_NO_SIGNATURE = "DELIVERY_SCAN_NO_SIGNATURE"
    DELIVERED_TO_BILLING_ADDRESS = "DELIVERED_TO_BILLING_ADDRESS"
    DEVICE_FINGERPRINT_MATCH_PRIOR = "DEVICE_FINGERPRINT_MATCH_PRIOR"
    IP_MATCH_PRIOR = "IP_MATCH_PRIOR"
    PRIOR_UNDISPUTED_ORDER = "PRIOR_UNDISPUTED_ORDER"  # one fact per order, with order_ref
    CUSTOMER_CONTACTED_SUPPORT = "CUSTOMER_CONTACTED_SUPPORT"  # one fact per contact
    NO_SUPPORT_REPLY = "NO_SUPPORT_REPLY"
    REFUND_ISSUED = "REFUND_ISSUED"
    CANCELLATION_POLICY_ACCEPTED = "CANCELLATION_POLICY_ACCEPTED"


class EvidenceError(ValueError):
    """An evidence file or citation is not acceptable."""


@dataclass(frozen=True)
class EvidenceFile:
    """Metadata for one evidence object. Bytes stay in the evidence store (S3 when deployed)."""

    evidence_id: str
    file_name: str
    content_type: str
    sha256: str
    size_bytes: int
    kind: EvidenceKind

    def to_intent(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "file_name": self.file_name,
            "content_type": self.content_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind.value,
        }


def check_evidence_file(item: EvidenceFile, config: DisputeConfig) -> None:
    if not _EVIDENCE_ID.match(item.evidence_id):
        raise EvidenceError(f"evidence_id {item.evidence_id!r} is malformed")
    if not _SHA256.match(item.sha256):
        raise EvidenceError(f"{item.evidence_id}: sha256 must be 64 lower-case hex characters")
    if item.content_type not in CONTENT_TYPES:
        raise EvidenceError(
            f"{item.evidence_id}: {item.content_type} is not accepted; Airwallex takes JPG or PDF"
        )
    if not item.file_name.lower().endswith(CONTENT_TYPES[item.content_type]):
        raise EvidenceError(f"{item.evidence_id}: file name does not match {item.content_type}")
    if "/" in item.file_name or "\\" in item.file_name or not item.file_name.strip():
        raise EvidenceError(f"{item.evidence_id}: file name must be a bare name")
    if isinstance(item.size_bytes, bool) or not 0 < item.size_bytes <= config.max_evidence_bytes:
        raise EvidenceError(f"{item.evidence_id}: size must be 1..{config.max_evidence_bytes}")


def inspect_bytes(
    evidence_id: str, file_name: str, data: bytes, kind: EvidenceKind, config: DisputeConfig
) -> EvidenceFile:
    """Build EvidenceFile from the bytes themselves: the content type comes from the magic
    number, never from the extension or the uploader. PNG is refused (Airwallex rejects it)."""
    if data.startswith(_PNG_MAGIC):
        raise EvidenceError(f"{evidence_id}: PNG is rejected by Airwallex; convert to JPG or PDF")
    content_type = next((ct for ct, magic in _MAGIC.items() if data.startswith(magic)), None)
    if content_type is None:
        raise EvidenceError(f"{evidence_id}: not a JPG or PDF by content")
    item = EvidenceFile(
        evidence_id=evidence_id,
        file_name=file_name,
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        kind=kind,
    )
    check_evidence_file(item, config)
    return item


@dataclass(frozen=True)
class Fact:
    kind: FactKind
    evidence_id: str
    quote: str
    order_ref: str | None = None

    def to_intent(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind.value,
            "evidence_id": self.evidence_id,
            "quote": self.quote,
        }
        if self.order_ref is not None:
            out["order_ref"] = self.order_ref
        return out


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


@dataclass(frozen=True)
class FactCheck:
    verified: tuple[Fact, ...]
    rejected: tuple[tuple[Fact, str], ...]
    quotes_checked: bool  # False when no evidence text was available to check quotes against


def verify_facts(
    facts: Sequence[Fact],
    evidence: Sequence[EvidenceFile],
    texts: Mapping[str, str] | None = None,
) -> FactCheck:
    """Keep a fact only if it cites a file in this pack and, when the file's extracted text is
    known, the quote really appears in it. A model cannot strengthen a case by inventing a
    citation. quotes_checked is True only when every verified fact was checked against text."""
    ids = {e.evidence_id for e in evidence}
    norm_texts = {k: _normalise(v) for k, v in (texts or {}).items()}
    verified: list[Fact] = []
    rejected: list[tuple[Fact, str]] = []
    for fact in facts:
        if fact.evidence_id not in ids:
            rejected.append((fact, "cites a file that is not in the evidence pack"))
            continue
        if not fact.quote.strip():
            rejected.append((fact, "has no quote"))
            continue
        if fact.kind is FactKind.PRIOR_UNDISPUTED_ORDER and not (fact.order_ref or "").strip():
            rejected.append((fact, "prior order fact has no order_ref"))
            continue
        text = norm_texts.get(fact.evidence_id)
        if text is not None and _normalise(fact.quote) not in text:
            rejected.append((fact, "quote does not appear in the cited file"))
            continue
        verified.append(fact)
    all_checked = all(f.evidence_id in norm_texts for f in verified)
    return FactCheck(tuple(verified), tuple(rejected), bool(norm_texts) and all_checked)


@dataclass(frozen=True)
class EvidenceAssessment:
    band: StrengthBand
    merchant_fault: bool  # verified facts say the merchant likely owes the customer
    contradictory: bool
    prior_orders: int
    support_contacts: int
    reasons: tuple[str, ...]


def _cap(band: StrengthBand, ceiling: StrengthBand) -> StrengthBand:
    return band if BAND_ORDER[band] <= BAND_ORDER[ceiling] else ceiling


def _has(facts: Iterable[Fact], kind: FactKind) -> bool:
    return any(f.kind is kind for f in facts)


def assess(
    family: ReasonFamily, facts: Sequence[Fact], config: DisputeConfig
) -> EvidenceAssessment:
    """Strength band from VERIFIED facts only. Counting is code's job."""
    prior = len(
        {
            (f.order_ref or "").strip().casefold()
            for f in facts
            if f.kind is FactKind.PRIOR_UNDISPUTED_ORDER
        }
    )
    contacts = sum(1 for f in facts if f.kind is FactKind.CUSTOMER_CONTACTED_SUPPORT)
    signed = _has(facts, FactKind.SIGNED_DELIVERY)
    unsigned = _has(facts, FactKind.DELIVERY_SCAN_NO_SIGNATURE)
    device = _has(facts, FactKind.DEVICE_FINGERPRINT_MATCH_PRIOR)
    ip = _has(facts, FactKind.IP_MATCH_PRIOR)
    billing = _has(facts, FactKind.DELIVERED_TO_BILLING_ADDRESS)
    no_reply = _has(facts, FactKind.NO_SUPPORT_REPLY)
    refunded = _has(facts, FactKind.REFUND_ISSUED)
    policy = _has(facts, FactKind.CANCELLATION_POLICY_ACCEPTED)
    reasons: list[str] = []
    contradictory = signed and unsigned
    if contradictory:
        reasons.append("delivery evidence contradicts itself (signed and unsigned)")
    merchant_fault = contacts >= 1 and no_reply
    if merchant_fault:
        reasons.append(f"customer contacted support {contacts} time(s) with no reply")
    linked = device and ip and prior >= config.min_prior_undisputed_orders

    band = StrengthBand.NONE
    if family is ReasonFamily.FRAUD_CARD_ABSENT:
        if linked and signed:
            band = StrengthBand.STRONG
        elif linked or signed:
            band = StrengthBand.MODERATE
        elif device or ip or prior >= 1 or billing:
            band = StrengthBand.WEAK
    elif family is ReasonFamily.NOT_RECEIVED:
        if signed:
            band = StrengthBand.STRONG
        elif billing:
            band = StrengthBand.MODERATE
        elif unsigned:
            band = StrengthBand.WEAK
    elif family is ReasonFamily.CREDIT_NOT_PROCESSED:
        if refunded:
            band = StrengthBand.STRONG
        elif policy and not merchant_fault:
            band = StrengthBand.MODERATE
        elif policy:
            band = StrengthBand.WEAK
    else:
        reasons.append("reason code is not one this kit models; a person decides")
    if contradictory:
        band = _cap(band, StrengthBand.WEAK)
    if merchant_fault and family is ReasonFamily.CREDIT_NOT_PROCESSED and not refunded:
        band = _cap(band, StrengthBand.WEAK)
    reasons.append(
        f"evidence band {band.value}: prior orders {prior}, signed {signed}, "
        f"device {device}, ip {ip}"
    )
    return EvidenceAssessment(band, merchant_fault, contradictory, prior, contacts, tuple(reasons))


# Minimum evidence to challenge, per family: at least one file of one of these kinds.
REQUIRED_KINDS: dict[ReasonFamily, frozenset[EvidenceKind]] = {
    ReasonFamily.FRAUD_CARD_ABSENT: frozenset(
        {EvidenceKind.ORDER_HISTORY, EvidenceKind.DEVICE_IP_LOG, EvidenceKind.DELIVERY_PROOF}
    ),
    ReasonFamily.NOT_RECEIVED: frozenset({EvidenceKind.DELIVERY_PROOF}),
    ReasonFamily.CREDIT_NOT_PROCESSED: frozenset(
        {EvidenceKind.REFUND_POLICY, EvidenceKind.CUSTOMER_COMMUNICATION}
    ),
}
MIN_FILES: dict[ReasonFamily, int] = {
    ReasonFamily.FRAUD_CARD_ABSENT: 2,
    ReasonFamily.NOT_RECEIVED: 1,
    ReasonFamily.CREDIT_NOT_PROCESSED: 1,
}


def minimum_evidence_gaps(
    family: ReasonFamily, evidence: Sequence[EvidenceFile], verified: Sequence[Fact]
) -> tuple[str, ...]:
    gaps: list[str] = []
    if family not in REQUIRED_KINDS:
        return ("reason family has no evidence rule; it cannot be challenged by this kit",)
    if len(evidence) < MIN_FILES[family]:
        gaps.append(f"needs at least {MIN_FILES[family]} evidence file(s)")
    if not {e.kind for e in evidence} & REQUIRED_KINDS[family]:
        kinds = ", ".join(sorted(k.value for k in REQUIRED_KINDS[family]))
        gaps.append(f"needs a file of kind {kinds}")
    cited = {f.evidence_id for f in verified}
    if not cited:
        gaps.append("no verified fact cites the evidence")
    return tuple(gaps)


_INSTRUCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("override", r"\b(ignore|disregard|forget)\b.{0,40}\b(instructions?|rules?|prompt)\b"),
        ("role", r"\b(you are now|act as|system\s*:|assistant\s*:|developer\s*:)"),
        ("accept", r"\baccept\b.{0,30}\b(dispute|chargeback|claim)\b"),
        ("refund", r"\b(issue|send|process|make)\b.{0,30}\brefund\s+(of|to)\b"),
        ("transfer", r"\b(transfer|wire|pay)\b.{0,30}\b(to|into)\b.{0,30}\baccount\b"),
        ("approval", r"\b(no|without|skip)\b.{0,20}\b(approval|approver|review)\b"),
        ("tool", r"\b(propose|call|invoke)_?\w*\b.{0,20}\b(tool|function)\b"),
    )
)


def instruction_markers(text: str) -> tuple[str, ...]:
    """Names of instruction-like patterns in document text. Flags for the approver only: a
    marker never lowers a risk tier and never triggers or blocks a write."""
    norm = unicodedata.normalize("NFKC", text)
    return tuple(name for name, pattern in _INSTRUCTION_PATTERNS if pattern.search(norm))


# ---- the code-owned evidence index (PolicyContext.store) ------------------------------------
# Ingest (the upload handler when deployed, the test harness offline) reads the bytes, sniffs
# the content type from the magic number, hashes them and writes one index record per dispute
# and file. The model only names evidence_ids; policy resolves each one here and blocks anything
# missing, filed under another dispute, or whose stored metadata is malformed. The bytes go into
# the EvidenceStore under store_id, a deterministic per-tenant-unique id, so two disputes can
# each have an "ev_signature" without colliding.


def index_key(dispute_id: str, evidence_id: str) -> str:
    return f"evidence/{dispute_id}/{evidence_id}"


def store_id(dispute_id: str, evidence_id: str) -> str:
    digest = hashlib.sha256(f"k4\x1f{dispute_id}\x1f{evidence_id}".encode()).hexdigest()
    return f"k4e_{digest[:40]}"


@dataclass(frozen=True)
class IndexedEvidence:
    dispute_id: str
    file: EvidenceFile
    store_id: str
    text: str | None  # code-extracted text (OCR or PDF text), None when not extracted
    markers: tuple[str, ...]


def ingest_evidence(
    store: Any,
    dispute_id: str,
    evidence_id: str,
    file_name: str,
    data: bytes,
    kind: EvidenceKind,
    config: DisputeConfig,
    *,
    text: str | None = None,
) -> IndexedEvidence:
    """Index one evidence file. Idempotent for the same bytes; refuses different bytes under
    an existing id. Metadata comes from the bytes, never from the model."""
    item = inspect_bytes(evidence_id, file_name, data, kind, config)
    record = {
        "dispute_id": dispute_id,
        **item.to_intent(),
        "store_id": store_id(dispute_id, evidence_id),
        "text": text,
        "markers": list(instruction_markers(text or "")),
    }
    key = index_key(dispute_id, evidence_id)
    existing = store.get(key)
    if existing is not None:
        if existing.value.get("sha256") != item.sha256:
            raise EvidenceError(f"{evidence_id} is already indexed with different bytes")
        return _from_record(existing.value)
    store.put(key, record, expected_version=None)
    return _from_record(record)


def _from_record(record: Mapping[str, Any]) -> IndexedEvidence:
    file = EvidenceFile(
        evidence_id=record["evidence_id"],
        file_name=record["file_name"],
        content_type=record["content_type"],
        sha256=record["sha256"],
        size_bytes=record["size_bytes"],
        kind=EvidenceKind(record["kind"]),
    )
    return IndexedEvidence(
        dispute_id=record["dispute_id"],
        file=file,
        store_id=record["store_id"],
        text=record.get("text"),
        markers=tuple(record.get("markers") or ()),
    )


def resolve_evidence(
    store: Any, dispute_id: str, evidence_ids: Sequence[str], config: DisputeConfig
) -> tuple[list[IndexedEvidence], list[str]]:
    """Look up each proposed evidence_id for this dispute. Returns (found, problems)."""
    found: list[IndexedEvidence] = []
    problems: list[str] = []
    if store is None:
        return [], ["no evidence index is available"] if evidence_ids else []
    for eid in evidence_ids:
        item = store.get(index_key(dispute_id, eid))
        if item is None:
            problems.append(f"{eid} is not in this dispute's evidence index")
            continue
        try:
            entry = _from_record(item.value)
            check_evidence_file(entry.file, config)
        except (EvidenceError, KeyError, ValueError) as exc:
            problems.append(f"{eid}: indexed metadata is not acceptable ({exc})")
            continue
        if entry.dispute_id != dispute_id or entry.file.evidence_id != eid:
            problems.append(f"{eid} is filed under another dispute")
            continue
        found.append(entry)
    return found, problems
