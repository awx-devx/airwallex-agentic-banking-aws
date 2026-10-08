"""Map Airwallex HTTP responses onto awx_common.errors and OperationResult.

The same functions serve AirwallexClient and FakeAirwallex, so a kit sees identical results from
both. Every code below was provoked in the sandbox on 6 Oct 2026 unless marked otherwise; the
request shapes and responses are in FAKE-FIDELITY.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from awx_common import Operation, OperationResult, OperationStatus
from awx_common.errors import (
    AwxError,
    DuplicateRequest,
    InsufficientFunds,
    LimitExceeded,
    NotFound,
    OutcomeUnknown,
    RateLimited,
    StateConflict,
    TransientServerError,
    Unauthorized,
    ValidationFailed,
)

from .endpoints import endpoint_for, extract_ids
from .redact import Redactor

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# One condition, three endpoint-specific codes (transfers, charges and connected account
# transfers, FX conversions).
INSUFFICIENT_FUNDS_CODES = frozenset(
    {"balance_insufficient", "insufficient_fund", "insufficient_funds"}
)

# Five spellings of "this request_id was used before", depending on the endpoint.
DUPLICATE_CODES = frozenset({"duplicate_request_id", "request_id_duplicate", "duplicate_request"})
_DUPLICATE_MESSAGES = ("has been used in the past", "already associated with a previous request")

STATE_CODES = frozenset({"unconfigured_client_fee", "forbidden", "invalid_state", "expired_quote"})
VALIDATION_CODES = frozenset(
    {
        "validation_failed",
        "validation_error",
        "field_required",
        "invalid_argument",
        "bad_request",
    }
)
LIMIT_MESSAGES = frozenset({"amount_above_limit"})


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _details(body: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Airwallex sends details as an object or as a list of {code, message}."""
    raw = body.get("details")
    correlation: str | None = None
    if isinstance(raw, Mapping):
        return dict(raw), None
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping) and item.get("code") == "correlation_id":
                correlation = _str(item.get("message"))
        return {"items": list(raw)}, correlation
    return {}, None


# Per-endpoint shapes (op name -> message fragment -> class). The cardholder duplicate is an
# email collision, not a request_id one, but it means the same thing: the object exists, so the
# executor reads it back by email. A connected-account card authorisation sent without its
# x-on-behalf-of answers 400 "Transaction could not be created" yet creates the authorisation.
_OP_SHAPES: dict[str, tuple[tuple[str, type[AwxError]], ...]] = {
    "issuing.cardholders.create": (("already associated with a cardholder", DuplicateRequest),),
    "issuing.simulate_authorization": (("Transaction could not be created", OutcomeUnknown),),
}
REDIRECT_CODE = "redirect_refused"


def classify(
    method: str,
    status: int,
    code: str | None,
    message: str,
    source: str | None,
    op_name: str | None = None,
) -> type[AwxError]:
    is_write = method.upper() in _WRITE_METHODS
    if 300 <= status < 400:
        # The client never follows redirects (a redirect could leave the sandbox hosts with the
        # credentials). The provider answered without executing: a definitive, non-retryable
        # failure on reads and writes alike.
        return AwxError
    for fragment, cls in _OP_SHAPES.get(op_name or "", ()):
        if fragment in message:
            return cls
    if code in INSUFFICIENT_FUNDS_CODES:
        return InsufficientFunds
    if code in DUPLICATE_CODES:
        return DuplicateRequest
    if code == "invalid_argument" and any(m in message for m in _DUPLICATE_MESSAGES):
        return DuplicateRequest  # FX conversions and card creation use this shape
    if message in LIMIT_MESSAGES:
        return LimitExceeded
    if code in STATE_CODES or "transition is not supported" in message:
        return StateConflict
    if status == 429:
        return RateLimited
    if status >= 500:
        # A 5xx on a write does not prove nothing happened: sandbox simulations returned 500
        # operation_failed and still applied the transition.
        return OutcomeUnknown if is_write else TransientServerError
    if status in (401, 403):
        return Unauthorized
    if status == 404:
        return NotFound
    if status == 409:
        return StateConflict
    if code in VALIDATION_CODES or status in (400, 422):
        return ValidationFailed
    return AwxError


def error_from_response(
    method: str,
    status: int,
    body: Any,
    redactor: Redactor | None = None,
    *,
    provider_request_id: str | None = None,
    op_name: str | None = None,
    headers: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> AwxError:
    redactor = redactor or Redactor()
    payload = body if isinstance(body, Mapping) else {}
    code = _str(payload.get("code"))
    message = _str(payload.get("message")) or f"HTTP {status}"
    if 300 <= status < 400:
        code, message, payload = REDIRECT_CODE, f"HTTP {status} redirect refused", {}
    source = _str(payload.get("source"))
    details, correlation = _details(payload)
    if source:
        details.setdefault("source", source)
    retry_after = retry_after_s(headers, now)
    if retry_after is not None:
        details["retry_after_s"] = retry_after
    cls = classify(method, status, code, message, source, op_name)
    return cls(
        redactor.text(message),
        code=code,
        http_status=status,
        provider_request_id=provider_request_id or correlation,
        details=redactor.value(details),
    )


def retry_after_s(
    headers: Mapping[str, str] | None, now: Callable[[], datetime] | None = None
) -> float | None:
    """Seconds from a Retry-After header (RFC 9110: delta-seconds or an HTTP-date), or None
    when it is absent or unreadable. A date in the past is 0."""
    if not headers:
        return None
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if raw is None or not str(raw).strip():
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or (lambda: datetime.now(UTC)))()).total_seconds())


def result_from_error(op: Operation, err: AwxError) -> OperationResult:
    return OperationResult(
        request_id=op.request_id,
        status=OperationStatus.OUTCOME_UNKNOWN if err.outcome_unknown else OperationStatus.FAILED,
        http_status=err.http_status,
        provider_id=duplicate_object_id(err),
        error_type=type(err).__name__,
        error_code=err.code,
        error_message=err.message,
        body={"details": err.details} if err.details else None,
    )


def result_from_response(
    op: Operation,
    status: int,
    body: Any,
    redactor: Redactor | None = None,
    headers: Mapping[str, str] | None = None,
) -> OperationResult:
    if 200 <= status < 300:
        provider_id, provider_status = extract_ids(endpoint_for(op.name, op.path), body)
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.SUCCEEDED,
            http_status=status,
            provider_id=provider_id,
            provider_status=provider_status,
            body=body if isinstance(body, Mapping) else {"items": body},
        )
    return result_from_error(
        op, error_from_response(op.method, status, body, redactor, op_name=op.name, headers=headers)
    )


def duplicate_object_id(err: AwxError) -> str | None:
    """The original object's id, when a duplicate-request rejection names it."""
    if not isinstance(err, DuplicateRequest):
        return None
    for key in ("id", "source_id"):
        value = err.details.get(key)
        if isinstance(value, str) and value:
            return value
    return None
