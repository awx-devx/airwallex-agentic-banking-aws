"""Operation records: written before the Airwallex call, completed after it.

One record per request_id. begin() is idempotent: the same Operation returns the stored record,
while the same request_id with a different body raises RequestIdReuse, because sending it would
either be rejected by Airwallex or, worse, be read as a retry of a different payment.

body_hash covers everything the provider sees (method, path, x-on-behalf-of, body), so moving an
operation to another connected account is a different body.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from awx_common import Operation, OperationResult, OperationStatus, intent_hash

PENDING = "PENDING"


class RequestIdReuse(Exception):
    """Same request_id, different body. Never send it."""

    def __init__(self, request_id: str, *, stored_hash: str, new_hash: str) -> None:
        super().__init__(
            f"request_id {request_id} is already bound to a different operation body "
            f"(stored {stored_hash[:12]}, new {new_hash[:12]})"
        )
        self.request_id = request_id
        self.stored_hash = stored_hash
        self.new_hash = new_hash


class UnknownRequestId(KeyError):
    """record_attempt or complete for a request_id that was never begun."""


def body_hash(op: Operation) -> str:
    return intent_hash(
        {"method": op.method, "path": op.path, "on_behalf_of": op.on_behalf_of, "body": op.body}
    )


@dataclass(frozen=True)
class OperationRecord:
    request_id: str
    kit: str
    name: str
    business_key: str
    intent_hash: str
    body_hash: str
    status: str  # "PENDING" or an OperationStatus value
    attempts: int
    result: OperationResult | None
    created_at: datetime
    updated_at: datetime
    # Set once any send of this request_id may have been processed without a
    # known result (complete() with OUTCOME_UNKNOWN). Never cleared.
    may_have_landed: bool = False
    # When the first attempt started (written once, never overwritten) and when
    # the current one started (every record_attempt). Both None before any attempt.
    first_attempt_at: datetime | None = None
    attempt_started_at: datetime | None = None


class OperationStore(Protocol):
    """Semantics every implementation must keep (awx_spine_aws's DynamoDbOperationStore included).

    begin(op)        idempotent on request_id; a different body_hash raises RequestIdReuse.
    record_attempt   called immediately BEFORE each send. It increments attempts AND sets status
                     to PENDING, atomically (one conditional UpdateItem in DynamoDB). The previous
                     result may be kept as history. A record that is PENDING with attempts > 0
                     therefore always means "a send may have left and was never completed", even
                     when an earlier attempt had ended in a retryable FAILED (429, not sent), so
                     a resumed executor reconciles before any resend (runner rule 9).
    complete         sets status to the result's status and stores the result. When the result is
                     OUTCOME_UNKNOWN it also sets may_have_landed, in the same write (one
                     UpdateItem in DynamoDB). may_have_landed is never cleared: not by a later
                     complete, not by record_attempt. While it is set the
                     executor reconciles before every send and never stores any FAILED for it
                     (a request_id_reused mismatch is OUTCOME_UNKNOWN too).
    get              the current record or None.

    record_attempt also sets attempt_started_at = now and, only if it is not set yet,
    first_attempt_at = now, in the same write as the increment (DynamoDB: `SET
    attempt_started_at = :now, first_attempt_at = if_not_exists(first_attempt_at, :now)` in the
    one UpdateItem). first_attempt_at is never overwritten. The executor uses it for the
    6-day deduplication margin and attempt_started_at for the attempt lease.
    """

    def begin(self, op: Operation) -> OperationRecord: ...
    def record_attempt(self, request_id: str) -> OperationRecord: ...
    def complete(self, request_id: str, result: OperationResult) -> OperationRecord: ...
    def get(self, request_id: str) -> OperationRecord | None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InMemoryOperationStore:
    """Thread-safe in-process store; awx_spine_aws's DynamoDbOperationStore matches it."""

    def __init__(self, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._records: dict[str, OperationRecord] = {}

    def begin(self, op: Operation) -> OperationRecord:
        digest = body_hash(op)
        with self._lock:
            existing = self._records.get(op.request_id)
            if existing is not None:
                if existing.body_hash != digest:
                    raise RequestIdReuse(
                        op.request_id, stored_hash=existing.body_hash, new_hash=digest
                    )
                return existing
            now = self._now()
            record = OperationRecord(
                request_id=op.request_id,
                kit=op.kit,
                name=op.name,
                business_key=op.business_key,
                intent_hash=op.intent_hash,
                body_hash=digest,
                status=PENDING,
                attempts=0,
                result=None,
                created_at=now,
                updated_at=now,
            )
            self._records[op.request_id] = record
            return record

    def record_attempt(self, request_id: str) -> OperationRecord:
        with self._lock:
            record = self._require(request_id)
            now = self._now()
            updated = replace(
                record,
                attempts=record.attempts + 1,
                status=PENDING,
                updated_at=now,
                attempt_started_at=now,
                first_attempt_at=record.first_attempt_at or now,
            )
            self._records[request_id] = updated
            return updated

    def complete(self, request_id: str, result: OperationResult) -> OperationRecord:
        if result.request_id != request_id:
            raise ValueError("result belongs to a different request_id")
        with self._lock:
            record = self._require(request_id)
            updated = replace(
                record,
                status=str(result.status),
                result=result,
                updated_at=self._now(),
                may_have_landed=record.may_have_landed
                or result.status is OperationStatus.OUTCOME_UNKNOWN,
            )
            self._records[request_id] = updated
            return updated

    def get(self, request_id: str) -> OperationRecord | None:
        with self._lock:
            return self._records.get(request_id)

    def all(self) -> list[OperationRecord]:
        with self._lock:
            return list(self._records.values())

    def _require(self, request_id: str) -> OperationRecord:
        record = self._records.get(request_id)
        if record is None:
            raise UnknownRequestId(request_id)
        return record
