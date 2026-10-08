"""What the mutation adapter does with one operation, shared by the Lambda and local_runner.

The executor is built with max_attempts=1, so one adapter call is at most one send (plus
awx_spine's rule-9 read-back before a send when the store already says OUTCOME_UNKNOWN). The
same holds for a re-emitted operation (kit rule M1): a later run's "send" of an operation
whose record is OUTCOME_UNKNOWN reconciles first and is never a fresh write. Every
resend is the runner's decision (rule 9), never a retry loop hidden inside one invocation, so a
Lambda timeout cannot leave several sends in flight.

Modes
  send      store.begin, then one attempt.
  advance   the same as send; the runner never resends an advance (rule 10).
  resend    the same operation again, after the runner's reconcile found nothing. The
            executor sees the stored OUTCOME_UNKNOWN (or a PENDING record with attempts > 0,
            left by a crash) and applies rule 9 itself: read back first, resend only if it
            did not land and the endpoint deduplicates. Once the record has
            may_have_landed, a refused or rate-limited resend comes back
            OUTCOME_UNKNOWN (the last error in body["last_error"]), never a FAILED, so
            the runner keeps the money in flight and its rule-9 path decides again.
  complete  no send: write the result the runner established (a reconciled object, or a
            final OUTCOME_UNKNOWN after a crash) into the store. An earlier definitive result
            wins, and is what comes back.

The body is sent exactly as the kit built it.
"""

from __future__ import annotations

from typing import Any

from awx_common.ops import Operation, OperationResult, OperationStatus
from awx_spine.executor import MISMATCH_CODE
from awx_spine.store import RequestIdReuse as _SpineRequestIdReuse

from .engine import Action

MAX_ATTEMPTS_PER_CALL = 1
STORED_STATUS_MARK = "stored record status "


class RequestIdReuse(_SpineRequestIdReuse):
    """awx_spine's RequestIdReuse plus the stored record's status, which the runner records in
    the step's results. Same class name, so the state machine still routes it."""

    def __init__(self, original: _SpineRequestIdReuse, stored_status: str | None) -> None:
        super().__init__(
            original.request_id, stored_hash=original.stored_hash, new_hash=original.new_hash
        )
        self.stored_status = stored_status
        self.args = (f"{original}; {STORED_STATUS_MARK}{stored_status}",)


def definitive(result: OperationResult | None) -> bool:
    """The same rule as awx_spine's executor: SUCCEEDED, or a non-retryable FAILED. A FAILED
    request_id_reused (a row in the older format, which stored a mismatch as FAILED) is never
    definitive: a mismatch is in flight."""
    if result is None or result.status is OperationStatus.OUTCOME_UNKNOWN:
        return False
    if legacy_mismatch(result):
        return False
    if result.status is OperationStatus.SUCCEEDED:
        return True
    err = result.error()
    return not (err is not None and err.retryable)


def dispatch(
    executor: Any,
    store: Any,
    op: Operation,
    action: Action | str,
    result: OperationResult | None = None,
) -> OperationResult:
    try:
        return _dispatch(executor, store, op, Action(action), result)
    except _SpineRequestIdReuse as exc:
        if isinstance(exc, RequestIdReuse):
            raise
        record = store.get(op.request_id)
        raise RequestIdReuse(exc, record.status if record else None) from None


def _dispatch(
    executor: Any,
    store: Any,
    op: Operation,
    action: Action,
    result: OperationResult | None,
) -> OperationResult:
    if action is Action.COMPLETE:
        if result is None or result.request_id != op.request_id:
            raise ValueError("complete needs a result for this operation")
        record = store.begin(op)  # RequestIdReuse if the body differs
        if definitive(record.result):
            return record.result
        return as_in_flight(store.complete(op.request_id, result).result)
    return as_in_flight(executor.run(op))


def legacy_mismatch(result: OperationResult | None) -> bool:
    """A stored FAILED request_id_reused: the older format of a mismatch row."""
    return (
        result is not None
        and result.status is OperationStatus.FAILED
        and result.error_code == MISMATCH_CODE
    )


def as_in_flight(result: OperationResult) -> OperationResult:
    """A legacy FAILED mismatch that still wins a DynamoDB COMPLETE (its row was written
    final) is returned in the one mismatch shape, so no runner records it as FAILED."""
    if not legacy_mismatch(result):
        return result
    details = dict((result.body or {}).get("details") or {})
    details.setdefault("provider_id", result.provider_id)
    message = (result.error_message or "request_id_reused") + "; stored as FAILED, in flight"
    return OperationResult(
        request_id=result.request_id,
        status=OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_code=MISMATCH_CODE,
        error_message=message[:300],
        body={
            "details": {
                "fields": list(details.get("fields") or ()),
                "provider_id": details["provider_id"],
            },
            "last_error": {
                "error_type": "StateConflict",
                "error_code": MISMATCH_CODE,
                "http_status": None,
                "error_message": message[:300],
            },
        },
    )
