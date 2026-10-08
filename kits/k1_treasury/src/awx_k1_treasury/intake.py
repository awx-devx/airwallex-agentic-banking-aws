"""Tool-input checks. The model names IDs and cites evidence; the book supplies the rest.

validate has no PolicyContext, so it checks shape only. Policy resolves the IDs against the
book in ctx.store and BLOCKs unknown ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_OBLIGATIONS = 5
MAX_CONTRADICTIONS = 10


class InvalidInput(ValueError):
    pass


def require_keys(raw: Any, allowed: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidInput(f"{where} must be an object")
    extra = set(raw) - allowed
    if extra:
        raise InvalidInput(f"{where} has unexpected fields {sorted(extra)}")
    return raw


def _text(value: Any, name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise InvalidInput(f"{name} must be a non-empty string of at most {max_len} characters")
    return value


def parse_contradictions(raw: Any) -> list[dict[str, str]]:
    """Each contradiction names a receipt and cites its source line. Sorted, deduplicated."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_CONTRADICTIONS:
        raise InvalidInput(f"contradictions must be a list of at most {MAX_CONTRADICTIONS}")
    out = set()
    for item in raw:
        require_keys(item, {"receipt_id", "source", "quote"}, "contradiction")
        out.add(
            (
                _text(item.get("receipt_id"), "receipt_id", 64),
                _text(item.get("source"), "source", 200),
                _text(item.get("quote"), "quote", 500),
            )
        )
    return [{"receipt_id": r, "source": s, "quote": q} for r, s, q in sorted(out)]


def parse_obligation_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OBLIGATIONS:
        raise InvalidInput(f"obligation_ids must list 1 to {MAX_OBLIGATIONS} ids")
    ids = [_text(i, "obligation_id", 64) for i in raw]
    if len(set(ids)) != len(ids):
        raise InvalidInput("obligation_ids must be unique")
    return sorted(ids)
