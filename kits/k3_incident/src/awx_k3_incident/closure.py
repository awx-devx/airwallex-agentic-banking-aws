"""Incident closure. An incident closes only when BOTH dispositions are recorded.

The guide's trap: closing because the replacement reached PAID while nobody recorded what
happened to the original. The closure record keeps the original's failure (the provider's
`failure` object: code and message) next to the replacement's outcome, and is written once to
the kit store under `closure:<original id>`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from awx_common.store import KitStore, VersionConflict

from .finality import TERMINAL_STATUS, failure_code_of, failure_of, failure_type_of

REPLACEMENT_TERMINAL = frozenset({"PAID", "CANCELLED"})
CLOSURE_PREFIX = "closure:"


class IncidentNotClosable(ValueError):
    """A disposition is missing or not yet terminal."""


@dataclass(frozen=True)
class IncidentClosure:
    original_transfer_id: str
    original_status: str
    original_failure_code: str
    original_failure_type: str | None
    original_failure: Mapping[str, Any]  # {"code", "message"} as Airwallex reported it
    replacement_transfer_id: str
    replacement_request_id: str
    replacement_status: str

    @property
    def resolved(self) -> bool:
        """The obligation was paid exactly once, by the replacement."""
        return self.replacement_status == "PAID"

    def record(self) -> dict[str, Any]:
        return {**asdict(self), "original_failure": dict(self.original_failure)}


def closure_record(
    original: Mapping[str, Any], replacement: Mapping[str, Any], *, replacement_request_id: str
) -> IncidentClosure:
    if original.get("status") != TERMINAL_STATUS:
        raise IncidentNotClosable(f"original is {original.get('status')!r}, not CANCELLED")
    code, doubts = failure_code_of(original)
    if code is None or doubts:
        raise IncidentNotClosable("original failure record is missing or inconsistent")
    status = replacement.get("status")
    if status not in REPLACEMENT_TERMINAL:
        raise IncidentNotClosable(f"replacement is {status!r}, not terminal")
    if replacement.get("request_id") not in (None, replacement_request_id):
        raise IncidentNotClosable("replacement carries a different request_id")
    if replacement.get("id") == original.get("id"):
        raise IncidentNotClosable("replacement and original are the same transfer")
    failure = failure_of(original) or {}
    message = failure.get("message") or original.get("failure_reason")
    return IncidentClosure(
        original_transfer_id=str(original.get("id")),
        original_status=TERMINAL_STATUS,
        original_failure_code=code,
        original_failure_type=failure_type_of(original)[0],
        original_failure={"code": code, "message": message},
        replacement_transfer_id=str(replacement.get("id")),
        replacement_request_id=replacement_request_id,
        replacement_status=str(status),
    )


def record_closure(store: KitStore | None, closure: IncidentClosure) -> bool:
    """Write the closure once. Idempotent: an identical existing record is fine; a different
    one is a conflict a person must look at. Returns True when a record is (now) stored."""
    if store is None:
        return False
    key = f"{CLOSURE_PREFIX}{closure.original_transfer_id}"
    value = closure.record()
    try:
        store.put(key, value, expected_version=None)
    except VersionConflict:
        existing = store.get(key)
        if existing is None or dict(existing.value) != value:
            raise IncidentNotClosable("a different closure is already recorded") from None
    return True
