"""Normalised errors.

The spine maps every Airwallex HTTP status and provider code onto one of these.

retryable       the SAME Operation (same request_id, same body) may be sent again.
outcome_unknown the write may or may not have landed. Read before using any new request_id.
Messages never carry credentials, tokens or request headers.
"""

from __future__ import annotations

from typing import Any


class AwxError(Exception):
    retryable: bool = False
    outcome_unknown: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        provider_request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.provider_request_id = provider_request_id
        self.details = details or {}


class ValidationFailed(AwxError):
    """400 field validation. Fix the request; nothing was created."""


class InsufficientFunds(AwxError):
    """Normalised from the endpoint-specific codes (they differ per endpoint; see the spine)."""


class LimitExceeded(AwxError):
    """A card, spend or platform limit refused the request."""


class DuplicateRequest(AwxError):
    """The provider has already seen this request_id. Read the original instead of retrying."""


class StateConflict(AwxError):
    """The object is not in a state that allows this action."""


class NotFound(AwxError):
    """404."""


class Unauthorized(AwxError):
    """401 or 403 that survived one token refresh."""


class RateLimited(AwxError):
    """429."""

    retryable = True


class TransientServerError(AwxError):
    """5xx before the provider accepted the request."""

    retryable = True


class OutcomeUnknown(AwxError):
    """Timeout, dropped connection or 5xx after submit.

    Resend the same Operation or read it back; never mint a new request_id first.
    """

    retryable = True
    outcome_unknown = True


class TenantScopeViolation(AwxError):
    """Our own guard: an operation's connected account does not match the server-resolved tenant."""


ERROR_TYPES: dict[str, type[AwxError]] = {
    cls.__name__: cls
    for cls in (
        AwxError,
        ValidationFailed,
        InsufficientFunds,
        LimitExceeded,
        DuplicateRequest,
        StateConflict,
        NotFound,
        Unauthorized,
        RateLimited,
        TransientServerError,
        OutcomeUnknown,
        TenantScopeViolation,
    )
}


def error_class(name: str) -> type[AwxError]:
    """Look up an error class by the name stored in OperationResult.error_type."""
    return ERROR_TYPES[name]
