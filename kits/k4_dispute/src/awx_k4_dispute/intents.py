"""Tool input -> canonical intent. Strict: unknown keys, wrong types and floats are refused.

The canonical intent is what an approver sees and what the approval hash covers: dispute,
disputed amount, typed reason, evidence ids and hashes, cited facts, narrative and the policy
version. Lists are sorted so the same request always hashes the same way.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from awx_common import Money

from .config import DEFAULT_CONFIG, DisputeConfig
from .evidence import EVIDENCE_ID_PATTERN, EvidenceFile, EvidenceKind, Fact, FactKind
from .ledger import CloseReason
from .provider import ID_PATTERN, AcceptReason, ChallengeReason

_ID = re.compile(ID_PATTERN)
_AMOUNT = r"^[0-9]{1,12}(\.[0-9]{1,3})?$"
_AMOUNT_RE = re.compile(_AMOUNT)
_CURRENCY = r"^[A-Z]{3}$"


class IntentError(ValueError):
    """The tool input cannot become an intent."""


def _keys(raw: Mapping[str, Any], required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(raw, Mapping):
        raise IntentError(f"{where} must be an object")
    keys = set(raw)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise IntentError(f"{where} is missing {sorted(missing)}")
    if extra:
        raise IntentError(f"{where} has unexpected fields {sorted(extra)}")


def _text(raw: Mapping[str, Any], key: str, limit: int, *, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise IntentError(f"{key} must be a string")
    cleaned = "".join(ch for ch in value if ch == "\n" or ch >= " ").strip()
    if required and not cleaned:
        raise IntentError(f"{key} must not be empty")
    if len(cleaned) > limit:
        raise IntentError(f"{key} is longer than {limit} characters")
    return cleaned


def _dispute_id(raw: Mapping[str, Any]) -> str:
    value = raw.get("dispute_id")
    if not isinstance(value, str) or not _ID.match(value):
        raise IntentError("dispute_id is malformed")
    return value


def _money(raw: Any, key: str) -> Money:
    _keys(raw, {"amount", "currency"}, set(), key)
    amount, currency = raw["amount"], raw["currency"]
    if not isinstance(amount, str) or not _AMOUNT_RE.match(amount):
        raise IntentError(f"{key}.amount must be a decimal string such as '900.00'")
    if not isinstance(currency, str) or not re.match(_CURRENCY, currency):
        raise IntentError(f"{key}.currency must be a 3-letter code")
    try:
        return Money.of(amount, currency)
    except ValueError as exc:
        raise IntentError(f"{key}: {exc}") from exc


def _enum(enum_type: type, raw: Mapping[str, Any], key: str):
    try:
        return enum_type(raw.get(key))
    except ValueError as exc:
        raise IntentError(f"{key} must be one of {[e.value for e in enum_type]}") from exc


_EVIDENCE_ID_RE = re.compile(EVIDENCE_ID_PATTERN)


def parse_evidence_ids(items: Any, config: DisputeConfig) -> list[str]:
    """The model names evidence only by id. Size, hash, type and kind come from the index."""
    if not isinstance(items, list):
        raise IntentError("evidence_ids must be a list")
    if len(items) > config.max_evidence_files:
        raise IntentError(f"at most {config.max_evidence_files} evidence files")
    for i, item in enumerate(items):
        if not isinstance(item, str) or not _EVIDENCE_ID_RE.match(item):
            raise IntentError(f"evidence_ids[{i}] is malformed")
    if len(set(items)) != len(items):
        raise IntentError("evidence ids must be unique")
    return sorted(items)


def parse_facts(items: Any) -> list[Fact]:
    if not isinstance(items, list):
        raise IntentError("facts must be a list")
    if len(items) > 50:
        raise IntentError("at most 50 facts")
    out: list[Fact] = []
    for i, item in enumerate(items):
        where = f"facts[{i}]"
        _keys(item, {"kind", "evidence_id", "quote"}, {"order_ref"}, where)
        order_ref = item.get("order_ref")
        if order_ref is not None and (not isinstance(order_ref, str) or len(order_ref) > 64):
            raise IntentError(f"{where}.order_ref must be a short string")
        if not isinstance(item["evidence_id"], str):
            raise IntentError(f"{where}.evidence_id must be a string")
        out.append(
            Fact(
                kind=_enum(FactKind, item, "kind"),
                evidence_id=item["evidence_id"],
                quote=_text(item, "quote", 500),
                order_ref=order_ref.strip() if isinstance(order_ref, str) else None,
            )
        )
    unique = {(f.kind, f.evidence_id, f.quote, f.order_ref) for f in out}
    if len(unique) != len(out):
        raise IntentError("duplicate facts")
    return sorted(out, key=lambda f: (f.kind.value, f.evidence_id, f.order_ref or "", f.quote))


def evidence_from_computed(computed: Mapping[str, Any]) -> list[tuple[EvidenceFile, str]]:
    """(file, store_id) pairs that policy resolved from the evidence index, in id order."""
    return [
        (
            EvidenceFile(
                evidence_id=e["evidence_id"],
                file_name=e["file_name"],
                content_type=e["content_type"],
                sha256=e["sha256"],
                size_bytes=e["size_bytes"],
                kind=EvidenceKind(e["kind"]),
            ),
            e["store_id"],
        )
        for e in computed.get("evidence", [])
    ]


def facts_from_intent(intent: Mapping[str, Any]) -> list[Fact]:
    return [
        Fact(FactKind(f["kind"]), f["evidence_id"], f["quote"], f.get("order_ref"))
        for f in intent.get("facts", [])
    ]


def money_from_intent(intent: Mapping[str, Any]) -> Money:
    return Money.from_dict(intent["disputed_amount"])


def validate_accept(raw: Mapping[str, Any], config: DisputeConfig = DEFAULT_CONFIG):
    _keys(raw, {"dispute_id", "disputed_amount", "reason", "rationale"}, set(), "input")
    return {
        "dispute_id": _dispute_id(raw),
        "disputed_amount": _money(raw["disputed_amount"], "disputed_amount").to_dict(),
        "reason": _enum(AcceptReason, raw, "reason").value,
        "rationale": _text(raw, "rationale", 1000),
        "policy_version": config.policy_version,
    }


def validate_challenge(raw: Mapping[str, Any], config: DisputeConfig = DEFAULT_CONFIG):
    _keys(
        raw,
        {"dispute_id", "disputed_amount", "reason", "narrative", "evidence_ids", "facts"},
        set(),
        "input",
    )
    return {
        "dispute_id": _dispute_id(raw),
        "disputed_amount": _money(raw["disputed_amount"], "disputed_amount").to_dict(),
        "reason": _enum(ChallengeReason, raw, "reason").value,
        "narrative": _text(raw, "narrative", config.max_narrative_chars),
        "evidence_ids": parse_evidence_ids(raw["evidence_ids"], config),
        "facts": [f.to_intent() for f in parse_facts(raw["facts"])],
        "policy_version": config.policy_version,
    }


def validate_refer(raw: Mapping[str, Any], config: DisputeConfig = DEFAULT_CONFIG):
    _keys(raw, {"dispute_id", "summary"}, {"question"}, "input")
    return {
        "dispute_id": _dispute_id(raw),
        "summary": _text(raw, "summary", 2000),
        "question": _text(raw, "question", 500, required=False),
        "policy_version": config.policy_version,
    }


def validate_close(raw: Mapping[str, Any], config: DisputeConfig = DEFAULT_CONFIG):
    _keys(raw, {"dispute_id", "close_reason"}, {"note"}, "input")
    return {
        "dispute_id": _dispute_id(raw),
        "close_reason": _enum(CloseReason, raw, "close_reason").value,
        "note": _text(raw, "note", 1000, required=False),
        "policy_version": config.policy_version,
    }


def validate_clear(raw: Mapping[str, Any], config: DisputeConfig = DEFAULT_CONFIG):
    _keys(raw, {"dispute_id", "request_ref", "note"}, set(), "input")
    ref = raw["request_ref"]
    if not isinstance(ref, str) or not re.match(r"^[0-9a-f-]{36}$", ref):
        raise IntentError("request_ref must be the attempt's request_id from read_dispute")
    return {
        "dispute_id": _dispute_id(raw),
        "request_ref": ref,
        "note": _text(raw, "note", 1000),
        "policy_version": config.policy_version,
    }


def validate_read_economics(raw: Mapping[str, Any], config: DisputeConfig = DEFAULT_CONFIG):
    _keys(raw, {"dispute_id"}, {"evidence_ids", "facts"}, "input")
    return {
        "dispute_id": _dispute_id(raw),
        "evidence_ids": parse_evidence_ids(raw.get("evidence_ids", []), config),
        "facts": parse_facts(raw.get("facts", [])),
    }


# ---- JSON Schemas for the tools (mirror the validators above) ----------------------------------

DISPUTE_ID_SCHEMA = {"type": "string", "pattern": ID_PATTERN}
MONEY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["amount", "currency"],
    "properties": {
        "amount": {"type": "string", "pattern": _AMOUNT},
        "currency": {"type": "string", "pattern": _CURRENCY},
    },
}


def _enum_schema(values: Sequence[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


EVIDENCE_SCHEMA = {
    "type": "array",
    "maxItems": DEFAULT_CONFIG.max_evidence_files,
    "items": {"type": "string", "pattern": EVIDENCE_ID_PATTERN},
}
FACTS_SCHEMA = {
    "type": "array",
    "maxItems": 50,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "evidence_id", "quote"],
        "properties": {
            "kind": _enum_schema([k.value for k in FactKind]),
            "evidence_id": {"type": "string"},
            "quote": {"type": "string", "maxLength": 500},
            "order_ref": {"type": "string", "maxLength": 64},
        },
    },
}
